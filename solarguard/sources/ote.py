"""
v3.7.2 NEW: OTE-CR spotove ceny - pres spotovaelektrina.cz API.

Od 1.10.2025 OTE primarne 15-min data, ale spotovaelektrina.cz dela
hodinovy prumer pres /api/v1/price/get-prices-json. To pouzivame.

API je zdarma, bez API klice, vraci uz CZK i EUR rovnou.

Update strategy: kazdou hodinu (3600s).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List

import aiohttp

log = logging.getLogger("ote")

PRICES_API_URL = "https://spotovaelektrina.cz/api/v1/price/get-prices-json"
CNB_API_URL = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"


class OteSource:
    def __init__(
        self,
        context,
        eur_to_kc: float = 25.0,
        fee_kc_per_kwh: float = 1.5,
        refresh_interval: int = 3600,
    ):
        self.ctx = context
        self.ctx.spot.eur_to_kc = eur_to_kc
        self.ctx.spot.fee_kc_per_kwh = fee_kc_per_kwh
        self.refresh_interval = refresh_interval
        self._session: Optional[aiohttp.ClientSession] = None
        self._shutdown = asyncio.Event()

    async def _fetch_eur_rate(self) -> Optional[float]:
        """CNB denni kurz - format: 'země|měna|množství|kód|kurz'."""
        try:
            async with self._session.get(
                CNB_API_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return None
                text = await r.text()
                for line in text.splitlines():
                    if "|EUR|" in line:
                        parts = line.split("|")
                        if len(parts) >= 5:
                            mnozstvi = float(parts[2].replace(",", "."))
                            kurz = float(parts[4].replace(",", "."))
                            return kurz / mnozstvi
            return None
        except Exception as e:
            log.debug(f"CNB rate fetch error: {e}")
            return None

    async def _fetch_prices(self) -> bool:
        """Stahne hodinove ceny ze spotovaelektrina.cz.

        Format JSON: {
          "hoursToday": [
            {"hour": 0, "priceCZK": 1024.5, "priceEur": 41.45},
            ...
          ],
          "hoursTomorrow": [...]
        }

        priceCZK je v Kc/MWh BEZ DPH a poplatku.
        """
        try:
            async with self._session.get(
                PRICES_API_URL, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status != 200:
                    log.warning(f"spotovaelektrina HTTP {r.status}")
                    return False
                data = await r.json(content_type=None)
        except Exception as e:
            log.warning(f"spotovaelektrina fetch error: {e}")
            return False

        try:
            today_hours = data.get("hoursToday", [])
            tomorrow_hours = data.get("hoursTomorrow", [])

            if not today_hours:
                log.warning("spotovaelektrina: hoursToday is empty")
                return False

            today_sorted = sorted(today_hours, key=lambda x: x.get("hour", 0))

            today_prices_eur = []
            today_prices_kc_mwh = []
            for h in today_sorted:
                eur = h.get("priceEur")
                kc = h.get("priceCZK")
                if eur is not None:
                    today_prices_eur.append(float(eur))
                if kc is not None:
                    today_prices_kc_mwh.append(float(kc))

            if len(today_prices_eur) < 24:
                log.warning(f"spotovaelektrina returned only {len(today_prices_eur)} hours for today")
                while len(today_prices_eur) < 24:
                    today_prices_eur.append(0.0)

            self.ctx.spot.today_prices_eur = today_prices_eur[:24]
            self.ctx.spot.today_date = datetime.now().date().isoformat()

            # Update EUR kurz z dat
            if today_prices_eur and today_prices_kc_mwh:
                for eur, kc in zip(today_prices_eur, today_prices_kc_mwh):
                    if eur > 0 and kc > 0:
                        rate = kc / eur
                        if 20 <= rate <= 30:
                            self.ctx.spot.eur_to_kc = round(rate, 2)
                            break

            log.info(
                f"OTE today: {len(today_prices_eur)} hours, "
                f"min={min(today_prices_eur):.1f}, "
                f"max={max(today_prices_eur):.1f} EUR/MWh "
                f"(EUR/CZK={self.ctx.spot.eur_to_kc})"
            )

            if tomorrow_hours:
                tomorrow_sorted = sorted(tomorrow_hours, key=lambda x: x.get("hour", 0))
                tomorrow_prices_eur = [
                    float(h.get("priceEur", 0)) for h in tomorrow_sorted
                ]
                if any(p != 0 for p in tomorrow_prices_eur):
                    self.ctx.spot.tomorrow_prices_eur = tomorrow_prices_eur[:24]
                    self.ctx.spot.tomorrow_date = (
                        datetime.now().date() + timedelta(days=1)
                    ).isoformat()
                    log.info(f"OTE tomorrow: {len(tomorrow_prices_eur)} hours published")
                else:
                    self.ctx.spot.tomorrow_prices_eur = []
                    self.ctx.spot.tomorrow_date = None
            else:
                self.ctx.spot.tomorrow_prices_eur = []
                self.ctx.spot.tomorrow_date = None

            self.ctx.spot.last_update = time.time()
            return True

        except Exception as e:
            log.warning(f"OTE parse error: {e}")
            return False

    async def _fetch_all(self) -> bool:
        success = await self._fetch_prices()

        # Fallback CNB rate
        if self.ctx.spot.eur_to_kc == 25.0:
            rate = await self._fetch_eur_rate()
            if rate:
                self.ctx.spot.eur_to_kc = round(rate, 2)
                log.debug(f"CNB fallback EUR/CZK = {rate:.2f}")

        return success

    async def _poll_loop(self) -> None:
        retry_count = 0
        while not self._shutdown.is_set():
            success = False
            try:
                success = await self._fetch_all()
            except Exception as ex:
                log.error(f"OTE loop error: {ex}")

            if not success and retry_count < 3:
                retry_count += 1
                wait = 60
                log.warning(f"OTE retry {retry_count}/3 in {wait}s")
            else:
                if success and retry_count > 0:
                    log.info(f"OTE recovered after {retry_count} retries")
                retry_count = 0
                wait = self.refresh_interval

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": "SolarGuard/3.7.3"}
        )
        log.info(
            f"OTE source starting (api=spotovaelektrina.cz, "
            f"eur_to_kc fallback={self.ctx.spot.eur_to_kc}, "
            f"fee={self.ctx.spot.fee_kc_per_kwh}, refresh {self.refresh_interval}s)"
        )
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._shutdown.set()
        if self._session:
            await self._session.close()
