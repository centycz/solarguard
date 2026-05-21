"""
SolarGuard v4.2.0 - Heat Pump engine test suite.

Testuje rozhodovaci logiku tepelneho cerpadla:
- SOLAR_BOOST scenare
- COOLING (letni rezim)
- NIGHT_SAVING
- SURVIVE
- MANUAL override
- ALARM handling

Spusteni:
    cd /home/pi/solarguard
    .venv/bin/python tests/test_heatpump.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solarguard.state import SystemContext, DayStrategy
from solarguard.engine.heatpump_engine import (
    HeatPumpEngine, HeatPumpConfig, HeatPumpState
)

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

results = []


def make_ctx(*, soc=85, surplus=2500, pv=3500,
             outdoor_c=10, light_lux=50000,
             strategy=DayStrategy.NORMAL,
             hp_online=True, hp_alarm=False,
             hp_manual=False):
    ctx = SystemContext()
    ctx.victron.soc_pct = soc
    ctx.victron.last_update = time.time()
    ctx.victron.pv_power_w = pv
    # vypocitat grid tak aby surplus odpovidal
    load_total = max(200, pv - surplus)
    ctx.victron.load_l1_w = load_total / 3
    ctx.victron.load_l2_w = load_total / 3
    ctx.victron.load_l3_w = load_total / 3
    ctx.victron.grid_l1_w = -surplus / 3
    ctx.victron.grid_l2_w = -surplus / 3
    ctx.victron.grid_l3_w = -surplus / 3

    ctx.env.air_temp_c = outdoor_c
    ctx.env.light_lux = light_lux
    ctx.env.last_update = time.time()

    ctx.plan.strategy = strategy

    # Heat pump
    ctx.heatpump.online = hp_online
    ctx.heatpump.last_update = time.time() if hp_online else 0
    ctx.heatpump.alarm_active = hp_alarm
    ctx.heatpump.manual_override = hp_manual
    if hp_manual:
        ctx.heatpump.manual_override_reason = "test override"

    return ctx


def check(name, ctx, expected_state, *, expected_actions_contain=None,
          expected_reason_contains=None, config=None):
    cfg = config or HeatPumpConfig(enabled=True)
    eng = HeatPumpEngine(cfg)
    decision = eng.decide(ctx)

    fails = []
    if decision.target_state != expected_state:
        fails.append(f"state: expected {expected_state.value}, got {decision.target_state.value}")
    if expected_actions_contain:
        action_names = [a[0] for a in decision.actions]
        for needed in expected_actions_contain:
            if needed not in action_names:
                fails.append(f"missing action '{needed}' in {action_names}")
    if expected_reason_contains and expected_reason_contains.lower() not in (decision.reason or "").lower():
        fails.append(f"reason missing '{expected_reason_contains}': '{decision.reason}'")

    passed = not fails
    results.append({"name": name, "passed": passed, "fails": fails, "decision": decision})
    color = GREEN if passed else RED
    icon = "✓" if passed else "✗"
    print(f"{color}{icon}{RESET} {name}")
    print(f"   {DIM}state={decision.target_state.value}, actions={[a[0] for a in decision.actions]}, reason: {decision.reason[:80]}{RESET}")
    if not passed:
        for f in fails:
            print(f"   {RED}{f}{RESET}")


# ─────────────────────────────────────────────────────────────────────────
# TESTY
# ─────────────────────────────────────────────────────────────────────────

print(f"\n{BOLD}{CYAN}═══ ZÁKLADNÍ SCÉNÁŘE ═══{RESET}")

# 1. Modul vypnuty -> DISABLED
ctx = make_ctx()
check("Heatpump enabled=false v configu -> DISABLED",
      ctx, HeatPumpState.DISABLED,
      config=HeatPumpConfig(enabled=False))

# 2. Slunce + plna baterka + venku chlad -> SOLAR_BOOST
ctx = make_ctx(soc=98, surplus=3000, pv=4500, outdoor_c=8, light_lux=55000)
check("Slunce + baterka 98% + chladno venku -> SOLAR_BOOST",
      ctx, HeatPumpState.SOLAR_BOOST,
      expected_actions_contain=["enable_solar_boost"],
      expected_reason_contains="boost")

# 3. Slunce + plna baterka + horko venku -> ne BOOST (nema smysl topit)
ctx = make_ctx(soc=98, surplus=3000, pv=4500, outdoor_c=22, light_lux=55000)
check("Slunce + baterka plna + venku 22°C -> NE boost (uz ne topit)",
      ctx, HeatPumpState.IDLE)

# 4. Cooling pri leto + slunci + dostatecny SOC
ctx = make_ctx(soc=85, surplus=3000, pv=5000, outdoor_c=27, light_lux=70000)
check("Leto 27°C + slunce + baterka 85% + cooling enabled -> COOLING",
      ctx, HeatPumpState.COOLING,
      expected_actions_contain=["enable_cooling"],
      expected_reason_contains="chladim",
      config=HeatPumpConfig(enabled=True, cooling_enabled=True))

# 5. Cooling vypnut v configu -> jen IDLE
ctx = make_ctx(soc=85, surplus=3000, pv=5000, outdoor_c=27, light_lux=70000)
check("Leto 27°C ale cooling_enabled=false -> IDLE (default)",
      ctx, HeatPumpState.IDLE,
      config=HeatPumpConfig(enabled=True, cooling_enabled=False))

# 6. Noc -> NIGHT_SAVING
ctx = make_ctx(soc=70, surplus=-200, pv=0, outdoor_c=5, light_lux=100)
check("Noc (light 100Lx) -> NIGHT_SAVING (blokuj dohrev)",
      ctx, HeatPumpState.NIGHT_SAVING,
      expected_actions_contain=["block_additional_heater"])

# 7. SURVIVE strategy
ctx = make_ctx(soc=40, surplus=200, pv=500, outdoor_c=8, strategy=DayStrategy.SURVIVE)
check("SURVIVE strategie -> blokuj dohrev, vrat defaulty",
      ctx, HeatPumpState.SURVIVE,
      expected_reason_contains="survive")

# 8. Manualni override
ctx = make_ctx(soc=98, surplus=3000, pv=4500, outdoor_c=8, light_lux=55000,
               hp_manual=True)
check("Manualni override aktivni -> MANUAL (nezasahuju)",
      ctx, HeatPumpState.MANUAL,
      expected_reason_contains="manualni")

# 9. Cerpadlo offline
ctx = make_ctx(hp_online=False)
check("Cerpadlo offline -> IDLE (nezasahuju)",
      ctx, HeatPumpState.IDLE,
      expected_reason_contains="offline")

# 10. Cerpadlo ma alarm
ctx = make_ctx(hp_alarm=True)
ctx.heatpump.alarm_code = "E15"
check("Cerpadlo ma alarm -> ALARM",
      ctx, HeatPumpState.ALARM,
      expected_reason_contains="alarm")

# 11. Slunce ale baterka jen 60% -> ne boost (cekame az se nabije)
ctx = make_ctx(soc=60, surplus=3000, pv=4500, outdoor_c=8, light_lux=55000)
check("Slunce ale baterka jen 60% (pod 80% threshold) -> IDLE",
      ctx, HeatPumpState.IDLE)

# 12. Maly prebytek + plna baterka -> stale BOOST (BAT-FULL kickstart logika)
ctx = make_ctx(soc=98, surplus=200, pv=600, outdoor_c=10, light_lux=55000)
check("BAT-FULL 98% + slunce + maly prebytek -> SOLAR_BOOST (kickstart)",
      ctx, HeatPumpState.SOLAR_BOOST)

# Souhrn
total = len(results)
passed = sum(1 for r in results if r["passed"])
failed = total - passed

print(f"\n{BOLD}{CYAN}═══════════════════════════════════════════════{RESET}")
print(f"{BOLD}Celkem: {total} | {GREEN}PASS: {passed}{RESET} | {RED}FAIL: {failed}{RESET}")

if failed > 0:
    print(f"\n{RED}{BOLD}❌ SELHALÉ TESTY:{RESET}")
    for r in results:
        if not r["passed"]:
            print(f"  {RED}✗ {r['name']}{RESET}")
            for f in r["fails"]:
                print(f"    {f}")
    sys.exit(1)
else:
    print(f"\n{GREEN}{BOLD}✓ Všechny heatpump testy prošly!{RESET}")
    sys.exit(0)
