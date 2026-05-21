"""
Cisticí programy viřivky - timer + auto-stop.

Pouziti:
  cm = CleaningManager(ctx, spa_controller)
  await cm.start_program(hours=3)     # zapne filter + sanitizer + spusti timer
  ...
  await cm.tick()                      # v hlavnim loopu - odpocitava, auto-vypne
  ...
  await cm.stop_program()              # predcasne ukonceni

Priority:
- Cleaning program ma PRIORITU nad solar auto rozhodovanim pro filter/sanitizer.
- Engine nemuze vypnout filter/sanitizer pokud bezi cleaning.
- Cleaning nezasahuje do heateru - ten se muze nezavisle zapinat/vypinat
  podle SOC, prebytku atd.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..state import SystemContext, CleaningState, CleaningProgram

log = logging.getLogger("cleaning")


class CleaningManager:
    def __init__(self, context: SystemContext, spa_controller,
                 event_logger=None, web_record_event=None):
        """
        Parametry:
          context        - SystemContext (obsahuje .cleaning a .spa)
          spa_controller - SpaController
          event_logger   - volitelne, EventLogger.log(type, **kwargs)
          web_record_event - volitelne, record_event(type, **kwargs) z web modulu
        """
        self.ctx = context
        self.spa = spa_controller
        self.event_logger = event_logger
        self.web_record_event = web_record_event

    def _log_event(self, event_type: str, **fields):
        if self.event_logger:
            try: self.event_logger.log(event_type, **fields)
            except Exception: pass
        if self.web_record_event:
            try: self.web_record_event(event_type, **fields)
            except Exception: pass

    async def start_program(self, hours: float) -> dict:
        """Spusti cleaning program na X hodin.

        Zapne filter + sanitizer, ulozi puvodni stav (aby se dal po skonceni vratit).

        Vraci dict {'ok': bool, 'message': str}.
        """
        if hours not in (3, 5, 8) and not (0.1 <= hours <= 12):
            return {"ok": False, "message": f"nepodporovana delka: {hours}h"}

        cp = self.ctx.cleaning
        if cp.is_running:
            return {"ok": False, "message": "cleaning program jiz bezi, nejdriv stop"}

        if not self.ctx.spa.online:
            return {"ok": False, "message": "virivka je offline"}

        log.info(f"Starting cleaning program: {hours}h")

        # Uloz puvodni stav, abychom mohli po dobehnuti vratit
        cp.filter_was_on = self.ctx.spa.filter_on
        cp.sanitizer_was_on = self.ctx.spa.sanitizer_on

        # Zapni filter (pokud uz nebezi)
        filter_ok = True
        if not self.ctx.spa.filter_on:
            filter_ok = await self.spa.set_filter(True, force=True)
            if not filter_ok:
                log.error("cleaning start: filter ON failed")
                cp.errors += 1

        # Zapni sanitizer (pokud uz nebezi)
        sanitizer_ok = True
        if not self.ctx.spa.sanitizer_on:
            sanitizer_ok = await self.spa.set_sanitizer(True, force=True)
            if not sanitizer_ok:
                log.error("cleaning start: sanitizer ON failed")
                cp.errors += 1

        # Spust timer - i kdyz pripadne nejaka komponenta selhala, user muze
        # videt stav a stopnout rucne. Nicmene hlasime partial.
        cp.state = CleaningState.RUNNING
        cp.started_at = time.time()
        cp.duration_hours = float(hours)
        cp.last_tick = time.time()
        cp.errors = 0 if (filter_ok and sanitizer_ok) else cp.errors

        msg = f"cleaning program {hours}h spusten"
        if not filter_ok or not sanitizer_ok:
            msg += " (ale nejaky komponent selhal - zkontroluj stav)"

        self._log_event("cleaning_start",
                        hours=hours, filter_ok=filter_ok, sanitizer_ok=sanitizer_ok)
        log.info(msg)
        return {"ok": True, "message": msg,
                "filter_ok": filter_ok, "sanitizer_ok": sanitizer_ok}

    async def stop_program(self, reason: str = "manual") -> dict:
        """Predcasne ukonci cleaning program.

        Vypne sanitizer. Filter necha v puvodnim stavu (pokud byl zaply pred
        programem, zustane zaply - solar auto engine si s tim pak poradi).
        """
        cp = self.ctx.cleaning
        if not cp.is_running:
            return {"ok": False, "message": "zadny cleaning program nebezi"}

        elapsed_h = cp.elapsed_sec / 3600.0
        log.info(f"Stopping cleaning program (elapsed {elapsed_h:.2f}h, reason={reason})")

        sanitizer_ok = True
        # Vypni sanitizer pokud bezi a puvodne nebezel (nebo pokud nevime, radeji ho vypnem)
        if self.ctx.spa.sanitizer_on and (cp.sanitizer_was_on is False or cp.sanitizer_was_on is None):
            sanitizer_ok = await self.spa.set_sanitizer(False, force=True)
            if not sanitizer_ok:
                log.error("cleaning stop: sanitizer OFF failed")

        # Filter NEvypinat tady - at si to solar engine vyresi podle topeni/strategie
        # Vypneme jen pokud puvodne NEbezel a zapli jsme ho jen kvuli cleaning
        filter_ok = True
        if cp.filter_was_on is False and self.ctx.spa.filter_on:
            # jen pokud zrovna netopi (ceho se nedotykame)
            if not self.ctx.spa.heater_on:
                filter_ok = await self.spa.set_filter(False, force=True)

        # Reset programu
        cp.state = CleaningState.IDLE
        cp.started_at = 0.0
        cp.duration_hours = 0.0
        cp.filter_was_on = None
        cp.sanitizer_was_on = None

        self._log_event("cleaning_stop",
                        reason=reason, elapsed_hours=round(elapsed_h, 2),
                        sanitizer_ok=sanitizer_ok, filter_ok=filter_ok)
        return {"ok": True, "message": f"cleaning zastaven po {elapsed_h:.2f}h"}

    async def tick(self):
        """Vola se periodicky z hlavni loopu. Auto-vypne po dobehnuti timeru."""
        cp = self.ctx.cleaning
        if not cp.is_running:
            return

        cp.last_tick = time.time()

        # Bezpecnostni check - pokud se sanitizer nejak sam vypne,
        # zkusime ho znova zapnout (max 2x)
        if self.ctx.spa.online and self.ctx.spa.sanitizer_on is False and cp.errors < 2:
            log.warning("cleaning: sanitizer sam vypnut, zkousim znova zapnout")
            ok = await self.spa.set_sanitizer(True, force=True)
            if not ok:
                cp.errors += 1

        # Dobehnutí timeru -> auto stop
        if cp.remaining_sec <= 0:
            log.info(f"cleaning program {cp.duration_hours}h dobehl - auto stop")
            await self.stop_program(reason="timer_completed")
