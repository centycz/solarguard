"""
Heatpump rozhodovaci engine - kdy SolarGuard automaticky zasahuje.

Logika analogicka virivce, ale pro tepelne cerpadlo (TC).

Stavy auto-rizeni:
- IDLE          - nezasahuju, ponechavam default nastaveni
- SOLAR_BOOST   - prebytek + sluneci den -> zvedam TUV target, blokuju dohrev
- COOLING       - leto, vysoke teploty venku, dostatek prebytku -> chlazeni
- NIGHT_SAVING  - noc -> blokuju elektricky dohrev (jen kompresor)
- SURVIVE       - SURVIVE strategie -> minimalizuju spotrebu, vse OFF
- MANUAL        - uzivatel zapnul manualni override

Bezpecne defaulty:
- Nezvedam TUV nad 60°C (rizko nebo neefektivni)
- Nezvedam room target nad 23°C (uspory vs komfort)
- Nedavam pumpu OFF nikdy automaticky (riskantni)
- Cooling jen kdy outdoor > 24°C (jinak nesmysl)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..state import SystemContext, DayStrategy

log = logging.getLogger("hp_engine")


class HeatPumpState(Enum):
    DISABLED = "disabled"          # heat pump rizeni vypnuto v configu
    IDLE = "idle"                  # nezasahuju
    SOLAR_BOOST = "solar_boost"    # vyuzivam slunecni den
    COOLING = "cooling"            # letni rezim
    NIGHT_SAVING = "night_saving"  # blokace dohrevu v noci
    SURVIVE = "survive"            # extremni setreni
    MANUAL = "manual"              # uzivatelsky override
    ALARM = "alarm"                # cerpadlo hlasi alarm


@dataclass
class HeatPumpConfig:
    enabled: bool = False

    # Solar boost - kdy zacit vyuzivat prebytek na vyssi teploty
    boost_min_surplus_w: float = 2000   # min prebytek pro boost
    boost_min_soc_pct: float = 80       # min SOC pro boost
    boost_min_lux: float = 30000        # potreba slunce
    boost_outdoor_max_c: float = 18     # nad to nema smysl topit
    boost_hold_time_min: int = 30       # po kolika minutach lze prejit zpet (anti-flicker)

    # Cooling - letni rezim
    cooling_enabled: bool = False       # default vypnuto, manualne zapnout
    cooling_min_outdoor_c: float = 24
    cooling_min_soc_pct: float = 70
    cooling_min_lux: float = 40000

    # Night saving
    night_saving_enabled: bool = True
    night_block_aux_heater: bool = True  # blokuj el. dohrev v noci

    # Bezpecnostni limity
    max_hot_water_target_c: float = 60
    max_room_target_c: float = 23
    min_room_target_c: float = 18


@dataclass
class HeatPumpDecision:
    target_state: HeatPumpState
    actions: list  # list of (method_name, args) tuples
    reason: str = ""


class HeatPumpEngine:
    def __init__(self, config: HeatPumpConfig):
        self.cfg = config
        self.current_state = HeatPumpState.IDLE if config.enabled else HeatPumpState.DISABLED
        self.state_entered_at = time.time()

    def _time_in_state(self) -> float:
        return time.time() - self.state_entered_at

    def decide(self, ctx: SystemContext) -> HeatPumpDecision:
        """Hlavni rozhodovani - co delat s tepelnym cerpadlem."""
        if not self.cfg.enabled:
            return HeatPumpDecision(HeatPumpState.DISABLED, [], "modul vypnut")

        hp = ctx.heatpump
        v = ctx.victron
        env = ctx.env
        plan = ctx.plan

        # Manualni override - nic neudelej
        if hp.manual_override:
            return HeatPumpDecision(
                HeatPumpState.MANUAL, [],
                f"manualni override: {hp.manual_override_reason}",
            )

        # Stale data nebo offline - nic
        if hp.is_stale or not hp.online:
            return HeatPumpDecision(
                HeatPumpState.IDLE, [],
                "TC offline/stale - nezasahuji",
            )

        # Alarm - jen log, nezasahuji
        if hp.has_alarm:
            return HeatPumpDecision(
                HeatPumpState.ALARM, [],
                f"TC ma alarm {hp.alarm_code}",
            )

        # SURVIVE strategie - minimalni rezim
        if plan.strategy == DayStrategy.SURVIVE:
            actions = [("disable_solar_boost", ())]
            if self.cfg.night_block_aux_heater:
                actions.append(("block_additional_heater", (True,)))
            return HeatPumpDecision(
                HeatPumpState.SURVIVE, actions,
                "SURVIVE - blokuji dohrev, vracim defaulty",
            )

        # Slunce sviti, baterka plna - SOLAR_BOOST
        sun_strong = (env.light_lux is not None
                      and env.light_lux > self.cfg.boost_min_lux
                      and not env.is_stale)
        battery_high = v.soc_pct is not None and v.soc_pct >= self.cfg.boost_min_soc_pct
        outdoor_cool = (env.air_temp_c is not None
                        and env.air_temp_c < self.cfg.boost_outdoor_max_c)
        big_surplus = (v.surplus_w is not None
                       and v.surplus_w > self.cfg.boost_min_surplus_w)

        if sun_strong and battery_high and outdoor_cool and (big_surplus or v.soc_pct >= 95):
            return HeatPumpDecision(
                HeatPumpState.SOLAR_BOOST,
                [("enable_solar_boost", ())],
                f"slunce + baterka {v.soc_pct:.0f}% + venku {env.air_temp_c:.0f}°C - boost TUV",
            )

        # Letni cooling
        if self.cfg.cooling_enabled \
                and env.air_temp_c is not None \
                and env.air_temp_c > self.cfg.cooling_min_outdoor_c \
                and battery_high and sun_strong:
            return HeatPumpDecision(
                HeatPumpState.COOLING,
                [("enable_cooling", ())],
                f"venku {env.air_temp_c:.0f}°C - chladim",
            )

        # Noc - blokace dohrevu
        is_night = env.light_lux is not None and env.light_lux < 1000
        if is_night and self.cfg.night_saving_enabled:
            return HeatPumpDecision(
                HeatPumpState.NIGHT_SAVING,
                [
                    ("disable_solar_boost", ()),
                    ("block_additional_heater", (self.cfg.night_block_aux_heater,)),
                ],
                "noc - blokuji elektricky dohrev",
            )

        # Default - vrat se na cisty stav
        return HeatPumpDecision(
            HeatPumpState.IDLE,
            [("disable_solar_boost", ())],
            "default - bez zasahu",
        )

    def transition(self, new_state: HeatPumpState) -> None:
        if new_state != self.current_state:
            log.info(f"HP state: {self.current_state.value} -> {new_state.value}")
            self.current_state = new_state
            self.state_entered_at = time.time()
