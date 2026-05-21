"""
Denni planovac - dynamicky prepocet podle skutecne spotreby.

V3.1 FIX (24.4.2026):
- Planovac si sam prepocitava `predicted_pv_kwh_remaining` z hourly_radiation
  podle AKTUALNI hodiny. Open-Meteo fetch bezi jen kazde 4h, ale plan se pocita
  kazdych 5 min a mezi fetchy bylo PV remaining zastarale.
- Pro lepsi presnost v prubehu hodiny se vyuzije aktualni minuta (proportion of
  hour) - tim se plynule snizuje predikce behem hodiny, ne skokove.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from ..state import SystemContext, DayStrategy

log = logging.getLogger("planner")


def _fmt(val, digits=1):
    if val is None: return "n/a"
    return f"{val:.{digits}f}"


@dataclass
class PlannerConfig:
    battery_kwh_total: float = 32.0
    battery_reserve_pct: float = 20.0
    battery_round_trip_eff: float = 0.9
    baseline_consumption_kwh: float = 12.0
    aggressive_threshold_kwh: float = 15.0
    normal_threshold_kwh: float = 8.0
    conservative_threshold_kwh: float = 3.0
    surplus_on_aggressive_w: float = 300
    surplus_on_normal_w: float = 1500
    surplus_on_conservative_w: float = 2500
    surplus_off_ratio: float = 0.5
    refresh_interval_sec: int = 300


class Planner:
    def __init__(self, context: SystemContext, config: PlannerConfig):
        self.ctx = context
        self.cfg = config
        self._shutdown = asyncio.Event()

    def _compute_pv_remaining_live(self, forecast, installed_kwp=11.8, efficiency=0.75) -> float:
        """
        Dynamicky prepocet PV remaining pro aktualni moment.

        Openmeteo posila hodinova data (hourly_radiation, hourly_times).
        Pocitame od ted do konce dne:
        - Pro aktualni hodinu: jen proportion zbyvajici casti hodiny
        - Pro dalsi hodiny: cela energie (radiation / 1000 * kWp * eff)
        """
        if not forecast.hourly_radiation:
            return 0.0
        now = datetime.now()
        hour = now.hour
        minute_frac = now.minute / 60.0   # kolik casti hodiny uz uplynulo

        total = 0.0
        for i, rad in enumerate(forecast.hourly_radiation):
            if rad is None:
                continue
            kwh_full = (rad / 1000.0) * installed_kwp * efficiency
            if i < hour:
                continue            # hodina uz probehla - ignoruj
            elif i == hour:
                # zbyvajici cast aktualni hodiny
                total += kwh_full * (1.0 - minute_frac)
            else:
                total += kwh_full
        return round(total, 2)

    def _estimate_pv_from_lux(self, ctx, hours_until_sunset: float) -> float:
        """v3.4 NEW: Fallback estimace PV remaining když Open-Meteo nefetchne.

        Pouzije aktualni jas z Loxone meteostanice + zbyvajici hodiny do zapadu slunce.
        Hruby vzorec: clear sky ~ 50000+ lux = ~7 kWh/h pro 11.8 kWp instalaci.

        Pri zatazeno (5000-15000 lux) bude vrobaa ~1-2 kWh/h.
        """
        e = ctx.env
        if e.is_stale or e.light_lux is None or e.light_lux < 1000:
            return 0.0

        # Mapovani lux -> kWh/h (kalibrovano pro 11.8 kWp Bojanovice setup)
        # 50000+ lux (plne slunce, poledne) = ~7 kWh/h
        # 30000 lux (polojasno) = ~3.5 kWh/h
        # 10000 lux (zatazeno) = ~1 kWh/h
        # 5000 lux = ~0.4 kWh/h
        rate_kwh_per_h = (e.light_lux / 50000.0) * 7.0
        rate_kwh_per_h = min(rate_kwh_per_h, 9.0)  # cap na realny peak

        # Linearni odhad: predpoklada se ze slunce postupne klesa, takze
        # opravdovy total bude cca 60% rate * hours (rate_now je peak this moment)
        if hours_until_sunset <= 0:
            return 0.0
        estimated = rate_kwh_per_h * hours_until_sunset * 0.6
        return round(estimated, 1)

    def _hours_until_sunset(self, ctx) -> float:
        """v3.4 NEW: Hodiny do zapadu slunce. Fallback na 18:00 pokud forecast chybi."""
        f = ctx.forecast
        now = datetime.now()
        if f.sunset:
            try:
                # format z Open-Meteo: "2026-04-26T19:34"
                sunset_dt = datetime.fromisoformat(f.sunset.replace('T', ' '))
                delta = (sunset_dt - now).total_seconds() / 3600.0
                return max(0.0, delta)
            except (ValueError, AttributeError):
                pass
        # Fallback: predpokladej zapad v 19:30 (jaro/leto v CR)
        sunset_default = now.replace(hour=19, minute=30, second=0, microsecond=0)
        delta = (sunset_default - now).total_seconds() / 3600.0
        return max(0.0, delta)

    def compute_plan(self) -> None:
        v = self.ctx.victron
        f = self.ctx.forecast
        e = self.ctx.env
        en = self.ctx.energy   # v3.7.5 FIX: EnergyTracker (session stats)
        plan = self.ctx.plan

        # FIX v3.1: dynamic PV remaining - nepouzij zastarala data z Open-Meteo
        # FIX v3.4: Pokud Open-Meteo nemame, FALLBACK na lux+yield_today
        pv_source = "unknown"
        if f.hourly_radiation:
            pv_predicted = self._compute_pv_remaining_live(f)
            f.predicted_pv_kwh_remaining = pv_predicted
            pv_source = "openmeteo-live"
        else:
            # Open-Meteo nikdy neufetchnul nebo selhal
            # FALLBACK: odhadni z aktualni svetlosti + yield_today
            hours_left = self._hours_until_sunset(self.ctx)
            pv_predicted = self._estimate_pv_from_lux(self.ctx, hours_left)
            f.predicted_pv_kwh_remaining = pv_predicted
            pv_source = f"lux-fallback ({hours_left:.1f}h to sunset)"
            if pv_predicted == 0.0 and v.pv_yield_today_kwh is not None:
                # Nemame ani lux data - aspon vime kolik se uz vyrobilo
                # Pokud uz svitilo dnes (yield > 5 kWh) a slunce jeste nezaslo,
                # predpokladej ze pojede aspon polovina rychlosti dosavadni vyroby
                hour = datetime.now().hour
                if 6 < hour < 19 and v.pv_yield_today_kwh > 2.0:
                    rate_so_far = v.pv_yield_today_kwh / max(1, hour - 6)
                    pv_predicted = round(rate_so_far * hours_left * 0.5, 1)
                    pv_source = "yield-extrapolation"

        if v.soc_pct is not None:
            usable_soc = max(0, v.soc_pct - self.cfg.battery_reserve_pct)
            bat_available = (usable_soc / 100.0) * self.cfg.battery_kwh_total \
                            * self.cfg.battery_round_trip_eff
        else:
            bat_available = None

        hour = datetime.now().hour
        remaining_fraction = max(0, (24 - hour) / 24.0)

        session_hours = (time.time() - en.session_start) / 3600.0
        if session_hours >= 0.5 and en.home_consumed_kwh > 0:
            home_rate_kwh_per_h = en.home_consumed_kwh / session_hours
            hours_remaining = 24 - hour + (60 - datetime.now().minute) / 60.0
            baseline_remaining = home_rate_kwh_per_h * hours_remaining
        else:
            baseline_remaining = self.cfg.baseline_consumption_kwh * remaining_fraction

        if pv_predicted is None or bat_available is None:
            discretionary = None
        else:
            discretionary = pv_predicted + bat_available - baseline_remaining

        if discretionary is None:
            strategy = DayStrategy.UNKNOWN
            reason = "cekam na data"
        elif discretionary > self.cfg.aggressive_threshold_kwh:
            strategy = DayStrategy.AGGRESSIVE
            reason = f"hodne energie ({discretionary:.1f} kWh volne)"
        elif discretionary > self.cfg.normal_threshold_kwh:
            strategy = DayStrategy.NORMAL
            reason = f"standardni den ({discretionary:.1f} kWh volne)"
        elif discretionary > self.cfg.conservative_threshold_kwh:
            strategy = DayStrategy.CONSERVATIVE
            reason = f"sporny rozpocet ({discretionary:.1f} kWh volne)"
        else:
            strategy = DayStrategy.SURVIVE
            reason = f"setrim baterku ({discretionary:.1f} kWh volne)"

        on_w, off_w = self._thresholds_for(strategy)

        plan.strategy = strategy
        plan.predicted_pv_kwh = pv_predicted
        plan.battery_available_kwh = bat_available
        plan.baseline_consumption_kwh = baseline_remaining
        plan.discretionary_kwh = discretionary
        plan.dynamic_surplus_on_w = on_w
        plan.dynamic_surplus_off_w = off_w
        plan.computed_at = time.time()
        plan.reason = reason

        log.info(
            f"Plan: {strategy.value} | "
            f"PV_rem={_fmt(pv_predicted)}kWh ({pv_source}) "
            f"bat={_fmt(bat_available)}kWh "
            f"baseline_rem={_fmt(baseline_remaining)}kWh "
            f"discr={_fmt(discretionary)}kWh | "
            f"on={on_w}W off={off_w}W | {reason}"
        )

    def _thresholds_for(self, strategy: DayStrategy):
        if strategy == DayStrategy.AGGRESSIVE:
            on = self.cfg.surplus_on_aggressive_w
        elif strategy == DayStrategy.NORMAL:
            on = self.cfg.surplus_on_normal_w
        elif strategy == DayStrategy.CONSERVATIVE:
            on = self.cfg.surplus_on_conservative_w
        elif strategy == DayStrategy.SURVIVE:
            return (float("inf"), float("inf"))
        else:
            on = self.cfg.surplus_on_normal_w
        off = on * self.cfg.surplus_off_ratio
        return (on, off)

    async def _loop(self):
        while not self._shutdown.is_set():
            try:
                self.compute_plan()
            except Exception as e:
                log.exception(f"plan compute error: {e}")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.cfg.refresh_interval_sec)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        log.info(f"Planner starting (refresh {self.cfg.refresh_interval_sec}s) - v3.1 live PV")
        asyncio.create_task(self._loop())

    async def stop(self):
        self._shutdown.set()
