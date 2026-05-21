"""
SolarGuard v4.1.8 - Appliance evaluator test suite.

Testuje fix bugu z v4.1.7 obrázku kdy:
- Pračka 2200W ukazovala "ANO" + bar 100% + "-60% kryto" (nesoudružné!)
- Trouba 3000W "ANO" když přebytek byl -120W
- Vše zelené i když realne by se vetsina vzala ze site

Spusteni:
    cd /home/pi/solarguard
    .venv/bin/python tests/test_appliances.py
"""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import Optional, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solarguard.state import SystemContext, SystemState, DayStrategy
from solarguard.engine.appliances import (
    ApplianceEvaluator, ApplianceProfile, ApplianceVerdict
)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

results = []


def make_ctx(
    *,
    soc=85, surplus=2000, pv=3000,
    load_l1=300, load_l2=300, load_l3=300,
    light_lux=50000,
    strategy=DayStrategy.NORMAL,
    history_sec=120,
):
    ctx = SystemContext()
    load_total = load_l1 + load_l2 + load_l3

    # Victron
    ctx.victron.soc_pct = soc
    ctx.victron.last_update = time.time()
    ctx.victron.pv_power_w = pv
    ctx.victron.load_l1_w = load_l1
    ctx.victron.load_l2_w = load_l2
    ctx.victron.load_l3_w = load_l3
    ctx.victron.battery_power_w = 0
    actual_surplus = pv - load_total
    grid_per_phase = -actual_surplus / 3
    ctx.victron.grid_l1_w = grid_per_phase
    ctx.victron.grid_l2_w = grid_per_phase
    ctx.victron.grid_l3_w = grid_per_phase

    # Env
    ctx.env.light_lux = light_lux
    ctx.env.last_update = time.time()
    ctx.env.air_temp_c = 18

    # Plan
    ctx.plan.strategy = strategy

    # History
    now = time.time()
    for i in range(history_sec):
        ctx.surplus_history.append((now - i, surplus))
    return ctx


def check(name, ctx, profile, *, expect_status=None, expect_covered_min=None,
          expect_covered_max=None, expect_message_contains=None,
          evaluator=None):
    ev = evaluator or ApplianceEvaluator(profiles=[profile])
    verdicts = ev.evaluate(ctx)
    v = verdicts[0]

    fails = []
    if expect_status and v.status != expect_status:
        fails.append(f"status: expected {expect_status}, got {v.status}")
    if expect_covered_min is not None and (v.covered_pct is None or v.covered_pct < expect_covered_min):
        fails.append(f"covered_pct: expected >= {expect_covered_min}, got {v.covered_pct}")
    if expect_covered_max is not None and (v.covered_pct is not None and v.covered_pct > expect_covered_max):
        fails.append(f"covered_pct: expected <= {expect_covered_max}, got {v.covered_pct}")
    if expect_message_contains and expect_message_contains.lower() not in (v.message or "").lower():
        fails.append(f"message missing '{expect_message_contains}': '{v.message}'")

    # KLICOVE: covered_pct nesmi byt zaporne nikdy (bug z v4.1.7)
    if v.covered_pct is not None and v.covered_pct < 0:
        fails.append(f"BUG: covered_pct ZAPORNE: {v.covered_pct}")
    if v.covered_pct is not None and v.covered_pct > 100:
        fails.append(f"BUG: covered_pct > 100: {v.covered_pct}")

    passed = not fails
    results.append({"name": name, "passed": passed, "fails": fails, "verdict": v})
    color = GREEN if passed else RED
    icon = "✓" if passed else "✗"
    print(f"{color}{icon}{RESET} {name}")
    print(f"   {DIM}status={v.status}, covered={v.covered_pct}%, msg: {v.message[:80]}{RESET}")
    if not passed:
        for f in fails:
            print(f"   {RED}{f}{RESET}")


# ─────────────────────────────────────────────────────────────────────────
# TESTY
# ─────────────────────────────────────────────────────────────────────────

print(f"\n{BOLD}{CYAN}═══ FIX BUGU Z v4.1.7 (covered_pct semantika) ═══{RESET}")

# Bug 1: Pračka, BAT-FULL, surplus zaporny -> nesmi byt -60% kryto
washer = ApplianceProfile(id="washer", name="Pračka", emoji="🧺",
                          peak_w=2200, avg_w=500, cycle_min=180, phase=1)
ctx = make_ctx(soc=98, surplus=-120, pv=580, light_lux=60000,
               load_l1=400, load_l2=200, load_l3=100,
               strategy=DayStrategy.AGGRESSIVE)
check("Pračka, BAT-FULL, PV 580W: covered_pct musí být 0-100%, ne zaporne",
      ctx, washer, expect_covered_min=0, expect_covered_max=100)

# Bug 2 + nove pravidlo v4.1.9: "slunecni den + plna baterka = JEĎ co chces"
# Trouba 3000W peak BAT-FULL + slunce -> GREEN (FVE naskoci jakmile zapne)
oven = ApplianceProfile(id="oven", name="Trouba", emoji="🔥",
                        peak_w=3000, avg_w=900, cycle_min=60, phase=1)
ctx = make_ctx(soc=98, surplus=-120, pv=580, light_lux=60000,
               load_l1=400, load_l2=200, load_l3=100)
check("Trouba BAT-FULL + silne slunce 60k Lx -> JEĎ (FVE naskoci)",
      ctx, oven, expect_status="green", expect_message_contains="slunce")

# Bug 3 (REVISED): I velka varna deska v slunci s plnou baterkou = JEĎ
# Argument: i kdyby FVE nestihla peak 5000W, baterka pomuze a hlavne kratky cyklus
hob = ApplianceProfile(id="hob", name="Varná deska", emoji="🍳",
                       peak_w=3500, avg_w=1800, cycle_min=30, phase=1)
ctx = make_ctx(soc=98, surplus=-120, pv=580, light_lux=60000,
               load_l1=400, load_l2=200, load_l3=100)
check("Varna deska BAT-FULL + silne slunce -> JEĎ (manzelce dava smysl)",
      ctx, hob, expect_status="green", expect_message_contains="slunce")

print(f"\n{BOLD}{CYAN}═══ NORMÁLNÍ SCÉNÁŘE ═══{RESET}")

# Test 4: Jasný GREEN - solidni prebytek + dostatecny SOC + slunce
ctx = make_ctx(soc=70, surplus=2000, pv=3500, light_lux=50000,
               load_l1=500, load_l2=500, load_l3=500)
check("Pračka, slunce + baterka 70% -> JEĎ",
      ctx, washer, expect_status="green")

# Test 5: AMBER - tma + neni BAT-FULL ale baterka pomuze
ctx = make_ctx(soc=65, surplus=200, pv=200, light_lux=5000,
               load_l1=200, load_l2=300, load_l3=200)
check("Pračka, vecer (5000Lx) + baterka 65% -> AMBER nebo RED",
      ctx, washer)  # nesmi byt green

# Test 6: RED kdyz SOC pod reservou
ctx = make_ctx(soc=20, surplus=3000, pv=4000, load_l1=300, load_l2=300, load_l3=300)
check("Pračka, SOC 20% pod reservou 25% -> RED",
      ctx, washer, expect_status="red", expect_message_contains="šetř")

# Test 7: SURVIVE strategy = vždy RED
ctx = make_ctx(soc=80, surplus=2500, pv=3500, strategy=DayStrategy.SURVIVE)
check("SURVIVE strategie -> vždy RED",
      ctx, washer, expect_status="red", expect_message_contains="zataženo")

# Test 7b: NOVE - Klicovy scenar z reality (z screenshotu uzivatele)
# 14:24, BAT-FULL 98%, FVE 469W (curtailed), slunce
# Pred v4.1.9: vsechny POZOR/RED -> manzelka nepochopi
# v4.1.9: vsechny GREEN
ctx = make_ctx(soc=98, surplus=-3, pv=469, light_lux=55000,
               load_l1=131, load_l2=312, load_l3=42)
ev_real = ApplianceEvaluator(profiles=[
    ApplianceProfile(id="washer", name="Pračka AEG", emoji="🧺", peak_w=2200, avg_w=200, cycle_min=180),
    ApplianceProfile(id="dryer", name="Sušička AEG", emoji="🌀", peak_w=800, avg_w=600, cycle_min=150),
    ApplianceProfile(id="oven", name="Trouba AEG", emoji="🔥", peak_w=3000, avg_w=900, cycle_min=60),
])
verdicts_real = ev_real.evaluate(ctx)
all_green = all(v.status == "green" for v in verdicts_real)
results.append({
    "name": "REAL SCENAR z screenshotu (BAT-FULL 98%, slunce 55k Lx, FVE curtailed) -> vse JEĎ",
    "passed": all_green,
    "fails": [] if all_green else [f"Nektery spotrebic neni green: {[(v.name, v.status) for v in verdicts_real]}"],
    "verdict": verdicts_real[0],
})
icon = "✓" if all_green else "✗"
color = GREEN if all_green else RED
print(f"{color}{icon}{RESET} REAL SCENAR z screenshotu uzivatele (slunce + BAT-FULL = vse JEĎ)")
for vr in verdicts_real:
    print(f"   {DIM}{vr.name}: {vr.status} - {vr.message}{RESET}")

print(f"\n{BOLD}{CYAN}═══ EDGE CASES (NaN, missing data) ═══{RESET}")

# Test 8: covered_pct musi byt VZDY 0-100
test_cases = [
    (98, -500, 600),   # zaporny surplus, BAT-FULL
    (98, -2000, 100),  # silne zaporny
    (50, 5000, 8000),  # extra vysoky surplus
    (50, 0, 1000),     # zero surplus
]
for soc_t, surp_t, pv_t in test_cases:
    ctx = make_ctx(soc=soc_t, surplus=surp_t, pv=pv_t, light_lux=60000)
    ev = ApplianceEvaluator(profiles=[washer])
    v = ev.evaluate(ctx)[0]
    if v.covered_pct is not None and 0 <= v.covered_pct <= 100:
        results.append({"name": f"covered_pct rozsah pro SOC={soc_t},surp={surp_t}", "passed": True, "fails": [], "verdict": v})
        print(f"{GREEN}✓{RESET} covered_pct rozumne pro SOC={soc_t},surp={surp_t}: {v.covered_pct}%")
    else:
        results.append({"name": f"covered_pct rozsah pro SOC={soc_t},surp={surp_t}", "passed": False, "fails": [f"out of range: {v.covered_pct}"], "verdict": v})
        print(f"{RED}✗{RESET} covered_pct OUT OF RANGE pro SOC={soc_t},surp={surp_t}: {v.covered_pct}%")

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
    print(f"\n{GREEN}{BOLD}✓ Všechny appliance testy prošly!{RESET}")
    sys.exit(0)
