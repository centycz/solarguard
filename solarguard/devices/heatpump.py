"""
Tepelne cerpadlo IVT AIR X 70 + Airmodul E9 - controller s logikou rizeni.

Podobne jako SpaController pro virivku, jen pro tepelne cerpadlo.

Klicove operace:
- set_target_hot_water_temp(temp) - boost TUV pri prebytku
- set_target_room_temp(temp)      - zvedat/snizovat target topeni
- set_operating_mode(mode)        - heat/cool/hot_water/off
- block_additional_heater(bool)   - blokace elektrickeho dohrevu (drahy!)
- enable_solar_boost()            - pri prebytku zvedni teplotu o X°C
- disable_solar_boost()           - vrat se na config target

Logika rizeni (analogicka virivce):
- Pri velkem prebytku + slunci: zvyseni target hot_water (boost)
- Pri SOC plne + slunci: zapnuti chlazeni (klimatizace) v lete
- V noci: blokace dohrevu (jen kompresor) a vraceni na default
- Pri SURVIVE: vsechno OFF
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ..state import SystemContext

log = logging.getLogger("heatpump")


class HeatPumpController:
    """High-level controller s pristupem k H66 source pro fyzicke ovladani."""

    def __init__(
        self,
        h66_source,                                # H66MqttSource instance
        context: SystemContext,
        default_hot_water_target_c: float = 50.0,
        boost_hot_water_target_c: float = 60.0,    # boost pri velkem prebytku
        default_room_target_c: float = 21.0,
        boost_room_target_c: float = 22.5,         # zvyseni pri prebytku
        cooling_room_target_c: float = 24.0,       # cilova v lete
        dry_run: bool = True,
    ):
        self.h66 = h66_source
        self.ctx = context
        self.default_hw = default_hot_water_target_c
        self.boost_hw = boost_hot_water_target_c
        self.default_room = default_room_target_c
        self.boost_room = boost_room_target_c
        self.cooling_room = cooling_room_target_c
        self.dry_run = dry_run

        # Sledovani co jsme nastavili (abychom necalibrovali stale dokola)
        self._last_set_hw: Optional[float] = None
        self._last_set_room: Optional[float] = None
        self._last_set_mode: Optional[str] = None
        self._last_set_block_aux: Optional[bool] = None
        self._lock = asyncio.Lock()

    # ───── Manualni operace (UI) ─────

    async def set_target_hot_water_temp(self, temp_c: float, force: bool = False) -> bool:
        """Nastavi cilovou teplotu TUV. force=True obchazi dry_run."""
        if self.dry_run and not force:
            log.info(f"[DRY RUN] set_target_hot_water_temp({temp_c}C)")
            return True
        async with self._lock:
            ok = await self.h66.set_register("target_hot_water", int(temp_c))
            if ok:
                self._last_set_hw = temp_c
                log.info(f"HotWater target -> {temp_c}C")
            return ok

    async def set_target_room_temp(self, temp_c: float, force: bool = False) -> bool:
        if self.dry_run and not force:
            log.info(f"[DRY RUN] set_target_room_temp({temp_c}C)")
            return True
        async with self._lock:
            ok = await self.h66.set_register("room_setpoint", round(temp_c, 1))
            if ok:
                self._last_set_room = temp_c
                log.info(f"Room target -> {temp_c}C")
            return ok

    async def set_operating_mode(self, mode: str, force: bool = False) -> bool:
        """mode: 'heat' | 'cool' | 'hot_water' | 'off'

        H66 muze ocekavat ciselny kod nebo text - to zalezi na pumpe.
        Mapping je v configu pod heatpump.mode_codes.
        """
        if self.dry_run and not force:
            log.info(f"[DRY RUN] set_operating_mode({mode})")
            return True
        async with self._lock:
            ok = await self.h66.set_register("operating_mode", mode)
            if ok:
                self._last_set_mode = mode
                log.info(f"Operating mode -> {mode}")
            return ok

    async def block_additional_heater(self, blocked: bool, force: bool = False) -> bool:
        """Blokuje/povoli elektricky dohrev. Drahy - radeji blokovat kdyz nejde."""
        if self.dry_run and not force:
            log.info(f"[DRY RUN] block_additional_heater({blocked})")
            return True
        async with self._lock:
            ok = await self.h66.set_register("block_add_heater", 1 if blocked else 0)
            if ok:
                self._last_set_block_aux = blocked
                log.info(f"Additional heater blocked = {blocked}")
            return ok

    # ───── Vyssi-urovenove operace (logika) ─────

    async def enable_solar_boost(self) -> None:
        """Zvedne TUV target a room target, vyuzij prebytek FVE.

        Volej kdyz: SOC vysoke, FVE prebytek velky, sluneci den.
        """
        log.info("Solar boost ENABLE - vyuzivam prebytek FVE")
        await self.set_target_hot_water_temp(self.boost_hw)
        await self.set_target_room_temp(self.boost_room)
        # Pri solar boostu blokujeme dohrev - ma byt z FVE, ne ze site
        await self.block_additional_heater(True)

    async def disable_solar_boost(self) -> None:
        """Vrat targety na default (vecer / kdyz prebytek konci)."""
        log.info("Solar boost DISABLE - zpet na default")
        await self.set_target_hot_water_temp(self.default_hw)
        await self.set_target_room_temp(self.default_room)
        await self.block_additional_heater(False)

    async def enable_cooling(self) -> None:
        """Letni rezim - chlazeni domu pri prebytku."""
        log.info("Cooling mode ENABLE")
        await self.set_operating_mode("cool")
        await self.set_target_room_temp(self.cooling_room)
        # Dohrev blokovat (chladime, ne topime)
        await self.block_additional_heater(True)

    async def is_safe_to_operate(self) -> bool:
        """Sanity check - nemame alarm, je online, atd."""
        hp = self.ctx.heatpump
        if hp.is_stale:
            log.warning("HP is stale - skip command")
            return False
        if hp.has_alarm:
            log.warning(f"HP has alarm {hp.alarm_code} - skip command")
            return False
        return True

    # ───── Helpers ─────

    def get_status_summary(self) -> dict:
        """Vrati souhrnny stav pro UI / API."""
        hp = self.ctx.heatpump
        return {
            "online": hp.online and not hp.is_stale,
            "operating_mode": hp.operating_mode,
            "compressor_running": hp.compressor_running,
            "add_heater_active": hp.additional_heater_active,
            "add_heater_blocked": hp.additional_heater_blocked,
            "outdoor_temp": hp.outdoor_temp_c,
            "indoor_temp": hp.indoor_temp_c,
            "supply_temp": hp.supply_temp_c,
            "return_temp": hp.return_temp_c,
            "hot_water_temp": hp.hot_water_temp_c,
            "target_room_temp": hp.target_room_temp_c,
            "target_hot_water": hp.target_hot_water_temp_c,
            "power_w": hp.power_consumption_w,
            "energy_today_kwh": hp.energy_today_kwh,
            "cop": hp.cop_estimated,
            "alarm_active": hp.alarm_active,
            "alarm_code": hp.alarm_code,
            "manual_override": hp.manual_override,
            "manual_override_reason": hp.manual_override_reason,
            "stale": hp.is_stale,
        }
