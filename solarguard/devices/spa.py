"""Intex PureSpa 28462 ovladani - s podporou sanitizer."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("spa")

try:
    from aio_intex_spa import IntexSpa
    INTEX_AVAILABLE = True
except ImportError:
    try:
        from intex_spa import IntexSpa
        INTEX_AVAILABLE = True
    except ImportError:
        log.warning("aio_intex_spa not installed")
        INTEX_AVAILABLE = False
        IntexSpa = None


class SpaController:
    def __init__(self, host, context, retry_count=3, verify_delay_sec=5, dry_run=False):
        self.host = host
        self.ctx = context
        self.retry_count = retry_count
        self.verify_delay_sec = verify_delay_sec
        self.dry_run = dry_run
        self._spa = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        if not INTEX_AVAILABLE: return False
        try:
            self._spa = IntexSpa(address=self.host)
            ok = await self.refresh_status()
            if ok: log.info(f"Spa connected at {self.host}")
            return ok
        except Exception as e:
            log.error(f"Spa connect failed: {e}")
            return False

    async def refresh_status(self) -> bool:
        if self._spa is None: return False
        try:
            await asyncio.wait_for(self._spa.async_update_status(), timeout=10)
            st = self._spa.status
            self.ctx.spa.current_temp_c = getattr(st, "current_temp", None)
            self.ctx.spa.target_temp_c = getattr(st, "preset_temp", None)
            self.ctx.spa.heater_on = getattr(st, "heater", None)
            self.ctx.spa.filter_on = getattr(st, "filter", None)
            self.ctx.spa.bubbles_on = getattr(st, "bubbles", None)
            self.ctx.spa.jets_on = getattr(st, "jets", None)
            self.ctx.spa.sanitizer_on = getattr(st, "sanitizer", None)
            self.ctx.spa.power_on = getattr(st, "power", None)
            self.ctx.spa.error_code = getattr(st, "error_code", None)
            self.ctx.spa.online = True
            self.ctx.spa.last_update = time.time()
            self.ctx.spa.consecutive_failures = 0
            if self.ctx.spa.error_code:
                log.warning(f"Spa error_code={self.ctx.spa.error_code}")
            return True
        except asyncio.TimeoutError:
            log.warning("Spa status refresh TIMEOUT")
            self.ctx.spa.consecutive_failures += 1
        except Exception as e:
            # v4.1.1: JSONDecodeError je častý transient (vířivka občas vrátí prázdný response)
            # Logujeme jako DEBUG aby logy nebyly zaplaveny
            err_str = str(e)
            if "Expecting value" in err_str or "JSONDecodeError" in err_str:
                log.debug(f"Spa transient JSON error: {e}")
            else:
                log.warning(f"Spa status refresh failed: {e}")
            self.ctx.spa.consecutive_failures += 1
        if self.ctx.spa.consecutive_failures >= 5:
            self.ctx.spa.online = False
        return False

    async def _set_and_verify(self, name, setter_factory, target, getter, force=False):
        """Force=True obchazi dry_run - pouziva se pro web commands a cleaning.

        v3.4 FIX: Robustnejsi verify - po commandu udelame az 3 refresh checks
        s odstupem (2s, 4s, 6s). Mezi pokusy se Intex API muze stihnut updatovat.
        Pokud ale rovnou pri prvnim refresh vidime spravny stav, je to OK.

        Take: pokud po commandu sice nedosli odpovedi, ale pak pri verify zjistime
        ze stav je spravny, povazujeme to za uspech (Intex API obcas ma timeout
        ale fyzicky to provedlo).
        """
        if self.dry_run and not force:
            log.info(f"[DRY RUN] {name}({target})")
            return True
        async with self._lock:
            for attempt in range(1, self.retry_count + 1):
                try:
                    log.info(f"{name}({target}) attempt {attempt}")
                    await asyncio.wait_for(setter_factory(), timeout=10)
                except asyncio.TimeoutError:
                    log.warning(f"{name} TIMEOUT (ale fyzicky se to mohlo provest)")
                except Exception as e:
                    log.warning(f"{name} exception: {e} (ale fyzicky se to mohlo provest)")

                # v3.4 FIX: multi-step verify - dej Intex casu na update stavu
                # zkusime 3x refresh s ruznym odstupem (2s, +2s, +2s = celkem 6s)
                for verify_attempt in range(3):
                    await asyncio.sleep(2 if verify_attempt == 0 else self.verify_delay_sec / 3)
                    await self.refresh_status()
                    actual = getter()
                    if actual == target:
                        log.info(f"{name} verified: {target} (verify_attempt {verify_attempt+1})")
                        return True

                log.warning(f"{name} attempt {attempt}: mismatch (wanted {target}, got {getter()})")

            # Posledni nadeje: jeste jeden refresh + check (mezi tim mohl stav dorazit)
            await asyncio.sleep(2)
            await self.refresh_status()
            if getter() == target:
                log.info(f"{name} verified at final check: {target}")
                return True

            log.error(f"{name}({target}) failed after {self.retry_count} attempts (final state: {getter()})")
            return False

    async def set_heater(self, on: bool, force: bool = False) -> bool:
        return await self._set_and_verify(
            "set_heater",
            lambda: self._spa.async_set_heater(on),
            on, lambda: self.ctx.spa.heater_on, force=force,
        )

    async def set_filter(self, on: bool, force: bool = False) -> bool:
        return await self._set_and_verify(
            "set_filter",
            lambda: self._spa.async_set_filter(on),
            on, lambda: self.ctx.spa.filter_on, force=force,
        )

    async def set_bubbles(self, on: bool) -> bool:
        return await self._set_and_verify(
            "set_bubbles",
            lambda: self._spa.async_set_bubbles(on),
            on, lambda: self.ctx.spa.bubbles_on, force=True,
        )

    async def set_jets(self, on: bool) -> bool:
        return await self._set_and_verify(
            "set_jets",
            lambda: self._spa.async_set_jets(on),
            on, lambda: self.ctx.spa.jets_on, force=True,
        )

    async def set_sanitizer(self, on: bool, force: bool = True) -> bool:
        """Zapnout/vypnout sanitizer (UVC cistic).

        Pouziva se hlavne z cleaning programu. force=True je default, aby to
        fungovalo i v dry_run rezimu (cleaning program by jinak nemel smysl).
        """
        return await self._set_and_verify(
            "set_sanitizer",
            lambda: self._spa.async_set_sanitizer(on),
            on, lambda: self.ctx.spa.sanitizer_on, force=force,
        )

    async def set_target_temp(self, temp_c: int) -> bool:
        temp_c = int(temp_c)
        if self.ctx.spa.target_temp_c == temp_c:
            return True
        return await self._set_and_verify(
            "set_preset_temp",
            lambda: self._spa.async_set_preset_temp(temp_c),
            temp_c, lambda: self.ctx.spa.target_temp_c, force=True,
        )
