"""
v3.7 NEW: Pre-shower mode - automaticka priprava virivky pred koupanim.

Uzivatel klepne v UI "Priprav virivku za 30 min" (nebo ke konkretnimu casu).
Server spocita kdy zapnout topeni podle heating_curve a sekvenci provede:

  T-X min:  filtrace ON, override aktivni, topi do cilove teploty
  T-5 min:  bublinky ON na 3 min (uvolneni napeti / mix vody)
  T-2 min:  bublinky OFF (klid pred vstupem)
  T-0:      zustane heat_now scena, status "ready"

Cancellable kdykoliv (klik "Zrusit pripravu").
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

log = logging.getLogger("preshower")


class PreShowerState(Enum):
    IDLE = "idle"
    WARMING = "warming"          # topi, ceka az se ohreje
    BUBBLES_ON = "bubbles_on"    # T-5: bublinky beziy
    BUBBLES_OFF = "bubbles_off"  # T-2: bublinky vypnuty, klid
    READY = "ready"              # T-0: vse ok, vyhrato
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class PreShowerProgram:
    """Stav probehajici/dokonene/zruseny prepare-program."""
    state: PreShowerState = PreShowerState.IDLE
    target_time: Optional[float] = None     # epoch sec - kdy ma byt ready
    target_temp: float = 38.0
    started_at: Optional[float] = None
    bubbles_started_at: Optional[float] = None
    failed_reason: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in (
            PreShowerState.WARMING,
            PreShowerState.BUBBLES_ON,
            PreShowerState.BUBBLES_OFF,
            PreShowerState.READY,
        )

    @property
    def time_remaining_sec(self) -> Optional[float]:
        if self.target_time is None:
            return None
        return max(0, self.target_time - time.time())

    @property
    def progress_pct(self) -> float:
        if self.target_time is None or self.started_at is None:
            return 0.0
        total = self.target_time - self.started_at
        elapsed = time.time() - self.started_at
        if total <= 0:
            return 100.0
        return min(100.0, (elapsed / total) * 100)


class PreShowerManager:
    """Async background task ktery rezi pre-shower sekvenci.

    Pouziti:
      mgr = PreShowerManager(ctx, spa, heating_curve, event_recorder)
      await mgr.start_program(target_temp=38, eta_minutes=30)
      ...
      await mgr.cancel()  # kdykoliv zrusit
    """

    BUBBLES_ON_BEFORE_SEC = 5 * 60   # T-5min: zapnout bublinky
    BUBBLES_OFF_BEFORE_SEC = 2 * 60  # T-2min: vypnout bublinky

    def __init__(
        self,
        context,
        spa_controller,
        heating_curve_tracker,
        event_logger=None,
        web_record_event=None,
    ):
        self.ctx = context
        self.spa = spa_controller
        self.curve = heating_curve_tracker
        self.events = event_logger
        self.web_record_event = web_record_event
        self._task: Optional[asyncio.Task] = None
        self._cancel_event = asyncio.Event()

    @property
    def program(self):
        return self.ctx.preshower

    @property
    def is_running(self) -> bool:
        return self.program.is_active

    async def start_program(
        self,
        target_temp: float = 38.0,
        eta_minutes: Optional[int] = None,
        eta_iso: Optional[str] = None,
    ) -> dict:
        """Spousti pre-shower program.

        Bud zadej eta_minutes (relativni) nebo eta_iso ('HH:MM' = absolutni dnes).
        """
        if self.is_running:
            return {"ok": False, "reason": "already_running"}
        if not self.ctx.spa.online:
            return {"ok": False, "reason": "spa_offline"}

        # Spocitat target_time
        now = time.time()
        if eta_iso:
            try:
                hh, mm = eta_iso.split(":")
                today = datetime.now().replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0
                )
                if today.timestamp() <= now:
                    today = today + timedelta(days=1)  # pristi den
                target_time = today.timestamp()
            except Exception as e:
                return {"ok": False, "reason": f"bad_eta_iso: {e}"}
        elif eta_minutes is not None:
            # v3.7.1: rozsireno na 1000 min (16h) protoze velka virivka pri zime potrebuje
            if eta_minutes < 5 or eta_minutes > 1000:
                return {"ok": False, "reason": "eta_out_of_range (5-1000 min)"}
            target_time = now + eta_minutes * 60
        else:
            return {"ok": False, "reason": "missing eta"}

        time_until_target = target_time - now
        # Predikce z heating curve
        current_water = self.ctx.spa.current_temp_c
        air = self.ctx.env.air_temp_c
        wind = self.ctx.env.wind_kmh
        if current_water is None:
            return {"ok": False, "reason": "no_water_temp"}

        prediction = self.curve.predict_to_target(
            current_water, target_temp, air, wind
        )
        predicted_min = prediction["minutes"]
        margin_min = (time_until_target / 60.0) - predicted_min

        log.info(
            f"PreShower start: target={target_temp}°C @ "
            f"{datetime.fromtimestamp(target_time).strftime('%H:%M')} "
            f"(in {time_until_target/60:.0f} min). "
            f"Current {current_water}°C. "
            f"Predicted heating: {predicted_min:.0f} min. "
            f"Margin: {margin_min:+.0f} min"
        )

        # Pokud nemame dost casu, varuj ale spustime
        warning = None
        if margin_min < 0:
            warning = (
                f"Pozor: predikce ohrevu je {predicted_min:.0f} min, "
                f"do cilu zbyva jen {time_until_target/60:.0f} min. "
                f"Voda nemusi dosahnout {target_temp}°C."
            )
            log.warning(warning)

        # Setup state
        self.program.state = PreShowerState.WARMING
        self.program.target_time = target_time
        self.program.target_temp = target_temp
        self.program.started_at = now
        self.program.bubbles_started_at = None
        self.program.failed_reason = ""

        # Aktivuj heat_now-like override
        self.ctx.override_active = True
        self.ctx.override_reason = f"preshower: ready @ {datetime.fromtimestamp(target_time).strftime('%H:%M')}"
        self.ctx.current_scene = "heat_now"
        self.ctx.target_temp_override = int(target_temp)

        # Spustit topeni a filtraci
        try:
            await self.spa.set_filter(True, force=True)
            await self.spa.set_target_temp(int(target_temp))
            await self.spa.set_heater(True, force=True)
        except Exception as e:
            log.error(f"PreShower spa setup failed: {e}")
            self.program.state = PreShowerState.FAILED
            self.program.failed_reason = f"spa_setup: {e}"
            self.ctx.override_active = False
            self.ctx.override_reason = ""
            return {"ok": False, "reason": "spa_setup_failed"}

        if self.events:
            self.events.log("preshower_start",
                target_temp=target_temp,
                target_time_iso=datetime.fromtimestamp(target_time).isoformat(),
                eta_minutes=int(time_until_target / 60),
                predicted_heating_min=predicted_min,
                margin_min=margin_min,
                warning=warning,
            )
        if self.web_record_event:
            self.web_record_event("preshower_start",
                target_temp=target_temp,
                target_time_iso=datetime.fromtimestamp(target_time).isoformat(),
                eta_minutes=int(time_until_target / 60))

        # Spustit background task
        self._cancel_event.clear()
        self._task = asyncio.create_task(self._run_loop())

        return {
            "ok": True,
            "target_time_iso": datetime.fromtimestamp(target_time).strftime("%H:%M"),
            "predicted_heating_min": predicted_min,
            "margin_min": margin_min,
            "warning": warning,
        }

    async def cancel(self) -> dict:
        if not self.is_running:
            return {"ok": False, "reason": "not_running"}
        log.info("PreShower CANCELLED by user")
        self._cancel_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        await self._cleanup(state=PreShowerState.CANCELLED)
        return {"ok": True}

    async def _cleanup(self, state: PreShowerState) -> None:
        """Reset overrides a vypni topeni / bublinky."""
        self.program.state = state
        self.ctx.override_active = False
        self.ctx.override_reason = ""
        self.ctx.target_temp_override = None
        self.ctx.current_scene = "solar_auto"

        # Vypni topeni a bublinky (necht filtraci)
        try:
            await self.spa.set_heater(False, force=True)
        except Exception as e:
            log.warning(f"cleanup heater off failed: {e}")
        try:
            if self.ctx.spa.bubbles_on:
                await self.spa.set_bubbles(False)
        except Exception as e:
            log.warning(f"cleanup bubbles off failed: {e}")

        if self.events:
            self.events.log("preshower_end", state=state.value,
                reason=self.program.failed_reason)
        if self.web_record_event:
            self.web_record_event("preshower_end", state=state.value)

    async def _run_loop(self) -> None:
        """Hlavni smycka programu - sleduje cas a triggeruje fáze."""
        try:
            bubbles_on_done = False
            bubbles_off_done = False

            while not self._cancel_event.is_set():
                now = time.time()
                if self.program.target_time is None:
                    break
                remaining = self.program.target_time - now

                # Fail check: spa offline
                if not self.ctx.spa.online and self.ctx.spa.consecutive_failures >= 5:
                    log.error("PreShower FAILED: spa offline")
                    self.program.failed_reason = "spa_offline"
                    await self._cleanup(PreShowerState.FAILED)
                    return

                # Faze 1 (BUBBLES_ON) v T-5min
                if not bubbles_on_done and remaining <= self.BUBBLES_ON_BEFORE_SEC:
                    log.info("PreShower T-5min: bublinky ON")
                    self.program.state = PreShowerState.BUBBLES_ON
                    self.program.bubbles_started_at = now
                    try:
                        await self.spa.set_bubbles(True)
                    except Exception as e:
                        log.warning(f"bubbles ON failed: {e}")
                    bubbles_on_done = True

                # Faze 2 (BUBBLES_OFF) v T-2min
                if not bubbles_off_done and remaining <= self.BUBBLES_OFF_BEFORE_SEC:
                    log.info("PreShower T-2min: bublinky OFF (klid)")
                    self.program.state = PreShowerState.BUBBLES_OFF
                    try:
                        await self.spa.set_bubbles(False)
                    except Exception as e:
                        log.warning(f"bubbles OFF failed: {e}")
                    bubbles_off_done = True

                # Faze 3 (READY) v T-0
                if remaining <= 0:
                    log.info("PreShower READY: virivka pripravena!")
                    self.program.state = PreShowerState.READY
                    if self.events:
                        self.events.log("preshower_ready",
                            water_temp=self.ctx.spa.current_temp_c,
                            target=self.program.target_temp)
                    if self.web_record_event:
                        self.web_record_event("preshower_ready",
                            water_temp=self.ctx.spa.current_temp_c)
                    # Drz READY stav 5 min, pak ukoncit
                    await asyncio.sleep(min(300, 300))
                    # Po 5 minutach vrat do solar_auto, ale nech topit kdyby uzivatel pridal vodu
                    await self._cleanup(PreShowerState.IDLE)
                    return

                # Sleep do dalsiho check (kratsi pri blizkem cili)
                if remaining > 600:
                    sleep_sec = 60
                elif remaining > 60:
                    sleep_sec = 10
                else:
                    sleep_sec = 2

                try:
                    await asyncio.wait_for(
                        self._cancel_event.wait(), timeout=sleep_sec)
                except asyncio.TimeoutError:
                    pass

            # Pokud sem dorazil bez cancellation, asi error
            if self._cancel_event.is_set():
                log.info("PreShower run loop: cancellation requested")
        except asyncio.CancelledError:
            log.info("PreShower run loop cancelled")
        except Exception as e:
            log.exception(f"PreShower run loop error: {e}")
            self.program.failed_reason = str(e)
            await self._cleanup(PreShowerState.FAILED)
