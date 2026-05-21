"""
Appliance Learning - "klikni a jed, ja si zapamatuju".

Princip:
- User v UI klikne "Pustil jsem" na karte spotrebice
- Zacneme sledovat na ktere fazi se zvedne load (po vyloucenii baselinu)
- Logujeme samples kazdych 30s po dobu cyklu
- Detekce konce: 90s trvalého poklesu pod 200W na te fazi
- Po dokonceni: spocitej peak/avg/duration/kWh a uloz do profilu
- Profil se updatuje vazenym prumerem (vic cyklu = stabilnejsi data)

Edge cases:
- Druhy spotrebic na stejne fazi behem cyklu -> ignoruj (warning v UI)
- Vıˇrivka na L2 (heater_on=True) -> odecti 2200W od baseline L2
- Cyklus s pulzama (pracka) -> sledujeme cele okno, peak = max sample,
  avg = prumer pres cely cyklus

Persistence:
- Naucene profily v JSON: /home/pi/solarguard/data/learned_profiles.json
- Pri startu se nactou a aplikuji na ApplianceProfile.peak_w/avg_w/cycle_min
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, List

from ..state import SystemContext

log = logging.getLogger("learning")


# ─────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ActiveCycle:
    """Probihajici cyklus jednoho spotrebice."""
    appliance_id: str
    started_at: float
    baseline_l1: float
    baseline_l2: float
    baseline_l3: float
    spa_running_at_start: bool = False  # virivka byla aktivni pri startu

    # Detekovana faze (po prvnich par vzorcich)
    detected_phase: Optional[int] = None  # 1, 2, 3 nebo None pokud jeste neidentifikovano

    # Samples: list of (timestamp, phase_load_watt, total_load_watt)
    samples: List[tuple] = field(default_factory=list)

    # Detekce konce - kdyz delta klesne pod 200W
    last_above_threshold: float = 0.0

    # Anomaly flagy
    interrupted_by_other_load: bool = False
    notes: str = ""


@dataclass
class LearnedProfile:
    """Naucenı profil spotrebice (perzistuje)."""
    appliance_id: str
    sample_count: int = 0       # kolik cyklu jsme nasledovali
    avg_peak_w: float = 0.0
    avg_avg_w: float = 0.0      # naivně, vážený průměr 'avg_w'
    avg_cycle_min: float = 0.0
    avg_cycle_kwh: float = 0.0
    detected_phase: Optional[int] = None  # nejcastejsi faze z poslednich cyklu
    last_cycle_at: float = 0.0
    last_cycle_peak_w: float = 0.0
    last_cycle_avg_w: float = 0.0
    last_cycle_min: float = 0.0
    last_cycle_kwh: float = 0.0


@dataclass
class CycleResult:
    """Vysledek dokonceneho cyklu - vraci se do UI."""
    appliance_id: str
    duration_min: float
    peak_w: float
    avg_w: float
    kwh: float
    detected_phase: Optional[int]
    sample_count: int
    notes: str = ""
    interrupted: bool = False


# ─────────────────────────────────────────────────────────────────────────
# MANAGER
# ─────────────────────────────────────────────────────────────────────────

class ApplianceLearningManager:
    """Sleduje aktivni cykly + uci se profily.

    Pouziti:
        mgr = ApplianceLearningManager(ctx, data_dir="/home/pi/solarguard/data")
        await mgr.start_cycle("washer")             # user klikl 'pustil jsem'
        # ... probiha sample loop ...
        await mgr.stop_cycle("washer")              # user klikl 'dokoncen' (nebo auto-detect)
        profile = mgr.get_profile("washer")         # ziskej naucena data
    """

    SAMPLE_INTERVAL_SEC = 30
    PHASE_DETECTION_DELTA_W = 200          # min stoupnuti na fazi pro identifikaci
    PHASE_DETECTION_DELAY_SEC = 30         # po jak dlouho zaciname identifikovat fazi
    CYCLE_END_THRESHOLD_W = 200            # pokud delta < tohle, blizime se konci
    CYCLE_END_DURATION_SEC = 90            # ... a kdyz takhle drzi 90s, cyklus konci
    OTHER_LOAD_SPIKE_W = 1500              # pokud na jine fazi naskoci > 1500W behem cyklu, oznac jako interrupted
    MAX_CYCLE_DURATION_SEC = 4 * 3600      # po 4 hodinach auto-stop (ochrana proti zapomenuti)

    HISTORY_WEIGHT = 0.7  # pri update profilu: 70% stara hodnota, 30% nova (smoothing)

    def __init__(self, context: SystemContext, data_dir: str = "/home/pi/solarguard/data"):
        self.ctx = context
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_path = self.data_dir / "learned_profiles.json"
        self.history_path = self.data_dir / "cycle_history.jsonl"

        self.active_cycles: Dict[str, ActiveCycle] = {}    # id -> ActiveCycle
        self.profiles: Dict[str, LearnedProfile] = {}      # id -> LearnedProfile
        self.recent_results: List[CycleResult] = []        # poslednich 20 cyklu pro UI

        self._sample_task: Optional[asyncio.Task] = None
        self._shutdown = asyncio.Event()
        self._lock = asyncio.Lock()

        self._load_profiles()

    # ─────── persistence ───────

    def _load_profiles(self):
        if not self.profiles_path.exists():
            log.info("No learned profiles found - start fresh")
            return
        try:
            with open(self.profiles_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for app_id, p_dict in data.items():
                self.profiles[app_id] = LearnedProfile(**p_dict)
            log.info(f"Loaded {len(self.profiles)} learned profiles")
        except Exception as e:
            log.warning(f"Failed to load profiles: {e}")

    def _save_profiles(self):
        try:
            data = {app_id: asdict(p) for app_id, p in self.profiles.items()}
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Failed to save profiles: {e}")

    def _append_history(self, result: CycleResult):
        try:
            record = {
                "ts": time.time(),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                **asdict(result),
            }
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"Failed to append history: {e}")

    # ─────── public API ───────

    async def start_cycle(self, appliance_id: str) -> dict:
        """User klikl 'PUSTIL JSEM' - zacni sledovat."""
        async with self._lock:
            if appliance_id in self.active_cycles:
                return {
                    "success": False,
                    "error": "Cyklus pro tento spotřebič už běží",
                    "started_at": self.active_cycles[appliance_id].started_at,
                }

            v = self.ctx.victron
            if v.is_stale or v.load_l1_w is None:
                return {"success": False, "error": "Victron data nejsou aktuální"}

            # Zaznamenej baseline. Pokud bezi virivka, odpoctej topny vykon z L2.
            baseline_l1 = v.load_l1_w or 0
            baseline_l2 = v.load_l2_w or 0
            baseline_l3 = v.load_l3_w or 0
            spa_running = bool(self.ctx.spa.heater_on)
            # Virivka topi na L2 ~2200W. Behem cyklu spotrebice budeme od L2 odecitat
            # baseline + (pokud spa pred concem nebezi nove zapnula) take 2200W.

            cycle = ActiveCycle(
                appliance_id=appliance_id,
                started_at=time.time(),
                baseline_l1=baseline_l1,
                baseline_l2=baseline_l2,
                baseline_l3=baseline_l3,
                spa_running_at_start=spa_running,
            )
            self.active_cycles[appliance_id] = cycle
            log.info(f"Started cycle: {appliance_id} (baseline L1={baseline_l1:.0f}W, "
                     f"L2={baseline_l2:.0f}W, L3={baseline_l3:.0f}W, spa={spa_running})")
            return {
                "success": True,
                "appliance_id": appliance_id,
                "started_at": cycle.started_at,
                "baseline": {"l1": baseline_l1, "l2": baseline_l2, "l3": baseline_l3},
            }

    async def stop_cycle(self, appliance_id: str, reason: str = "manual") -> Optional[CycleResult]:
        """User klikl 'STOP' nebo auto-detekce konce."""
        async with self._lock:
            cycle = self.active_cycles.pop(appliance_id, None)
            if not cycle:
                return None

            result = self._evaluate_cycle(cycle, end_reason=reason)
            if result.sample_count >= 3:  # alespon 90s aby to dávalo smysl
                self._update_profile(result)
                self._save_profiles()
                self._append_history(result)
            self.recent_results.insert(0, result)
            self.recent_results = self.recent_results[:20]

            log.info(f"Cycle finished: {appliance_id} duration={result.duration_min:.1f}min "
                     f"peak={result.peak_w:.0f}W avg={result.avg_w:.0f}W "
                     f"kwh={result.kwh:.2f} samples={result.sample_count}")
            return result

    def get_active_cycles(self) -> List[dict]:
        """Pro UI - vrati seznam aktivnich cyklu s aktualnimi daty."""
        out = []
        for cycle in self.active_cycles.values():
            current_phase_w = self._current_phase_load(cycle)
            elapsed = time.time() - cycle.started_at
            out.append({
                "appliance_id": cycle.appliance_id,
                "started_at": cycle.started_at,
                "elapsed_sec": int(elapsed),
                "detected_phase": cycle.detected_phase,
                "current_phase_w": current_phase_w,
                "sample_count": len(cycle.samples),
                "interrupted": cycle.interrupted_by_other_load,
            })
        return out

    def get_profile(self, appliance_id: str) -> Optional[LearnedProfile]:
        return self.profiles.get(appliance_id)

    def get_all_profiles(self) -> Dict[str, LearnedProfile]:
        return dict(self.profiles)

    def get_recent_results(self) -> List[CycleResult]:
        return list(self.recent_results)

    # ─────── internal ───────

    def _current_phase_load(self, cycle: ActiveCycle) -> float:
        """Aktualni delta na detekovane (nebo nejsilnejsi) fazi vs baseline."""
        v = self.ctx.victron
        if v.load_l1_w is None: return 0
        d1 = (v.load_l1_w or 0) - cycle.baseline_l1
        d2 = (v.load_l2_w or 0) - cycle.baseline_l2
        d3 = (v.load_l3_w or 0) - cycle.baseline_l3

        # Pokud spa nezacela behem cyklu, jeji 2200W jsou v baselinu - OK.
        # Pokud spa zacala mid-cycle, odpoctit:
        if not cycle.spa_running_at_start and self.ctx.spa.heater_on:
            d2 -= 2200

        if cycle.detected_phase == 1: return max(0, d1)
        if cycle.detected_phase == 2: return max(0, d2)
        if cycle.detected_phase == 3: return max(0, d3)
        # Jeste neidentifikovano - vrat nejsilnejsi
        return max(0, d1, d2, d3)

    async def _take_sample(self, cycle: ActiveCycle):
        """Zaznamena jeden vzorek."""
        v = self.ctx.victron
        if v.load_l1_w is None: return

        elapsed = time.time() - cycle.started_at
        d1 = (v.load_l1_w or 0) - cycle.baseline_l1
        d2 = (v.load_l2_w or 0) - cycle.baseline_l2
        d3 = (v.load_l3_w or 0) - cycle.baseline_l3
        if not cycle.spa_running_at_start and self.ctx.spa.heater_on:
            d2 -= 2200

        # Detekuj fazi pokud jeste neni
        if cycle.detected_phase is None and elapsed >= self.PHASE_DETECTION_DELAY_SEC:
            deltas = [(1, d1), (2, d2), (3, d3)]
            deltas.sort(key=lambda x: x[1], reverse=True)
            top_phase, top_delta = deltas[0]
            if top_delta >= self.PHASE_DETECTION_DELTA_W:
                cycle.detected_phase = top_phase
                log.info(f"  {cycle.appliance_id}: detected phase L{top_phase} (+{top_delta:.0f}W)")

        # Detekuj interrupted: pokud na *jine* fazi nez detekovane je velky skok
        if cycle.detected_phase is not None and not cycle.interrupted_by_other_load:
            others = [d for ph, d in [(1, d1), (2, d2), (3, d3)] if ph != cycle.detected_phase]
            for od in others:
                if od > self.OTHER_LOAD_SPIKE_W:
                    cycle.interrupted_by_other_load = True
                    cycle.notes = f"Jiný spotřebič naskočil mid-cyklus (+{od:.0f}W)"
                    log.warning(f"  {cycle.appliance_id}: {cycle.notes}")
                    break

        # Aktualni phase load
        phase_w = self._current_phase_load(cycle)
        total_w = (v.load_l1_w or 0) + (v.load_l2_w or 0) + (v.load_l3_w or 0)
        cycle.samples.append((time.time(), phase_w, total_w))

        # Sleduj kdy load posledne presel pres prah
        if phase_w > self.CYCLE_END_THRESHOLD_W:
            cycle.last_above_threshold = time.time()

    def _check_auto_end(self, cycle: ActiveCycle) -> bool:
        """Vraci True pokud detekujeme konec cyklu."""
        elapsed = time.time() - cycle.started_at

        # Tvrdy max - 4 hodiny
        if elapsed > self.MAX_CYCLE_DURATION_SEC:
            return True

        # Bez detekované faze: jenom kdyz nikdy nepresel pres prah a uz uplynulo 5 min
        if cycle.detected_phase is None:
            if elapsed > 300:
                # Nikdy jsme nedetekovali load - asi uzivatel klikl omylem
                cycle.notes = "Žádný odběr nedetekován (možná chybný klik?)"
                return True
            return False

        # Detekovaná faze: konec kdyz load < threshold po dobu CYCLE_END_DURATION_SEC
        if cycle.last_above_threshold == 0:
            return False  # jeste nikdy nebyla nad
        time_since_above = time.time() - cycle.last_above_threshold
        return time_since_above > self.CYCLE_END_DURATION_SEC

    def _evaluate_cycle(self, cycle: ActiveCycle, end_reason: str = "manual") -> CycleResult:
        """Spocita finalni metriky cyklu."""
        if not cycle.samples:
            return CycleResult(
                appliance_id=cycle.appliance_id,
                duration_min=0, peak_w=0, avg_w=0, kwh=0,
                detected_phase=cycle.detected_phase, sample_count=0,
                notes="Žádné vzorky", interrupted=cycle.interrupted_by_other_load,
            )

        powers = [s[1] for s in cycle.samples]  # phase_load samples
        peak_w = max(powers)
        avg_w = sum(powers) / len(powers)
        duration_sec = cycle.samples[-1][0] - cycle.samples[0][0]
        duration_min = duration_sec / 60.0
        # Energy = sum(power * dt) - kazdy sample reprezentuje SAMPLE_INTERVAL_SEC
        # presnejsi by bylo trapezoidal, ale staci aproximace
        kwh = (avg_w * duration_sec / 3600.0) / 1000.0

        notes = cycle.notes or f"end: {end_reason}"

        return CycleResult(
            appliance_id=cycle.appliance_id,
            duration_min=round(duration_min, 1),
            peak_w=round(peak_w, 0),
            avg_w=round(avg_w, 0),
            kwh=round(kwh, 3),
            detected_phase=cycle.detected_phase,
            sample_count=len(cycle.samples),
            notes=notes,
            interrupted=cycle.interrupted_by_other_load,
        )

    def _update_profile(self, result: CycleResult):
        """Aktualizuj naucenı profil. Vyhodi mid-cyklus interrupted vysledky."""
        if result.interrupted:
            log.info(f"  Skipping profile update for {result.appliance_id} (interrupted cycle)")
            return

        prof = self.profiles.get(result.appliance_id)
        if prof is None:
            # Prvni cyklus - prijmi přímo
            prof = LearnedProfile(
                appliance_id=result.appliance_id,
                sample_count=1,
                avg_peak_w=result.peak_w,
                avg_avg_w=result.avg_w,
                avg_cycle_min=result.duration_min,
                avg_cycle_kwh=result.kwh,
                detected_phase=result.detected_phase,
                last_cycle_at=time.time(),
                last_cycle_peak_w=result.peak_w,
                last_cycle_avg_w=result.avg_w,
                last_cycle_min=result.duration_min,
                last_cycle_kwh=result.kwh,
            )
        else:
            # Vazeny prumer (smoothing) - 70% historie, 30% novy
            w = self.HISTORY_WEIGHT
            prof.avg_peak_w = w * prof.avg_peak_w + (1-w) * result.peak_w
            prof.avg_avg_w = w * prof.avg_avg_w + (1-w) * result.avg_w
            prof.avg_cycle_min = w * prof.avg_cycle_min + (1-w) * result.duration_min
            prof.avg_cycle_kwh = w * prof.avg_cycle_kwh + (1-w) * result.kwh
            prof.sample_count += 1
            prof.detected_phase = result.detected_phase or prof.detected_phase
            prof.last_cycle_at = time.time()
            prof.last_cycle_peak_w = result.peak_w
            prof.last_cycle_avg_w = result.avg_w
            prof.last_cycle_min = result.duration_min
            prof.last_cycle_kwh = result.kwh

        self.profiles[result.appliance_id] = prof
        log.info(f"  Updated profile: {result.appliance_id} "
                 f"(N={prof.sample_count}, avg peak={prof.avg_peak_w:.0f}W, "
                 f"avg cycle={prof.avg_cycle_min:.0f}min, avg kWh={prof.avg_cycle_kwh:.2f})")

    # ─────── Sample loop ───────

    async def _sample_loop(self):
        while not self._shutdown.is_set():
            try:
                # Iterate copy of values (mozna se behem iterace upravi)
                for cycle in list(self.active_cycles.values()):
                    await self._take_sample(cycle)
                    if self._check_auto_end(cycle):
                        log.info(f"  Auto-ending cycle: {cycle.appliance_id}")
                        await self.stop_cycle(cycle.appliance_id, reason="auto-detect")
            except Exception as e:
                log.error(f"sample loop error: {e}")

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self.SAMPLE_INTERVAL_SEC,
                )
            except asyncio.TimeoutError:
                pass

    async def start(self):
        if self._sample_task is not None: return
        self._shutdown.clear()
        self._sample_task = asyncio.create_task(self._sample_loop())
        log.info(f"Appliance learning manager started (sample every {self.SAMPLE_INTERVAL_SEC}s)")

    async def stop(self):
        self._shutdown.set()
        if self._sample_task:
            self._sample_task.cancel()
            try: await self._sample_task
            except asyncio.CancelledError: pass
        # Save state
        self._save_profiles()
