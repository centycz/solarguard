"""
Loxone Miniserver - lokalni HTTP REST API.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger("loxone")


SENSORS = {
    "light_lux":    "16397d17-0246-1000-ffff42cc3cb2327c",
    "air_temp_c":   "16397d17-0246-1008-ffff42cc3cb2327c",
    "wind_kmh":     "16397d17-0246-1004-ffff42cc3cb2327c",
    "is_raining":   "16397d17-0246-1013-ffff42cc3cb2327c",
}


class LoxoneSource:
    def __init__(self, host: str, username: str, password: str, context,
                 poll_interval: int = 60):
        self.host = host
        self.username = username
        self.password = password
        self.ctx = context
        self.poll_interval = poll_interval
        self._session: Optional[aiohttp.ClientSession] = None
        self._shutdown = asyncio.Event()

    async def _fetch_value(self, uuid: str) -> Optional[float]:
        url = f"http://{self.host}/jdev/sps/io/{uuid}/state"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    log.warning(f"Loxone HTTP {r.status} for {uuid}")
                    return None
                data = await r.json(content_type=None)
                val_str = data.get("LL", {}).get("value")
                if val_str is None: return None
                import re
                m = re.match(r"[-+]?\d*\.?\d+", str(val_str).replace(",", "."))
                if m: return float(m.group(0))
                return None
        except asyncio.TimeoutError:
            log.warning(f"Loxone TIMEOUT for {uuid}")
            return None
        except Exception as e:
            log.warning(f"Loxone fetch error for {uuid}: {e}")
            return None

    async def _poll_once(self):
        results = {}
        for name, uuid in SENSORS.items():
            val = await self._fetch_value(uuid)
            results[name] = val

        e = self.ctx.env
        if results.get("air_temp_c") is not None:
            e.air_temp_c = results["air_temp_c"]
        if results.get("light_lux") is not None:
            e.light_lux = results["light_lux"]
        if results.get("wind_kmh") is not None:
            e.wind_kmh = results["wind_kmh"]
        if results.get("is_raining") is not None:
            e.is_raining = bool(results["is_raining"])
        e.last_update = time.time()

        log.info(f"Loxone: temp={e.air_temp_c}C, light={e.light_lux}Lx, "
                 f"wind={e.wind_kmh}km/h, rain={e.is_raining}")

    async def _poll_loop(self):
        while not self._shutdown.is_set():
            try:
                await self._poll_once()
            except Exception as ex:
                log.error(f"Loxone poll error: {ex}")
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        auth = aiohttp.BasicAuth(self.username, self.password)
        self._session = aiohttp.ClientSession(auth=auth)
        log.info(f"Loxone source starting ({self.host}, poll {self.poll_interval}s)")
        asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._shutdown.set()
        if self._session:
            await self._session.close()
