"""
v3.6 NEW: Casovy planovac scen.

Sleduje kalendar a v urcity cas spousti scenu (Solar auto / Mirny 33C / Heat now).
Pravidla jsou v config.yaml pod klicem 'schedule:'.

Priklad pravidla:
    - name: "Deti dopoledne"
      enabled: true
      days: [Mo, Tu, We, Th, Fr]
      time: "10:00"
      scene: gentle           # gentle | solar_auto | heat_now
      target_temp_c: 33       # volitelne, pro gentle scenu
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Awaitable

from ..state import SystemContext

log = logging.getLogger("scheduler")

DAYS_MAP = {
    "Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6,
    "Po": 0, "Ut": 1, "St": 2, "Ct": 3, "Pa": 4, "So": 5, "Ne": 6,
}


@dataclass
class ScheduleRule:
    name: str
    enabled: bool
    days: List[int]              # 0=Mo .. 6=Su
    time_str: str                # "HH:MM"
    scene: str                   # 'gentle' | 'solar_auto' | 'heat_now'
    target_temp_c: Optional[int] = None
    last_triggered: Optional[float] = None  # epoch sec, pro debouncing

    def matches_now(self, now: datetime, debounce_sec: int = 90) -> bool:
        """Vrati True pokud by pravidlo melo trigger nyni."""
        if not self.enabled:
            return False
        if now.weekday() not in self.days:
            return False

        # Parse "HH:MM"
        try:
            hh, mm = self.time_str.split(":")
            target_hour = int(hh)
            target_min = int(mm)
        except (ValueError, AttributeError):
            return False

        # Match: aktualni cas = target +/- 30s (tick je 30s, takze 1 minute window)
        # ALE musi to byt dost daleko od posledniho triggeru aby se to nesplnilo 2x
        if now.hour != target_hour or now.minute != target_min:
            return False
        if self.last_triggered:
            age = time.time() - self.last_triggered
            if age < debounce_sec:
                return False
        return True


class Scheduler:
    """Casovy planovac - kontroluje pravidla kazdych 30s a spousti sceny.

    SceneCallback: async funkce co umi spustit scenu, signature:
        async def callback(scene_id: str, target_temp_c: Optional[int]) -> bool
    """

    def __init__(
        self,
        ctx: SystemContext,
        rules: List[ScheduleRule],
        scene_callback: Callable[[str, Optional[int]], Awaitable[bool]],
        check_interval_sec: int = 30,
        global_enabled: bool = True,
    ):
        self.ctx = ctx
        self.rules = rules
        self.scene_callback = scene_callback
        self.check_interval = check_interval_sec
        self.global_enabled = global_enabled
        self._shutdown = asyncio.Event()
        self._last_check: float = 0.0

    def set_global_enabled(self, enabled: bool) -> None:
        """Web API: master switch pro cely planovac."""
        self.global_enabled = enabled
        log.info(f"Scheduler global_enabled = {enabled}")

    def get_rules_status(self) -> List[dict]:
        """Pro UI: serializovany seznam pravidel."""
        out = []
        for r in self.rules:
            day_labels = [k for k, v in DAYS_MAP.items() if v in r.days and len(k) == 2 and k[0].isupper()][:7]
            day_labels_clean = []
            seen = set()
            for d in day_labels:
                if DAYS_MAP[d] not in seen:
                    seen.add(DAYS_MAP[d])
                    day_labels_clean.append(d)
            out.append({
                "name": r.name,
                "enabled": r.enabled,
                "days": sorted(r.days),
                "day_labels": day_labels_clean,
                "time": r.time_str,
                "scene": r.scene,
                "target_temp_c": r.target_temp_c,
                "last_triggered": r.last_triggered,
            })
        return out

    def get_next_trigger(self) -> Optional[dict]:
        """Vrati info o pristim plánovaném triggeru (pro UI)."""
        now = datetime.now()
        candidates = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            try:
                hh, mm = rule.time_str.split(":")
                rh, rm = int(hh), int(mm)
            except Exception:
                continue
            # Zkus 8 dni dopredu, najdi nejblizsi den+cas
            for offset_days in range(8):
                from datetime import timedelta
                check_day = now + timedelta(days=offset_days)
                check_dow = check_day.weekday()
                if check_dow in rule.days:
                    check_dt = check_day.replace(
                        hour=rh, minute=rm, second=0, microsecond=0)
                    if check_dt > now:
                        candidates.append((check_dt, rule))
                        break
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        next_dt, next_rule = candidates[0]
        return {
            "name": next_rule.name,
            "scene": next_rule.scene,
            "time": next_dt.strftime("%Y-%m-%d %H:%M"),
            "in_minutes": int((next_dt - now).total_seconds() / 60),
        }

    async def _check_and_trigger(self) -> None:
        if not self.global_enabled:
            return
        if self.ctx.override_active:
            # Pokud je manual override (Heat now), neper se schedule
            log.debug("Schedule skipped: override_active")
            return

        now = datetime.now()
        for rule in self.rules:
            if rule.matches_now(now):
                log.info(
                    f"Schedule TRIGGER: '{rule.name}' "
                    f"(day={now.strftime('%a')} {now.strftime('%H:%M')}) "
                    f"-> scene={rule.scene}"
                )
                try:
                    success = await self.scene_callback(rule.scene, rule.target_temp_c)
                    rule.last_triggered = time.time()
                    if success:
                        log.info(f"Schedule '{rule.name}' executed OK")
                    else:
                        log.warning(f"Schedule '{rule.name}' callback returned False")
                except Exception as e:
                    log.error(f"Schedule '{rule.name}' callback error: {e}")
                # Jen 1 trigger per tick - aby se predeslo race conditions
                break

    async def _loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self._check_and_trigger()
            except Exception as e:
                log.exception(f"scheduler loop error: {e}")
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self.check_interval)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        log.info(
            f"Scheduler starting: {len(self.rules)} rules, "
            f"global_enabled={self.global_enabled}, check {self.check_interval}s"
        )
        for r in self.rules:
            log.info(f"  - {r.name}: days={r.days} time={r.time_str} scene={r.scene} {'ON' if r.enabled else 'OFF'}")
        asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._shutdown.set()


def parse_rules_from_config(rules_cfg: list) -> List[ScheduleRule]:
    """Z YAML configu vyrobi ScheduleRule objekty."""
    rules = []
    for r in rules_cfg or []:
        days_raw = r.get("days", [])
        days_int = []
        for d in days_raw:
            if isinstance(d, int):
                days_int.append(d)
            elif isinstance(d, str) and d in DAYS_MAP:
                days_int.append(DAYS_MAP[d])
        rule = ScheduleRule(
            name=r.get("name", "?"),
            enabled=r.get("enabled", True),
            days=sorted(set(days_int)),
            time_str=r.get("time", "00:00"),
            scene=r.get("scene", "solar_auto"),
            target_temp_c=r.get("target_temp_c"),
        )
        rules.append(rule)
    return rules
