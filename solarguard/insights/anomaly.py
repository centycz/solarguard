"""
v4.0 NEW: Anomaly detection.

Vede rolling window posledních 14 dnů a detekuje odchylky:
- FV výroba dnes vs 7-day median (značí špatné počasí, ALE i poškozený panel)
- Spotřeba domu vs 7-day median (značí nezvyklou aktivitu)
- Vířivka topí déle než obvykle při dané delta T a počasí

Data se ukládají do JSON souboru v log_dir, perzistuje přes restarty.
Kompatibilní bez InfluxDB - vlastní mini-DB.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

log = logging.getLogger("insights")


@dataclass
class DailySummary:
    """Souhrn jednoho dne - ukládáme jeden záznam za 24h."""
    date: str  # YYYY-MM-DD
    pv_yield_kwh: Optional[float] = None
    consumption_kwh: Optional[float] = None
    avg_air_temp_c: Optional[float] = None
    sunny_hours: float = 0  # počet hodin s lux > 40000
    heating_sessions: int = 0
    total_heating_minutes: float = 0
    spike_count: int = 0  # kolikrát se aktivoval spike_cool


@dataclass
class Insight:
    """Jeden insight pro UI."""
    severity: str   # "info", "warn", "alert"
    icon: str       # emoji
    title: str
    detail: str
    metric: Optional[str] = None  # např. "pv_yield"
    value: Optional[float] = None
    baseline: Optional[float] = None
    deviation_pct: Optional[float] = None


class AnomalyDetector:
    """Sleduje denní souhrny a detekuje anomálie.

    Použití:
        d = AnomalyDetector(log_dir, ctx)
        await d.start()  # registruje midnight tick

        # V tick handleru:
        d.update_running_sums(ctx)

        # Pro UI:
        insights = d.compute_insights()
    """

    def __init__(self, log_dir: str, context, window_days: int = 14,
                 min_baseline_days: int = 3):
        self.ctx = context
        self.window_days = window_days
        self.min_baseline_days = min_baseline_days  # kolik dní potřeba pro baseline
        self.path = Path(log_dir) / "daily_summaries.json"
        self.history: List[DailySummary] = []
        self._today: Optional[DailySummary] = None
        self._load()
        self._init_today()

        # Rolling stav během dne
        self._sunny_seconds = 0.0
        self._last_sample_ts = 0.0
        self._last_heater = False
        self._heating_start_ts = 0.0
        self._heating_minutes_today = 0.0
        self._heating_count_today = 0
        self._spike_count_today = 0

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.history = [DailySummary(**d) for d in data]
            log.info(f"Loaded {len(self.history)} daily summaries from {self.path}")
        except Exception as e:
            log.warning(f"Failed to load daily summaries: {e}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(d) for d in self.history]
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to save daily summaries: {e}")

    def _init_today(self) -> None:
        today_str = date.today().isoformat()
        # Pokud poslední záznam je dnes, pokračuj v něm
        if self.history and self.history[-1].date == today_str:
            self._today = self.history[-1]
        else:
            self._today = DailySummary(date=today_str)
            self.history.append(self._today)
            # Trim na window
            if len(self.history) > self.window_days:
                self.history = self.history[-self.window_days:]

    def update_running_sums(self, ctx) -> None:
        """Volat každý tick - aktualizuje rolling sumy pro dnešní summary."""
        # Detekuj přechod přes půlnoc
        today_str = date.today().isoformat()
        if self._today is None or self._today.date != today_str:
            self._rollover(today_str)

        now = time.time()
        if self._last_sample_ts == 0:
            self._last_sample_ts = now
            return

        dt_h = (now - self._last_sample_ts) / 3600.0
        if dt_h > 0.1:  # po restartu, neaplikuj
            self._last_sample_ts = now
            return

        # Sunny hours
        e = ctx.env
        if e.light_lux is not None and e.light_lux > 40000:
            self._sunny_seconds += (now - self._last_sample_ts)

        # Heating session detection
        s = ctx.spa
        if s.heater_on and not self._last_heater:
            # Začátek topení
            self._heating_start_ts = now
            self._heating_count_today += 1
        elif not s.heater_on and self._last_heater:
            # Konec topení
            if self._heating_start_ts > 0:
                duration_min = (now - self._heating_start_ts) / 60.0
                self._heating_minutes_today += duration_min
                self._heating_start_ts = 0.0
        self._last_heater = bool(s.heater_on)

        # Spike count
        from .. state import SystemState
        # Detekce přechodu na SPIKE_COOLDOWN by byla v transition handleru,
        # ale tady aspoň napočítáme jednorázové sample
        # (lepší by bylo callback z transition - integrace v main.py)

        # Ulož aktuální průběžné hodnoty do _today
        v = ctx.victron
        self._today.pv_yield_kwh = v.pv_yield_today_kwh
        self._today.consumption_kwh = v.consumption_today_kwh
        if e.air_temp_c is not None:
            # Klouzavý průměr - jednoduchá EMA
            if self._today.avg_air_temp_c is None:
                self._today.avg_air_temp_c = e.air_temp_c
            else:
                self._today.avg_air_temp_c = 0.99 * self._today.avg_air_temp_c + 0.01 * e.air_temp_c
        self._today.sunny_hours = round(self._sunny_seconds / 3600.0, 2)
        self._today.heating_sessions = self._heating_count_today
        self._today.total_heating_minutes = round(self._heating_minutes_today, 1)
        self._today.spike_count = self._spike_count_today

        self._last_sample_ts = now

    def _rollover(self, new_date_str: str) -> None:
        """Půlnoc - finalizuj včerejšek, založ nový dnešek."""
        log.info(f"Daily rollover: {self._today.date} -> {new_date_str}")
        self._save()
        self._today = DailySummary(date=new_date_str)
        self.history.append(self._today)
        if len(self.history) > self.window_days:
            self.history = self.history[-self.window_days:]
        self._sunny_seconds = 0.0
        self._heating_minutes_today = 0.0
        self._heating_count_today = 0
        self._spike_count_today = 0
        self._last_heater = False
        self._heating_start_ts = 0.0

    def record_spike(self) -> None:
        """Volat z main.py při přechodu do SPIKE_COOLDOWN."""
        self._spike_count_today += 1
        if self._today:
            self._today.spike_count = self._spike_count_today

    def save(self) -> None:
        """Volat při shutdown - persist current state."""
        self._save()

    def _baseline(self, attr: str) -> Optional[float]:
        """Median posledních N dní (mimo dnešek) pro daný atribut."""
        prev_days = self.history[:-1] if self.history else []
        if len(prev_days) < self.min_baseline_days:
            return None
        values = [getattr(d, attr) for d in prev_days[-7:]]  # max 7 dní pro median
        values = [v for v in values if v is not None]
        if len(values) < self.min_baseline_days:
            return None
        return statistics.median(values)

    def compute_insights(self) -> List[Insight]:
        """Vrátí list insightů pro UI. Empty list = vše OK."""
        insights = []
        if self._today is None:
            return insights

        now = datetime.now()

        # 1. FV výroba dnes - pouze pokud už je dost pozdě (po 18h)
        # Předtím ještě stoupá, neporovnávat
        if now.hour >= 18:
            today_pv = self._today.pv_yield_kwh
            baseline = self._baseline("pv_yield_kwh")
            if today_pv is not None and baseline is not None and baseline > 0:
                deviation = (today_pv - baseline) / baseline * 100
                if deviation < -40:
                    insights.append(Insight(
                        severity="warn",
                        icon="☀",
                        title=f"FV výroba {abs(deviation):.0f}% pod průměrem",
                        detail=f"Dnes {today_pv:.1f} kWh vs průměr 7 dní {baseline:.1f} kWh. Zkontroluj jestli není znečištěný panel nebo neobvyklý stín.",
                        metric="pv_yield",
                        value=today_pv,
                        baseline=baseline,
                        deviation_pct=deviation,
                    ))
                elif deviation < -25:
                    insights.append(Insight(
                        severity="info",
                        icon="☁",
                        title=f"FV výroba {abs(deviation):.0f}% pod průměrem",
                        detail=f"Dnes {today_pv:.1f} kWh vs {baseline:.1f} kWh. Pravděpodobně oblačnost.",
                        metric="pv_yield",
                        value=today_pv,
                        baseline=baseline,
                        deviation_pct=deviation,
                    ))

        # 2. Spotřeba domu - kontrola až po 20h, předtím se pořád akumuluje
        if now.hour >= 20:
            today_cons = self._today.consumption_kwh
            baseline = self._baseline("consumption_kwh")
            if today_cons is not None and baseline is not None and baseline > 0:
                deviation = (today_cons - baseline) / baseline * 100
                if deviation > 50:
                    insights.append(Insight(
                        severity="warn",
                        icon="🏠",
                        title=f"Spotřeba {deviation:.0f}% nad průměrem",
                        detail=f"Dnes {today_cons:.1f} kWh vs {baseline:.1f} kWh. Něco velkého ti běží - zkontroluj sušičku, klima, přímotop.",
                        metric="consumption",
                        value=today_cons,
                        baseline=baseline,
                        deviation_pct=deviation,
                    ))

        # 3. Vířivka - moc spike událostí
        if self._spike_count_today >= 5:
            insights.append(Insight(
                severity="warn",
                icon="⚡",
                title=f"{self._spike_count_today}× spike protection dnes",
                detail="Časté skoky odběru vypínají vířivku. Pravděpodobně ti něco velkého na L1/L3 cykluje (varná konvice, klima, fén). Vířivku to drží zbytečně chladnou.",
                metric="spike_count",
                value=self._spike_count_today,
            ))

        # 4. Spike protection ALE žádný ohřev
        if (self._today.heating_sessions == 0 and now.hour >= 14
                and self._spike_count_today == 0):
            today_pv = self._today.pv_yield_kwh
            if today_pv and today_pv > 20:  # bylo dost slunce
                insights.append(Insight(
                    severity="info",
                    icon="🛁",
                    title="Vířivka dnes netopila",
                    detail=f"FV vyrobilo {today_pv:.1f} kWh, ale vířivka se nezapnula. Možná je voda na cíli, nebo je strategie SURVIVE.",
                    metric="no_heating",
                ))

        # 5. Pozitivní info - sluneční den
        sunny_h = self._today.sunny_hours
        if sunny_h >= 8:
            insights.append(Insight(
                severity="info",
                icon="🌞",
                title=f"Krásný sluneční den ({sunny_h:.1f}h)",
                detail="Plně-sluneční den (Lux > 40k) přes 8 hodin - ideální pro vířivku.",
                metric="sunny_day",
                value=sunny_h,
            ))

        return insights

    def get_history_dict(self) -> List[Dict[str, Any]]:
        """Pro API - vrátí historii jako list dictů."""
        return [asdict(d) for d in self.history]
