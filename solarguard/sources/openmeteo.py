"""Open-Meteo API - predpoved pocasi + slunecni radiace."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import aiohttp

log = logging.getLogger("openmeteo")

API_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoSource:
    def __init__(self, latitude: float, longitude: float, context,
                 installed_kwp: float = 11.8, efficiency: float = 0.75,
                 refresh_interval: int = 14400):
        self.lat = latitude
        self.lon = longitude
        self.ctx = context
        self.installed_kwp = installed_kwp
        self.efficiency = efficiency
        self.refresh_interval = refresh_interval
        self._session: Optional[aiohttp.ClientSession] = None
        self._shutdown = asyncio.Event()

    async def _fetch_forecast(self) -> bool:
        params = {
            "latitude": self.lat, "longitude": self.lon,
            "hourly": "shortwave_radiation,temperature_2m,cloudcover,precipitation_probability",
            "daily": "sunrise,sunset",
            "forecast_days": 1, "timezone": "Europe/Prague",
        }
        try:
            async with self._session.get(API_URL, params=params,
                                          timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning(f"Open-Meteo HTTP {r.status}: {body[:300]}")
                    return False
                data = await r.json()
        except asyncio.TimeoutError:
            log.warning(f"Open-Meteo TIMEOUT (15s) connecting to {API_URL}")
            return False
        except aiohttp.ClientError as e:
            log.warning(f"Open-Meteo network error: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            log.exception(f"Open-Meteo fetch unexpected error: {e}")
            return False

        # v3.7.4 FIX: validuj že data jsou kompletni (ne prazdna)
        hourly = data.get("hourly", {})
        daily = data.get("daily", {})
        radiation = hourly.get("shortwave_radiation", [])
        if not radiation or len(radiation) < 12:
            log.warning(
                f"Open-Meteo returned incomplete data: radiation={len(radiation)} items, "
                f"raw response keys: {list(data.keys())}"
            )
            return False

        f = self.ctx.forecast
        f.hourly_times = hourly.get("time", [])
        f.hourly_radiation = radiation
        f.hourly_temp = hourly.get("temperature_2m", [])
        f.hourly_cloudcover = hourly.get("cloudcover", [])
        f.hourly_rain_prob = hourly.get("precipitation_probability", [])

        f.sunrise = (daily.get("sunrise") or [None])[0]
        f.sunset = (daily.get("sunset") or [None])[0]

        total_kwh = sum(
            (rad or 0) / 1000.0 * self.installed_kwp * self.efficiency
            for rad in f.hourly_radiation
        )
        f.predicted_pv_kwh_today = round(total_kwh, 1)

        now_hour = datetime.now().hour
        remaining_kwh = sum(
            (rad or 0) / 1000.0 * self.installed_kwp * self.efficiency
            for i, rad in enumerate(f.hourly_radiation)
            if i >= now_hour
        )
        f.predicted_pv_kwh_remaining = round(remaining_kwh, 1)

        f.last_update = time.time()

        log.info(
            f"Forecast: predicted PV today={f.predicted_pv_kwh_today} kWh, "
            f"remaining={f.predicted_pv_kwh_remaining} kWh "
            f"({len(radiation)} hours), "
            f"sunrise={f.sunrise}, sunset={f.sunset}"
        )
        return True

    async def _poll_loop(self):
        # v3.4 FIX: Po startu zkusime fetch každých 30s 5x, pak teprve 4h interval.
        # v3.7.4 FIX: Pokud i po quick retries selze, dej do "slow retry" rezimu
        # (10 min) misto cekani 4h - aby se nenecekalo cely den na ranni vypadek.
        retry_count = 0
        max_quick_retries = 5
        quick_retry_sec = 30
        slow_retry_sec = 600  # 10 min mezi pokusy po vyprseni quick retries

        while not self._shutdown.is_set():
            success = False
            try:
                success = await self._fetch_forecast()
            except Exception as ex:
                log.exception(f"Open-Meteo loop unexpected error: {ex}")

            # Rozhodnout interval do dalsiho fetche
            if success:
                if retry_count > 0:
                    log.info(f"Open-Meteo recovered after {retry_count} retries")
                retry_count = 0
                wait_sec = self.refresh_interval
            elif retry_count < max_quick_retries:
                retry_count += 1
                wait_sec = quick_retry_sec
                log.warning(
                    f"Open-Meteo fetch failed (attempt {retry_count}/{max_quick_retries}), "
                    f"quick-retry in {wait_sec}s"
                )
            else:
                # Quick retries vyprsely - jdeme do slow retry (10 min) misto 4h
                wait_sec = slow_retry_sec
                log.warning(
                    f"Open-Meteo still failing after {retry_count} retries, "
                    f"falling back to slow-retry every {wait_sec}s"
                )

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=wait_sec)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        self._session = aiohttp.ClientSession()
        log.info(
            f"Open-Meteo source starting "
            f"(lat={self.lat}, lon={self.lon}, kWp={self.installed_kwp}, "
            f"refresh {self.refresh_interval}s, quick-retry on failure)"
        )
        asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._shutdown.set()
        if self._session:
            await self._session.close()
