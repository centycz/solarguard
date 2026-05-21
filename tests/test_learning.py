"""
SolarGuard v4.3.0 - Appliance Learning test suite.

Testuje sledovani aktivnich cyklu + uceni profilu.

Spusteni:
    cd /home/pi/solarguard
    .venv/bin/python tests/test_learning.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solarguard.state import SystemContext
from solarguard.engine.appliance_learning import (
    ApplianceLearningManager, ActiveCycle, LearnedProfile, CycleResult
)

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

results = []


def make_ctx(*, l1=300, l2=200, l3=150, spa_heater=False):
    ctx = SystemContext()
    ctx.victron.last_update = time.time()
    ctx.victron.load_l1_w = l1
    ctx.victron.load_l2_w = l2
    ctx.victron.load_l3_w = l3
    ctx.spa.heater_on = spa_heater
    return ctx


def ok(name, condition, detail=""):
    results.append({"name": name, "passed": condition, "detail": detail})
    color = GREEN if condition else RED
    icon = "✓" if condition else "✗"
    print(f"{color}{icon}{RESET} {name}")
    if detail:
        print(f"   {DIM}{detail}{RESET}")


async def main():
    tmpdir = tempfile.mkdtemp(prefix="sg-learn-test-")

    try:
        print(f"\n{BOLD}{CYAN}═══ START / STOP CYKLU ═══{RESET}")

        # 1. Start cyklu
        ctx = make_ctx(l1=300, l2=200, l3=150)
        mgr = ApplianceLearningManager(ctx, data_dir=tmpdir)
        result = await mgr.start_cycle("washer")
        ok("Start cyklu - vraci success",
           result.get("success") is True,
           f"baseline: {result.get('baseline')}")

        ok("Po startu je cyklus v active_cycles",
           "washer" in mgr.active_cycles)

        # 2. Duplicitni start
        result2 = await mgr.start_cycle("washer")
        ok("Duplicitni start vraci error",
           result2.get("success") is False)

        # 3. Stop bez sample - zadne data
        result3 = await mgr.stop_cycle("washer", reason="test")
        ok("Stop bez sample vraci CycleResult",
           result3 is not None and result3.sample_count == 0)
        ok("Stop nevyrobi profil pri 0 sample",
           "washer" not in mgr.profiles)

        print(f"\n{BOLD}{CYAN}═══ DETEKCE FÁZE ═══{RESET}")

        # 4. Detekce L1 (pracka, kuchynska faze)
        ctx = make_ctx(l1=300, l2=200, l3=150)
        mgr = ApplianceLearningManager(ctx, data_dir=tmpdir)
        await mgr.start_cycle("washer")
        cycle = mgr.active_cycles["washer"]
        # Simuluj zvysenou load na L1 (pracka topi)
        cycle.started_at = time.time() - 60  # backdate aby PHASE_DETECTION_DELAY_SEC nevadil
        ctx.victron.load_l1_w = 300 + 1500  # +1500W na L1
        await mgr._take_sample(cycle)
        ok("Detekce faze L1 po +1500W na L1",
           cycle.detected_phase == 1,
           f"detekovana faze: L{cycle.detected_phase}")
        await mgr.stop_cycle("washer")

        # 5. Detekce L2 (susicka)
        ctx = make_ctx(l1=300, l2=200, l3=150)
        mgr = ApplianceLearningManager(ctx, data_dir=tmpdir)
        await mgr.start_cycle("dryer")
        cycle = mgr.active_cycles["dryer"]
        cycle.started_at = time.time() - 60
        ctx.victron.load_l2_w = 200 + 800
        await mgr._take_sample(cycle)
        ok("Detekce faze L2 po +800W na L2",
           cycle.detected_phase == 2)
        await mgr.stop_cycle("dryer")

        print(f"\n{BOLD}{CYAN}═══ KOMPLETNI CYKLUS - peak/avg/kwh ═══{RESET}")

        # 6. Simulace celeho cyklu - profile update
        ctx = make_ctx(l1=300, l2=200, l3=150)
        mgr = ApplianceLearningManager(ctx, data_dir=tmpdir)
        await mgr.start_cycle("washer")
        cycle = mgr.active_cycles["washer"]
        # Simuluj 10 vzorku po 30s = 5 minut cyklu na L1 s prum 1500W (peak 2000W)
        loads = [1000, 1500, 2000, 1900, 1500, 1200, 800, 600, 400, 200]
        for i, load_delta in enumerate(loads):
            cycle.samples.append((cycle.started_at + i * 30, load_delta, load_delta + 650))
            cycle.detected_phase = 1
        # Vyhodnocení
        result = mgr._evaluate_cycle(cycle, end_reason="test")
        ok("CycleResult peak je max",
           result.peak_w == 2000.0, f"peak={result.peak_w}")
        ok("CycleResult avg je mean",
           abs(result.avg_w - sum(loads)/len(loads)) < 1)
        ok("CycleResult duration_min je rozumny (~4.5 min)",
           4 < result.duration_min < 6, f"duration={result.duration_min}")
        ok("CycleResult kwh > 0",
           result.kwh > 0)
        await mgr.stop_cycle("washer")  # cleanup

        print(f"\n{BOLD}{CYAN}═══ PERSISTENCE PROFILU ═══{RESET}")

        # 7. Po ulozeni profilu se nacte pri restartu (vlastni subdir)
        sub1 = tempfile.mkdtemp(prefix="sg-pers1-")
        ctx = make_ctx()
        mgr1 = ApplianceLearningManager(ctx, data_dir=sub1)
        mgr1._update_profile(CycleResult(
            appliance_id="washer", duration_min=120, peak_w=2200,
            avg_w=500, kwh=1.0, detected_phase=1, sample_count=10,
        ))
        mgr1._save_profiles()
        ctx2 = make_ctx()
        mgr2 = ApplianceLearningManager(ctx2, data_dir=sub1)
        prof = mgr2.get_profile("washer")
        ok("Profil je perzistovan a nacten po restartu",
           prof is not None and prof.avg_peak_w == 2200,
           f"prof: {prof}")

        # 8. Vazene prumerovani po druhem cyklu (mgr2 ma uz 1 cyklus z disku)
        mgr2._update_profile(CycleResult(
            appliance_id="washer", duration_min=130, peak_w=2400,
            avg_w=550, kwh=1.2, detected_phase=1, sample_count=12,
        ))
        prof2 = mgr2.get_profile("washer")
        ok("Vazeny prumer peak po druhem cyklu (~2260)",
           2255 < prof2.avg_peak_w < 2265,
           f"actual={prof2.avg_peak_w:.1f}")
        ok("sample_count se inkrementuje",
           prof2.sample_count == 2,
           f"sample_count={prof2.sample_count}")
        shutil.rmtree(sub1, ignore_errors=True)

        print(f"\n{BOLD}{CYAN}═══ INTERRUPTED CYCLE ═══{RESET}")

        # 9. Interrupted cyklus se NEPOUZIJE pro update profilu
        ctx = make_ctx(l1=300, l2=200, l3=150)
        mgr3 = ApplianceLearningManager(ctx, data_dir=tmpdir)
        await mgr3.start_cycle("oven")
        cycle = mgr3.active_cycles["oven"]
        cycle.detected_phase = 1
        cycle.interrupted_by_other_load = True
        cycle.notes = "Test interrupted"
        cycle.samples = [(cycle.started_at + i*30, 1500, 2000) for i in range(5)]
        result_int = await mgr3.stop_cycle("oven", reason="test")
        ok("Interrupted result vraci interrupted=True",
           result_int.interrupted is True)
        ok("Interrupted cyklus NEAKTUALIZUJE profil",
           "oven" not in mgr3.profiles)

        print(f"\n{BOLD}{CYAN}═══ STAR CYKLU SE STALE DATY ═══{RESET}")

        # 10. Stale Victron data
        ctx = make_ctx()
        ctx.victron.last_update = time.time() - 300  # 5 min stara
        mgr4 = ApplianceLearningManager(ctx, data_dir=tmpdir)
        result_stale = await mgr4.start_cycle("dish")
        ok("Stale Victron -> start cycle vraci error",
           result_stale.get("success") is False)

        # 11. Auto-end pri zadnem load (uzivatel klikl omylem)
        ctx = make_ctx(l1=300, l2=200, l3=150)
        mgr5 = ApplianceLearningManager(ctx, data_dir=tmpdir)
        await mgr5.start_cycle("iron")
        cycle = mgr5.active_cycles["iron"]
        cycle.started_at = time.time() - 400  # 6+ minut zpatky, zadne sample
        end = mgr5._check_auto_end(cycle)
        ok("Auto-end pri 5+ min bez detekovane faze",
           end is True)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

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
                if r.get("detail"):
                    print(f"    {r['detail']}")
        return 1
    print(f"\n{GREEN}{BOLD}✓ Všechny learning testy prošly!{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
