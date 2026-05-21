"""
v3.7 NEW: Heating curve - uci se rychlost ohrevu virivky.

Po kazdem ukoncenem ohrevu (z X°C na Y°C) ulozi zaznam:
  - start_temp, end_temp, time_minutes
  - air_temp_c, wind_kmh, has_cover

Po 5+ zaznamech zacne predikovat: kolik minut bude trvat ohrev z A na B
za soucasnych venkovnich podminek.

Model: jednoduchá linearni regrese (multivariable least squares).
  time_per_degree = base_rate + air_coef * air_temp + wind_coef * wind

Defaultni hodnoty (bez dat) odpovidaji typicke nafukovaci virivce:
  ~12 minut na 1°C pri vnejsi 15°C bez vetru.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("heating_curve")


@dataclass
class HeatingSample:
    timestamp: float
    start_temp: float
    end_temp: float
    duration_minutes: float
    air_temp_c: Optional[float] = None
    wind_kmh: Optional[float] = None

    @property
    def temp_delta(self) -> float:
        return self.end_temp - self.start_temp

    @property
    def minutes_per_degree(self) -> float:
        if self.temp_delta <= 0: return 999
        return self.duration_minutes / self.temp_delta

    @property
    def is_valid(self) -> bool:
        # Filtruj nesmysly - rozumne meze pro 1000L+ virivku
        return (
            self.temp_delta >= 0.5
            and self.duration_minutes >= 15
            and self.duration_minutes <= 900   # 15h max je realne pro velkou virivku v zime
            and 15 <= self.minutes_per_degree <= 200
        )


@dataclass
class HeatingCurveModel:
    """Model ohrevu = fyzikalni baseline + ucici se korekce z dat.

    PHYSICS:
        Q = m * c * dT      ... energie na ohrev (J)
        m = volume_l * 1.0  ... voda 1 kg/L
        c = 4186 J/(kg·°C)
        time = Q / (heater_power_W * efficiency)

    Pro Intex 28462 (1098L), heater 2200W, 60% real efficiency:
        baseline = 4186 * 1098 / (2200 * 0.6) / 60 = 58 min/°C
        S vetrem a chladem se to zhorsuje.

    Po 5+ vzorcich z reality korigujeme baseline (rychlejsi/pomalejsi nez fyzika).
    Po 20+ vzorcich linearni regrese pro vliv air_temp a wind.
    """
    # Fyzikalni parametry virivky (z config)
    volume_l: float = 1098.0          # Intex 28462 default
    heater_power_w: float = 2200.0    # nominal
    base_efficiency: float = 0.6       # realne ucinost (ztraty 40%)

    # Korekce z naucenych dat (zacatek = 0, po datech se nastavi)
    learned_correction: float = 1.0    # multiplikator k fyzikalnimu baseline
    air_coef: float = -1.5             # za kazdy 1°C tepleji venku ubere X min/°C
    wind_coef: float = 0.5             # za kazdy 1 km/h vetru pridava X min/°C
    n_samples: int = 0
    last_train: float = 0.0

    @property
    def physical_baseline_min_per_deg(self) -> float:
        """Cas v minutach na ohrev 1°C podle fyziky.

        time_sec = m * c * 1 / (P * eff)
        time_min = time_sec / 60
        """
        if self.heater_power_w <= 0 or self.base_efficiency <= 0:
            return 60.0  # safe fallback
        time_sec = (self.volume_l * 4186 * 1.0) / (self.heater_power_w * self.base_efficiency)
        return time_sec / 60.0

    @property
    def base_min_per_deg(self) -> float:
        """Aktualni baseline = fyzika * naucena korekce."""
        return self.physical_baseline_min_per_deg * self.learned_correction

    def predict_minutes(
        self,
        delta_c: float,
        air_temp_c: Optional[float],
        wind_kmh: Optional[float],
    ) -> float:
        if delta_c <= 0:
            return 0.0
        air = air_temp_c if air_temp_c is not None else 15.0
        wind = wind_kmh if wind_kmh is not None else 5.0
        rate = self.base_min_per_deg + self.air_coef * (air - 15.0) + self.wind_coef * wind
        # Clamp - vetsi rozsah pro velkou virivku
        rate = max(15.0, min(rate, 200.0))
        return rate * delta_c


class HeatingCurveTracker:
    """Sleduje aktualni ohrev a po dokonceni ulozi vzorek do historie."""

    def __init__(
        self,
        log_dir: str,
        history_file: str = "heating_history.jsonl",
        volume_l: float = 1098.0,
        heater_power_w: float = 2200.0,
        base_efficiency: float = 0.6,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.log_dir / history_file

        # Aktuální ohřev (None pokud netopí)
        self.current_start_temp: Optional[float] = None
        self.current_start_time: Optional[float] = None
        self.current_air_temp: Optional[float] = None
        self.current_wind: Optional[float] = None
        self.current_target: Optional[float] = None

        self.samples: List[HeatingSample] = self._load_history()
        self.model = HeatingCurveModel(
            volume_l=volume_l,
            heater_power_w=heater_power_w,
            base_efficiency=base_efficiency,
        )
        log.info(
            f"Heating curve init: volume={volume_l}L, heater={heater_power_w}W, "
            f"physical baseline = {self.model.physical_baseline_min_per_deg:.1f} min/°C"
        )
        self._train_model()

    def _load_history(self) -> List[HeatingSample]:
        samples = []
        if not self.history_path.exists():
            return samples
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        d = json.loads(line)
                        s = HeatingSample(**d)
                        if s.is_valid:
                            samples.append(s)
                    except (json.JSONDecodeError, TypeError) as e:
                        log.debug(f"skip invalid history line: {e}")
        except Exception as e:
            log.error(f"failed to load heating history: {e}")
        log.info(f"Heating curve: loaded {len(samples)} historical samples")
        return samples

    def _train_model(self) -> None:
        """Updatuje self.model: ucici korekce a koeficienty z dat.

        Pri 5-19 vzorcich: spocti learned_correction = avg(real) / physical_baseline
        Pri 20+ vzorcich: linearni regrese pro air_coef a wind_coef
        """
        n = len(self.samples)
        self.model.n_samples = n
        self.model.last_train = time.time()

        physical_base = self.model.physical_baseline_min_per_deg

        if n < 5:
            return  # default = fyzika

        # Average real minutes per degree
        rates = [s.minutes_per_degree for s in self.samples]
        avg_rate = sum(rates) / len(rates)

        # Korekce: jak moc se realita lisi od fyzikalniho baseline
        # < 1.0 = realita rychlejsi nez fyzika (asi mala ztrata, dobra izolace)
        # > 1.0 = realita pomalejsi (vetsi ztraty, kryt nedosedl, vitr)
        correction = avg_rate / physical_base if physical_base > 0 else 1.0
        # Clamp na rozumne meze
        correction = max(0.5, min(2.5, correction))
        self.model.learned_correction = correction

        if n < 20:
            log.info(
                f"Heating curve trained (n={n}): "
                f"physical_base={physical_base:.1f} min/°C, "
                f"learned_correction={correction:.2f} "
                f"(avg real {avg_rate:.1f} min/°C)"
            )
            return

        # 20+ vzorku: regrese pro air a wind koeficienty
        try:
            ys = []
            air_xs = []
            wind_xs = []
            for s in self.samples:
                if s.air_temp_c is None or s.wind_kmh is None:
                    continue
                # Cilova promenna: residuum vuci baseline (po korekci)
                # Tj. kolik navic/minus oproti `correction * physical_base`
                expected = self.model.learned_correction * physical_base
                residual = s.minutes_per_degree - expected
                ys.append(residual)
                air_xs.append(s.air_temp_c)
                wind_xs.append(s.wind_kmh)

            if len(ys) < 20:
                return

            n_y = len(ys)
            mean_y = sum(ys) / n_y
            mean_air = sum(air_xs) / n_y
            mean_wind = sum(wind_xs) / n_y

            num_air = sum((air_xs[i] - mean_air) * (ys[i] - mean_y) for i in range(n_y))
            den_air = sum((air_xs[i] - mean_air) ** 2 for i in range(n_y))
            air_coef = num_air / den_air if den_air > 0 else -1.5

            num_wind = sum((wind_xs[i] - mean_wind) * (ys[i] - mean_y) for i in range(n_y))
            den_wind = sum((wind_xs[i] - mean_wind) ** 2 for i in range(n_y))
            wind_coef = num_wind / den_wind if den_wind > 0 else 0.5

            # Clamp - vetsi virivka ma vetsi vliv pocasi
            air_coef = max(-3.0, min(0.0, air_coef))
            wind_coef = max(0.0, min(2.0, wind_coef))

            self.model.air_coef = air_coef
            self.model.wind_coef = wind_coef
            log.info(
                f"Heating curve trained (n={n}, regrese n={n_y}): "
                f"phys={physical_base:.1f} corr={self.model.learned_correction:.2f} "
                f"-> base={self.model.base_min_per_deg:.1f} min/°C, "
                f"air_coef={air_coef:+.2f}, wind_coef={wind_coef:+.2f}"
            )
        except Exception as e:
            log.error(f"regrese failed: {e}")

    def on_heating_start(self, current_temp: float, target_temp: float,
                         air_temp: Optional[float], wind_kmh: Optional[float]) -> None:
        """Volat kdyz zapne topeni. Zaznamena startovni stav."""
        self.current_start_temp = current_temp
        self.current_start_time = time.time()
        self.current_air_temp = air_temp
        self.current_wind = wind_kmh
        self.current_target = target_temp
        log.info(
            f"Heating started: {current_temp}°C -> {target_temp}°C "
            f"(air={air_temp}, wind={wind_kmh})"
        )

    def on_heating_stop(self, current_temp: float, reason: str = "") -> Optional[HeatingSample]:
        """Volat kdyz topeni dosahne cile (nebo se vypne).

        Pokud:
        - jsme topili dost dlouho (10+ min)
        - delta byla aspon 1°C
        ulozime sample do historie a re-trenujeme model.
        """
        if self.current_start_temp is None or self.current_start_time is None:
            return None

        end_temp = current_temp
        delta = end_temp - self.current_start_temp
        duration_min = (time.time() - self.current_start_time) / 60.0

        sample = HeatingSample(
            timestamp=self.current_start_time,
            start_temp=self.current_start_temp,
            end_temp=end_temp,
            duration_minutes=duration_min,
            air_temp_c=self.current_air_temp,
            wind_kmh=self.current_wind,
        )

        if sample.is_valid:
            self.samples.append(sample)
            try:
                with open(self.history_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
            except Exception as e:
                log.error(f"failed to write history: {e}")
            self._train_model()
            log.info(
                f"Heating sample recorded: Δ{delta:+.1f}°C in {duration_min:.0f} min "
                f"({sample.minutes_per_degree:.1f} min/°C). "
                f"Total samples: {len(self.samples)} ({reason})"
            )
        else:
            log.debug(
                f"Heating sample SKIPPED (invalid): "
                f"Δ{delta:+.1f}°C in {duration_min:.0f} min ({reason})"
            )

        # Reset current
        self.current_start_temp = None
        self.current_start_time = None
        self.current_air_temp = None
        self.current_wind = None
        self.current_target = None
        return sample if sample.is_valid else None

    def predict_to_target(
        self,
        current_temp: float,
        target_temp: float,
        air_temp_c: Optional[float],
        wind_kmh: Optional[float],
    ) -> dict:
        """Predict pro UI: kolik minut + cas dokonceni."""
        delta = max(0, target_temp - current_temp)
        if delta <= 0:
            return {
                "delta_c": 0,
                "minutes": 0,
                "eta_iso": None,
                "model": self.get_model_info(),
                "in_progress": False,
            }
        minutes = self.model.predict_minutes(delta, air_temp_c, wind_kmh)
        eta = time.time() + minutes * 60
        from datetime import datetime
        return {
            "delta_c": round(delta, 1),
            "minutes": round(minutes, 0),
            "eta_iso": datetime.fromtimestamp(eta).strftime("%H:%M"),
            "model": self.get_model_info(),
            "in_progress": self.current_start_time is not None,
        }

    def get_model_info(self) -> dict:
        confidence = "low"
        if self.model.n_samples >= 20:
            confidence = "high"
        elif self.model.n_samples >= 5:
            confidence = "medium"
        return {
            "n_samples": self.model.n_samples,
            "volume_l": self.model.volume_l,
            "heater_power_w": self.model.heater_power_w,
            "base_efficiency": self.model.base_efficiency,
            "physical_baseline_min_per_deg": round(self.model.physical_baseline_min_per_deg, 1),
            "learned_correction": round(self.model.learned_correction, 2),
            "base_min_per_deg": round(self.model.base_min_per_deg, 1),
            "air_coef": round(self.model.air_coef, 2),
            "wind_coef": round(self.model.wind_coef, 2),
            "confidence": confidence,
            "last_train_iso": time.strftime("%H:%M", time.localtime(self.model.last_train)) if self.model.last_train else None,
        }

    def get_recent_samples(self, n: int = 10) -> List[dict]:
        recent = self.samples[-n:] if len(self.samples) > n else self.samples[:]
        return [asdict(s) for s in reversed(recent)]
