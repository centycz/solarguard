"""
v3.8 NEW: InfluxDB 2.x writer.

Posila metriky do InfluxDB v line protocol formatu pres HTTP API.
Pouziva Bearer token auth (InfluxDB 2.x default).

Pokud InfluxDB neni dostupna, SolarGuard funguje dal - jen prestane logovat.
Po obnoveni connection (kazdych 60s retry) se znovu zacne posílat.

Buffering: pokud se write nepodari, pridaji se points do bufferu (max 1000),
po obnoveni se posle vsechno najednou.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Deque, Dict, Optional

import aiohttp

log = logging.getLogger("influx")


class InfluxWriter:
    """Async writer do InfluxDB 2.x."""

    BUFFER_MAX = 1000           # max points v bufferu pri vypadku
    FLUSH_INTERVAL_SEC = 30      # auto flush kazdych 30s pro low-volume metriky
    BATCH_SIZE = 500            # max points v jednom HTTP requestu

    def __init__(
        self,
        url: str,
        org: str,
        bucket: str,
        token: str,
        location_tag: str = "bojanovice",
    ):
        self.url = url.rstrip("/")
        self.org = org
        self.bucket = bucket
        self.token = token
        self.location = location_tag

        self._buffer: Deque[str] = deque(maxlen=self.BUFFER_MAX)
        self._session: Optional[aiohttp.ClientSession] = None
        self._shutdown = asyncio.Event()
        self._available = False
        self._last_flush_attempt: float = 0
        self._consecutive_failures = 0
        self._total_points_written = 0
        self._total_points_dropped = 0

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def stats(self) -> dict:
        return {
            "configured": True,
            "url": self.url,
            "available": self._available,
            "buffer_size": len(self._buffer),
            "consecutive_failures": self._consecutive_failures,
            "total_written": self._total_points_written,
            "total_dropped": self._total_points_dropped,
        }

    @staticmethod
    def _escape_tag(s: str) -> str:
        return str(s).replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")

    @staticmethod
    def _format_field(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            if isinstance(value, int):
                return f"{value}i"
            return str(value)
        s = str(value).replace('"', '\\"')
        return f'"{s}"'

    def _make_line(
        self, measurement: str, fields: Dict,
        tags: Optional[Dict] = None,
        timestamp_ns: Optional[int] = None,
    ) -> Optional[str]:
        all_tags = {"location": self.location}
        if tags:
            all_tags.update(tags)
        tag_str = ",".join(
            f"{k}={self._escape_tag(v)}"
            for k, v in sorted(all_tags.items())
            if v is not None
        )
        field_parts = []
        for k, v in fields.items():
            formatted = self._format_field(v)
            if formatted is not None:
                field_parts.append(f"{k}={formatted}")
        if not field_parts:
            return None
        field_str = ",".join(field_parts)
        ts = timestamp_ns if timestamp_ns is not None else int(time.time() * 1e9)
        return f"{measurement},{tag_str} {field_str} {ts}"

    def write_point(
        self, measurement: str, fields: Dict,
        tags: Optional[Dict] = None,
        timestamp_ns: Optional[int] = None,
    ) -> None:
        line = self._make_line(measurement, fields, tags, timestamp_ns)
        if line:
            if len(self._buffer) >= self.BUFFER_MAX:
                self._total_points_dropped += 1
            self._buffer.append(line)

    async def _flush(self) -> bool:
        if not self._buffer or self._session is None:
            return True

        batch = []
        for _ in range(min(self.BATCH_SIZE, len(self._buffer))):
            batch.append(self._buffer.popleft())

        body = "\n".join(batch)
        url = f"{self.url}/api/v2/write"
        params = {"org": self.org, "bucket": self.bucket, "precision": "ns"}
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "text/plain; charset=utf-8",
        }

        try:
            async with self._session.post(
                url, params=params, headers=headers, data=body,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status in (200, 204):
                    self._total_points_written += len(batch)
                    if not self._available:
                        log.info(f"InfluxDB recovered, wrote {len(batch)} points")
                    self._available = True
                    self._consecutive_failures = 0
                    return True
                err_body = await resp.text()
                log.warning(f"InfluxDB write HTTP {resp.status}: {err_body[:200]}")
                if resp.status >= 500:
                    for line in reversed(batch):
                        self._buffer.appendleft(line)
                else:
                    self._total_points_dropped += len(batch)
                self._available = False
                self._consecutive_failures += 1
                return False
        except asyncio.TimeoutError:
            log.warning("InfluxDB write TIMEOUT")
            for line in reversed(batch):
                self._buffer.appendleft(line)
            self._available = False
            self._consecutive_failures += 1
            return False
        except Exception as e:
            log.warning(f"InfluxDB write error: {e}")
            for line in reversed(batch):
                self._buffer.appendleft(line)
            self._available = False
            self._consecutive_failures += 1
            return False

    async def _check_health(self) -> bool:
        if self._session is None:
            return False
        try:
            async with self._session.get(
                f"{self.url}/health",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _flush_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                if not self._available and self._consecutive_failures > 0:
                    if await self._check_health():
                        log.info("InfluxDB health check passed - resuming writes")
                        self._available = True
                if self._buffer:
                    await self._flush()
                self._last_flush_attempt = time.time()
            except Exception as e:
                log.exception(f"flush loop error: {e}")
            wait = self.FLUSH_INTERVAL_SEC if self._available else 60
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        ok = await self._check_health()
        if ok:
            log.info(f"InfluxDB connected: {self.url} bucket={self.bucket}")
            self._available = True
        else:
            log.warning(
                f"InfluxDB initial health check FAILED ({self.url}). "
                f"Will retry in background. SolarGuard works without it."
            )
        asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._shutdown.set()
        if self._buffer and self._available:
            log.info(f"InfluxDB final flush: {len(self._buffer)} points")
            try:
                await asyncio.wait_for(self._flush(), timeout=5)
            except asyncio.TimeoutError:
                pass
        if self._session:
            await self._session.close()


# ===== Helper funkce pro snadne posilani z main.py =====

def write_solar_metrics(writer: InfluxWriter, ctx) -> None:
    if writer is None: return
    v = ctx.victron
    fields = {
        "soc_pct": v.soc_pct,
        "pv_w": v.pv_power_w,
        "battery_w": v.battery_power_w,
        "grid_w": v.grid_total_w,
        "load_w": v.load_total_w,
        "surplus_w": v.surplus_w,
    }
    writer.write_point("solar", fields)

    phases_fields = {
        "grid_l1_w": v.grid_l1_w,
        "grid_l2_w": v.grid_l2_w,
        "grid_l3_w": v.grid_l3_w,
        "load_l1_w": v.load_l1_w,
        "load_l2_w": v.load_l2_w,
        "load_l3_w": v.load_l3_w,
    }
    writer.write_point("phases", phases_fields)

    if v.pv_yield_today_kwh is not None or v.consumption_today_kwh is not None:
        writer.write_point("energy_daily", {
            "pv_yield_kwh": v.pv_yield_today_kwh,
            "consumption_kwh": v.consumption_today_kwh,
            "battery_in_kwh": v.battery_in_today_kwh,
        })


def write_spa_metrics(writer: InfluxWriter, ctx) -> None:
    if writer is None: return
    s = ctx.spa
    if not s.online: return
    fields = {
        "water_temp_c": s.current_temp_c,
        "target_temp_c": s.target_temp_c,
        "heater_on": s.heater_on,
        "filter_on": s.filter_on,
        "bubbles_on": s.bubbles_on,
        "jets_on": s.jets_on,
        "online": s.online,
    }
    # Sanitizer pokud existuje
    if hasattr(s, 'sanitizer_on'):
        fields["sanitizer_on"] = s.sanitizer_on
    writer.write_point("spa", fields)


def write_env_metrics(writer: InfluxWriter, ctx) -> None:
    if writer is None: return
    e = ctx.env
    if e.is_stale: return
    writer.write_point("environment", {
        "air_temp_c": e.air_temp_c,
        "light_lux": e.light_lux,
        "wind_kmh": e.wind_kmh,
        "is_raining": e.is_raining,
    })


def write_decision_metrics(writer: InfluxWriter, ctx, decision_reason: str = "") -> None:
    if writer is None: return
    p = ctx.plan
    state = ctx.current_state.value if ctx.current_state else "unknown"
    strategy = p.strategy.value if p.strategy else "unknown"
    fields = {
        "state": state,
        "strategy": strategy,
        "surplus_on_w": p.dynamic_surplus_on_w,
        "surplus_off_w": p.dynamic_surplus_off_w,
        "discretionary_kwh": p.discretionary_kwh,
        "predicted_pv_kwh": p.predicted_pv_kwh,
        "battery_available_kwh": p.battery_available_kwh,
    }
    writer.write_point("decisions", fields, tags={
        "state": state, "strategy": strategy,
    })


def write_spot_metrics(writer: InfluxWriter, ctx) -> None:
    if writer is None: return
    sp = ctx.spot
    if sp.is_stale: return
    price = sp.current_price_kc()
    if price is None: return
    writer.write_point("spot_price", {
        "price_kc_per_kwh": price,
        "eur_to_kc": sp.eur_to_kc,
    })


def write_heating_sample(writer: InfluxWriter, sample, model_info: dict) -> None:
    """Volat z heating_curve.on_heating_stop kdyz je novy validni vzorek."""
    if writer is None or sample is None: return
    writer.write_point("heating_curve_samples", {
        "start_temp_c": sample.start_temp,
        "end_temp_c": sample.end_temp,
        "delta_c": sample.temp_delta,
        "duration_min": sample.duration_minutes,
        "min_per_degree": sample.minutes_per_degree,
        "air_temp_c": sample.air_temp_c,
        "wind_kmh": sample.wind_kmh,
        "n_samples_total": model_info.get("n_samples"),
        "learned_correction": model_info.get("learned_correction"),
    }, timestamp_ns=int(sample.timestamp * 1e9))
