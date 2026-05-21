"""
Appliance Evaluator - "muzu pustit pracku ted?"

v4.1.9 PREPSANE NA SROZUMITELNE PRAVIDLA:

Logika musi davat smysl manzelce, ne mit dokonalou matematiku.

Pravidla v poradi (prvni co plati = vysledek):

1. Noc / data nedostupna / SOC pod reservou       -> NE (red)
2. Hodne slunce + plna baterka                    -> JEĎ (green)
3. Hodne slunce + dostatecna baterka              -> JEĎ (green)
4. Trochu slunce + plna baterka                   -> JEĎ (green)
5. Skutecny prebytek pokryje cely cyklus          -> JEĎ (green)
6. Bez slunce / vecer + spotrebic je maly         -> POZOR (amber)
7. Vsechno ostatni                                -> POCKEJ (red)

Klicova zmena oproti v4.1.8:
- "Slunecni den + plna baterka" = VZDY GREEN, bez ohledu na peak.
  Argument: FVE je ted curtailed (omezena) protoze nema kam dat energii.
  Jakmile zapnes spotrebic, FVE okamzite naskoci a pokryje to. Stejne jako
  u virivky (BAT-FULL kickstart). Baterka se ani nedotkne.
- Coverage_pct ukazuje "PV potencial / avg_w" - srozumitelne 0-100%
- Verdict text rika srozumitelne PROC ne nejakou technickou hatmatilku
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from ..state import SystemContext, DayStrategy


@dataclass
class ApplianceProfile:
    id: str
    name: str
    emoji: str
    peak_w: float
    avg_w: float
    cycle_min: int
    phase: int = 0

    @property
    def cycle_kwh(self) -> float:
        return self.avg_w * (self.cycle_min / 60.0) / 1000.0


DEFAULT_PROFILES: List[ApplianceProfile] = [
    ApplianceProfile(id="washer", name="Pračka",     emoji="🧺",
                     peak_w=2200, avg_w=500,  cycle_min=120, phase=1),
    ApplianceProfile(id="dryer",  name="Sušička",    emoji="🌀",
                     peak_w=2200, avg_w=800,  cycle_min=90,  phase=2),
    ApplianceProfile(id="dish",   name="Myčka",      emoji="🍽",
                     peak_w=2200, avg_w=700,  cycle_min=120, phase=3),
    ApplianceProfile(id="oven",   name="Trouba",     emoji="🔥",
                     peak_w=3000, avg_w=1500, cycle_min=60,  phase=1),
    ApplianceProfile(id="hob",    name="Varná deska", emoji="🍳",
                     peak_w=3500, avg_w=1800, cycle_min=30,  phase=1),
    ApplianceProfile(id="iron",   name="Žehlička",   emoji="👔",
                     peak_w=2400, avg_w=1000, cycle_min=30,  phase=0),
]


@dataclass
class ApplianceVerdict:
    id: str
    name: str
    emoji: str
    status: str           # "green" | "amber" | "red"
    confidence: str       # "high" | "medium" | "low"
    message: str
    peak_w: float
    avg_w: float
    cycle_min: int
    cycle_kwh: float
    surplus_now_w: Optional[float]
    covered_pct: Optional[float]
    pv_now_w: Optional[float] = None
    deficit_kwh: float = 0.0
    from_battery_kwh: float = 0.0
    from_grid_kwh: float = 0.0
    # v4.1.9 NEW: source pro jednoduche zobrazeni odkud energie pojde
    energy_source: str = "unknown"  # "solar" | "solar+battery" | "battery" | "grid"


class ApplianceEvaluator:
    def __init__(self, profiles: Optional[List[ApplianceProfile]] = None,
                 soc_reserve_pct: float = 25.0,
                 battery_kwh_total: float = 32.0,
                 min_soc_for_green_pct: float = 50.0,
                 battery_full_soc_pct: float = 95.0):
        self.profiles = profiles or DEFAULT_PROFILES
        self.soc_reserve_pct = soc_reserve_pct
        self.battery_kwh_total = battery_kwh_total
        self.min_soc_for_green_pct = min_soc_for_green_pct
        self.battery_full_soc_pct = battery_full_soc_pct

    def _battery_available_kwh(self, ctx: SystemContext) -> Optional[float]:
        soc = ctx.victron.soc_pct
        if soc is None: return None
        usable = max(0, soc - self.soc_reserve_pct)
        return (usable / 100.0) * self.battery_kwh_total

    def _stable_surplus(self, ctx: SystemContext) -> Optional[float]:
        cutoff = time.time() - 90
        recent = [v for t, v in ctx.surplus_history if t >= cutoff]
        if len(recent) < 3:
            return None
        return sum(recent) / len(recent)

    def _pv_potential_w(self, ctx: SystemContext) -> float:
        """Odhad maximalni produkce FVE v aktualnich svetelnych podminkach.

        Pri BAT-FULL je realna produkce (pv_power_w) curtailed - FVE schvalne
        produkuje min protoze nema kam s tim. Tato funkce vrati kolik by FVE
        umela dat KDYBY byla potreba.

        Vychazime z mereni jasu (Lux) a typickeho pomeru pro 11.8 kWp instalaci.
        """
        env = ctx.env
        pv_now = ctx.victron.pv_power_w or 0
        if env.is_stale or env.light_lux is None:
            # Bez meteostanice - veriit aktualnimu pv_now
            return pv_now

        lux = env.light_lux
        # Empiricka tabulka pro 11.8 kWp - kalibrovat podle reality
        if lux > 80000:    # plne slunce v poledne
            min_potential = 8000
        elif lux > 60000:  # silne slunce
            min_potential = 5500
        elif lux > 40000:  # bezne slunecno
            min_potential = 3500
        elif lux > 20000:  # polojasno
            min_potential = 2000
        elif lux > 10000:  # zatazeno-svetlo
            min_potential = 1000
        elif lux > 1000:   # silne zatazeno
            min_potential = 400
        else:              # tma / vecer
            min_potential = 0

        # Vrat MAX z aktualni produkce a teoretickeho potencialu
        return max(pv_now, min_potential)

    def evaluate(self, ctx: SystemContext) -> List[ApplianceVerdict]:
        surplus = self._stable_surplus(ctx)
        if surplus is None:
            surplus = ctx.victron.surplus_w
        bat_kwh = self._battery_available_kwh(ctx)
        soc = ctx.victron.soc_pct
        strat = ctx.plan.strategy

        results = []
        for p in self.profiles:
            verdict = self._evaluate_one(p, surplus, bat_kwh, soc, strat, ctx)
            results.append(verdict)
        return results

    def _evaluate_one(self, p: ApplianceProfile, surplus: Optional[float],
                      bat_kwh: Optional[float], soc: Optional[float],
                      strat: DayStrategy, ctx: SystemContext) -> ApplianceVerdict:
        v = ctx.victron
        env = ctx.env
        pv_now = v.pv_power_w
        pv_potential = self._pv_potential_w(ctx)
        battery_full = soc is not None and soc >= self.battery_full_soc_pct

        # Slunce - postavene na lux + nestaly cas + mame nejaky pv potencial
        sun_strong = (env.light_lux is not None and env.light_lux > 40000
                      and not env.is_stale and pv_potential > 2500)
        sun_some = (env.light_lux is not None and env.light_lux > 10000
                    and not env.is_stale and pv_potential > 800)

        # ============ STEP 1: Stale / nedostupne ============
        if ctx.victron.is_stale:
            return self._mk_verdict(p, "red", "low", "data nejsou aktuální",
                                    surplus, None, pv_now, energy_source="unknown")

        if surplus is None or soc is None:
            return self._mk_verdict(p, "amber", "low", "nedostatek dat",
                                    surplus, None, pv_now, energy_source="unknown")

        # ============ STEP 2: Hard RED - SOC pod reservou ============
        if soc < self.soc_reserve_pct:
            return self._mk_verdict(p, "red", "high",
                                    f"baterka {soc:.0f}% - musíme šetřit",
                                    surplus, 0.0, pv_now, energy_source="grid")

        # ============ STEP 3: SURVIVE strategie - hard RED ============
        if strat == DayStrategy.SURVIVE:
            return self._mk_verdict(
                p, "red", "high", "zataženo, šetříme do večera",
                surplus, 0.0, pv_now, energy_source="grid",
            )

        # ============ Pomocne vypocty ============
        # Coverage - vzdy ze SLUSNEHO odhadu, NE ze surplus
        # Co se da realne pokryt z FVE potencialu (po odecteni domu)
        load_now = v.load_total_w or 500
        pv_available_for_appliance = max(0, pv_potential - load_now)
        covered_pct = self._clamp_pct(pv_available_for_appliance, p.avg_w)

        needed_kwh = p.cycle_kwh
        # Energie kterou nabidne FVE behem cyklu (zjednoduseny odhad - linearne)
        pv_cycle_kwh = pv_available_for_appliance * (p.cycle_min / 60.0) / 1000.0
        deficit_kwh = max(0, needed_kwh - pv_cycle_kwh)
        from_battery = min(deficit_kwh, bat_kwh or 0)
        from_grid = max(0, deficit_kwh - from_battery)

        # ============ STEP 4: SLUNECNI DEN + PLNA BATERKA = JEĎ (klicove pravidlo) ============
        # Tohle je presny scenar z reality:
        # FVE produkuje malo (curtailed), surplus je zaporny, ale jakmile zapnes
        # spotrebic, FVE okamzite naskoci. To same chovani jako vırivka KICKSTART.
        # Manzelka: "Slunce sviti, baterka plna -> jed co chces."
        if sun_strong and battery_full:
            return self._mk_verdict(
                p, "green", "high",
                f"☀ slunce + 🔋 baterka plná ({soc:.0f}%) - jeď, FVE naskočí",
                surplus, covered_pct, pv_now,
                deficit_kwh=0.0, from_battery_kwh=0.0, from_grid_kwh=0.0,
                energy_source="solar",
            )

        # ============ STEP 5: SILNE SLUNCE + DOSTATECNA BATERKA = JEĎ ============
        # I kdyz baterka neni 100% ale je slusna (>70%) a hodne sviti
        if sun_strong and soc is not None and soc >= 70:
            return self._mk_verdict(
                p, "green", "high",
                f"☀ slunce + 🔋 baterka {soc:.0f}% - jeď, FVE pokryje",
                surplus, covered_pct, pv_now,
                deficit_kwh=0.0, from_battery_kwh=0.0, from_grid_kwh=0.0,
                energy_source="solar",
            )

        # ============ STEP 6: TROCHU SLUNCE + PLNA BATERKA + maly spotrebic = JEĎ ============
        # Mensi spotrebic (pracka 500W, mycka 700W) zvladne FVE i pri polojasnu
        if sun_some and battery_full and p.avg_w <= 1500:
            return self._mk_verdict(
                p, "green", "high",
                f"⛅ jasno + 🔋 baterka plná - jeď, malý odběr FVE zvládne",
                surplus, covered_pct, pv_now,
                deficit_kwh=0.0, from_battery_kwh=0.0, from_grid_kwh=0.0,
                energy_source="solar",
            )

        # ============ STEP 7: Skutecny prebytek pokryje cely cyklus ============
        if surplus is not None and surplus >= p.avg_w and soc >= self.min_soc_for_green_pct:
            return self._mk_verdict(
                p, "green", "high",
                f"přebytek {surplus:.0f}W ≥ potřeba {p.avg_w:.0f}W",
                surplus, covered_pct, pv_now,
                deficit_kwh=0.0, from_battery_kwh=0.0, from_grid_kwh=0.0,
                energy_source="solar",
            )

        # ============ STEP 8: AMBER - baterka pomuze, ale neni to ideal ============
        # Nelze prosvitit slunce ale baterka ma dost rezervy a deficit je male
        if bat_kwh is not None and from_battery >= deficit_kwh - 0.05 \
                and deficit_kwh < bat_kwh * 0.3 \
                and soc is not None and soc >= 60:
            cause = "bez slunce" if not sun_some else "slunce slabne"
            return self._mk_verdict(
                p, "amber", "medium",
                f"{cause}, ~{deficit_kwh:.1f} kWh z baterky",
                surplus, covered_pct, pv_now,
                deficit_kwh=deficit_kwh, from_battery_kwh=from_battery, from_grid_kwh=0.0,
                energy_source="solar+battery" if sun_some else "battery",
            )

        # ============ STEP 9: RED - vetsina by sla ze site ============
        if from_grid > 0.2:
            cause = "tma" if not sun_some else "slabé slunce"
            return self._mk_verdict(
                p, "red", "medium",
                f"{cause}, ~{from_grid:.1f} kWh ze sítě",
                surplus, covered_pct, pv_now,
                deficit_kwh=deficit_kwh, from_battery_kwh=from_battery,
                from_grid_kwh=from_grid,
                energy_source="grid",
            )

        # ============ STEP 10: Fallback AMBER ============
        return self._mk_verdict(
            p, "amber", "low",
            f"~{deficit_kwh:.1f} kWh z baterky",
            surplus, covered_pct, pv_now,
            deficit_kwh=deficit_kwh, from_battery_kwh=from_battery,
            from_grid_kwh=from_grid,
            energy_source="solar+battery",
        )

    def _clamp_pct(self, value: float, denom: float) -> float:
        """Vraci 0-100. Nikdy zaporne, nikdy nad 100."""
        if denom <= 0: return 0.0
        return round(max(0.0, min(100.0, (value / denom) * 100.0)), 0)

    def _mk_verdict(self, p: ApplianceProfile, status: str, confidence: str,
                     message: str, surplus: Optional[float],
                     covered_pct: Optional[float], pv_now: Optional[float],
                     deficit_kwh: float = 0.0, from_battery_kwh: float = 0.0,
                     from_grid_kwh: float = 0.0,
                     energy_source: str = "unknown") -> ApplianceVerdict:
        return ApplianceVerdict(
            id=p.id, name=p.name, emoji=p.emoji,
            status=status, confidence=confidence,
            message=message,
            peak_w=p.peak_w, avg_w=p.avg_w, cycle_min=p.cycle_min,
            cycle_kwh=p.cycle_kwh,
            surplus_now_w=surplus,
            covered_pct=covered_pct,
            pv_now_w=pv_now,
            deficit_kwh=round(deficit_kwh, 2),
            from_battery_kwh=round(from_battery_kwh, 2),
            from_grid_kwh=round(from_grid_kwh, 2),
            energy_source=energy_source,
        )


def profiles_from_config(cfg: dict) -> List[ApplianceProfile]:
    """Vytvari profily ze config.yaml sekce 'appliances'. Pokud chybi, vraci default."""
    if "appliances" not in cfg or not cfg["appliances"]:
        return DEFAULT_PROFILES
    profiles = []
    for item in cfg["appliances"]:
        profiles.append(ApplianceProfile(
            id=item["id"],
            name=item["name"],
            emoji=item.get("emoji", "⚡"),
            peak_w=float(item.get("peak_w", 2200)),
            avg_w=float(item.get("avg_w", 800)),
            cycle_min=int(item.get("cycle_min", 60)),
            phase=int(item.get("phase", 0)),
        ))
    return profiles
