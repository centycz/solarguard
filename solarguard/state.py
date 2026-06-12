"""Centralni stav systemu - s daily stats, cleaning programs a appliance hints."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional


class SystemState(Enum):
    IDLE = "idle"
    HEATING = "heating"
    COOLDOWN = "cooldown"
    SPIKE_COOLDOWN = "spike_cool"
    SAFE_MODE = "safe_mode"
    NIGHT_OFF = "night_off"  # v3.4: po zapadu slunce vypnuto, rano se zase spusti


class DayStrategy(Enum):
    AGGRESSIVE = "aggressive"
    NORMAL = "normal"
    CONSERVATIVE = "conservative"
    SURVIVE = "survive"
    UNKNOWN = "unknown"


class CleaningState(Enum):
    """Stav cistici ho programu."""
    IDLE = "idle"
    RUNNING = "running"


@dataclass
class VictronData:
    soc_pct: Optional[float] = None
    pv_power_w: Optional[float] = None
    battery_power_w: Optional[float] = None
    grid_l1_w: Optional[float] = None
    grid_l2_w: Optional[float] = None
    grid_l3_w: Optional[float] = None
    load_l1_w: Optional[float] = None
    load_l2_w: Optional[float] = None
    load_l3_w: Optional[float] = None
    last_update: float = 0.0

    pv_yield_today_kwh: Optional[float] = None
    consumption_today_kwh: Optional[float] = None
    battery_in_today_kwh: Optional[float] = None
    pv_yield_yesterday_kwh: Optional[float] = None
    consumption_yesterday_kwh: Optional[float] = None

    # v4.3.1 NEW: napeti jednotlivych clanku z BMS pres Cerbo GX MQTT
    # {1: 3.314, 2: 3.312, ...} - klic = cislo clanku, hodnota = napeti v V
    cell_voltages: dict = field(default_factory=dict)
    min_cell_voltage_v: Optional[float] = None
    max_cell_voltage_v: Optional[float] = None
    min_cell_id: Optional[str] = None   # ID packu/clanku s nejnizsim napetim (napr. "Pack-01#")
    max_cell_id: Optional[str] = None   # ID packu/clanku s nejvyssim napetim

    @property
    def grid_total_w(self) -> Optional[float]:
        vals = [self.grid_l1_w, self.grid_l2_w, self.grid_l3_w]
        if any(v is None for v in vals): return None
        return sum(vals)

    @property
    def load_total_w(self) -> Optional[float]:
        vals = [self.load_l1_w, self.load_l2_w, self.load_l3_w]
        if any(v is None for v in vals): return None
        return sum(vals)

    @property
    def surplus_w(self) -> Optional[float]:
        pv = self.pv_power_w
        load = self.load_total_w
        if pv is None or load is None: return None
        base = pv - load
        if self.soc_pct is not None and self.soc_pct >= 95:
            bat = self.battery_power_w or 0
            if bat > 0:
                return base + bat
        return base

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_update) > 120


@dataclass
class SpaData:
    current_temp_c: Optional[float] = None
    target_temp_c: Optional[float] = None
    heater_on: Optional[bool] = None
    filter_on: Optional[bool] = None
    bubbles_on: Optional[bool] = None
    jets_on: Optional[bool] = None
    sanitizer_on: Optional[bool] = None          # NEW
    power_on: Optional[bool] = None              # NEW (spa master power)
    online: bool = False
    error_code: Optional[object] = None
    last_update: float = 0.0
    consecutive_failures: int = 0

    @property
    def has_error(self) -> bool:
        return bool(self.error_code)


@dataclass
class EnvironmentData:
    air_temp_c: Optional[float] = None
    light_lux: Optional[float] = None
    wind_kmh: Optional[float] = None
    is_raining: Optional[bool] = None
    last_update: float = 0.0

    @property
    def is_stale(self) -> bool:
        if self.last_update == 0: return True
        return (time.time() - self.last_update) > 300

    @property
    def is_sunny(self) -> bool:
        return self.light_lux is not None and self.light_lux > 40000

    @property
    def is_overcast(self) -> bool:
        return self.light_lux is not None and self.light_lux < 10000


@dataclass
class ForecastData:
    hourly_times: List[str] = field(default_factory=list)
    hourly_radiation: List[float] = field(default_factory=list)
    hourly_temp: List[float] = field(default_factory=list)
    hourly_cloudcover: List[float] = field(default_factory=list)
    hourly_rain_prob: List[float] = field(default_factory=list)

    predicted_pv_kwh_today: Optional[float] = None
    predicted_pv_kwh_remaining: Optional[float] = None
    sunrise: Optional[str] = None
    sunset: Optional[str] = None

    last_update: float = 0.0

    @property
    def is_stale(self) -> bool:
        if self.last_update == 0: return True
        return (time.time() - self.last_update) > 14400


@dataclass
class DailyPlan:
    strategy: DayStrategy = DayStrategy.UNKNOWN
    predicted_pv_kwh: Optional[float] = None
    battery_available_kwh: Optional[float] = None
    baseline_consumption_kwh: float = 12.0
    discretionary_kwh: Optional[float] = None
    dynamic_surplus_on_w: Optional[float] = None
    dynamic_surplus_off_w: Optional[float] = None
    computed_at: float = 0.0
    reason: str = ""


@dataclass
class CleaningProgram:
    """Cistici program - timer s auto-vypnutim po X hodinach.

    Typicke programy:
      3h - lehke osvezeni (napr. po prchazce na pravidelne udrzbou)
      5h - standardni cisteni (napr. po par dnech nepouzivani)
      8h - hluboke cisteni (napr. tydenni udrzba nebo po delsi pauze)
    """
    state: CleaningState = CleaningState.IDLE
    started_at: float = 0.0
    duration_hours: float = 0.0
    # co se pri startu udelalo (aby engine vedel, jestli se ma vratit stav)
    filter_was_on: Optional[bool] = None
    sanitizer_was_on: Optional[bool] = None
    last_tick: float = 0.0
    # pocet chyb pri pokusu o zapnuti / vypnuti komponent (pro healthcheck)
    errors: int = 0

    @property
    def is_running(self) -> bool:
        return self.state == CleaningState.RUNNING

    @property
    def elapsed_sec(self) -> float:
        if not self.is_running or self.started_at == 0:
            return 0.0
        return time.time() - self.started_at

    @property
    def remaining_sec(self) -> float:
        if not self.is_running:
            return 0.0
        total = self.duration_hours * 3600.0
        rem = total - self.elapsed_sec
        return max(0.0, rem)

    @property
    def progress_pct(self) -> float:
        if not self.is_running or self.duration_hours <= 0:
            return 0.0
        total = self.duration_hours * 3600.0
        return min(100.0, (self.elapsed_sec / total) * 100.0)

    @property
    def ends_at(self) -> Optional[float]:
        if not self.is_running:
            return None
        return self.started_at + self.duration_hours * 3600.0


@dataclass
class EnergyTracker:
    """Skutecna spotreba behem bezici session (od startu)."""
    session_start: float = field(default_factory=time.time)
    last_sample_time: float = 0.0
    pv_produced_kwh: float = 0.0
    home_consumed_kwh: float = 0.0
    battery_charged_kwh: float = 0.0
    battery_discharged_kwh: float = 0.0
    grid_in_kwh: float = 0.0
    grid_out_kwh: float = 0.0

    def update(self, v: VictronData) -> None:
        now = time.time()
        if self.last_sample_time == 0:
            self.last_sample_time = now
            return
        dt_h = (now - self.last_sample_time) / 3600.0
        if dt_h <= 0 or dt_h > 0.1:
            self.last_sample_time = now
            return
        if v.pv_power_w is not None:
            self.pv_produced_kwh += max(0, v.pv_power_w) * dt_h / 1000.0
        if v.load_total_w is not None:
            self.home_consumed_kwh += max(0, v.load_total_w) * dt_h / 1000.0
        if v.battery_power_w is not None:
            if v.battery_power_w > 0:
                self.battery_charged_kwh += v.battery_power_w * dt_h / 1000.0
            else:
                self.battery_discharged_kwh += abs(v.battery_power_w) * dt_h / 1000.0
        if v.grid_total_w is not None:
            if v.grid_total_w > 0:
                self.grid_in_kwh += v.grid_total_w * dt_h / 1000.0
            else:
                self.grid_out_kwh += abs(v.grid_total_w) * dt_h / 1000.0
        self.last_sample_time = now


@dataclass
class SpotPriceData:
    """v3.6 NEW: Spotove ceny elektriny z OTE-CR API.

    Hodinove ceny za dnesek + zitra (zitrejsi se publikuji ~14:00).
    EUR/MWh -> Kc/kWh: cena_eur * eur_to_kc / 1000 + distribucni_poplatek
    """
    today_prices_eur: List[float] = field(default_factory=list)
    today_date: Optional[str] = None
    tomorrow_prices_eur: List[float] = field(default_factory=list)
    tomorrow_date: Optional[str] = None
    eur_to_kc: float = 25.0
    fee_kc_per_kwh: float = 1.5
    last_update: float = 0.0

    @property
    def is_stale(self) -> bool:
        if self.last_update == 0: return True
        return (time.time() - self.last_update) > 14400

    def current_price_kc(self, with_fee: bool = True) -> Optional[float]:
        """Aktualni cena teto hodiny (Kc/kWh).
        with_fee=True (default): + distribucni poplatek
        with_fee=False: ciste spot z OTE (jen energie)
        """
        if not self.today_prices_eur:
            return None
        from datetime import datetime as _dt
        hour = _dt.now().hour
        if hour >= len(self.today_prices_eur):
            return None
        eur_mwh = self.today_prices_eur[hour]
        base = (eur_mwh * self.eur_to_kc) / 1000.0
        return round(base + (self.fee_kc_per_kwh if with_fee else 0), 2)

    def best_hours_today(self, count: int = 4) -> List[int]:
        """Vrati 'count' hodin s nejlevnejsim spotem (od ted do konce dne)."""
        if not self.today_prices_eur:
            return []
        from datetime import datetime as _dt
        now_hour = _dt.now().hour
        candidates = [(p, i) for i, p in enumerate(self.today_prices_eur) if i >= now_hour]
        if not candidates:
            return []
        candidates.sort(key=lambda x: x[0])
        return sorted([h for _, h in candidates[:count]])

    def hourly_prices_kc(self, with_fee: bool = True) -> List[float]:
        """Vsechny dnesni hodinove ceny v Kc/kWh."""
        fee = self.fee_kc_per_kwh if with_fee else 0
        return [
            round((p * self.eur_to_kc) / 1000.0 + fee, 2)
            for p in self.today_prices_eur
        ]


@dataclass
class SeplosData:
    """v4.3.2 NEW: Seplos BMS V3.0 - data z RS485 Modbus RTU.

    Obsahuje napeti vsech clanku ze vsech pack - neco co Cerbo GX pres CAN neposila.
    pack_cell_voltages[pack_idx][cell_idx] = napeti v V.
    """
    online: bool = False
    last_update: float = 0.0

    pack_cell_voltages: list = field(default_factory=list)   # [[3.322, ...], [...]]
    pack_temperatures: list = field(default_factory=list)    # [[25.1, 26.2, ...], ...]
    pack_voltages: list = field(default_factory=list)        # [53.70, 53.71]
    pack_currents: list = field(default_factory=list)        # [-16.5, -16.5]
    pack_soc: list = field(default_factory=list)             # [98.0, 98.0]
    pack_soh: list = field(default_factory=list)             # [100.0, 100.0]
    # v4.3.2 NEW: aktivni balancing flags z PIC bloku (0x1200, bity 96-111)
    # pack_balance_flags[pack_idx][cell_idx] = True kdyz BMS prave bleed-uje tu bunku
    pack_balance_flags: list = field(default_factory=list)   # [[False, True, ...], [...]]

    min_cell_voltage: Optional[float] = None
    max_cell_voltage: Optional[float] = None
    min_cell_pack:    Optional[int]   = None   # 1-based
    min_cell_index:   Optional[int]   = None   # 1-based
    max_cell_pack:    Optional[int]   = None
    max_cell_index:   Optional[int]   = None

    @property
    def is_stale(self) -> bool:
        if self.last_update == 0: return True
        return (time.time() - self.last_update) > 120

    @property
    def all_cells_flat(self) -> list:
        """[{pack, cell, v}, ...] serazeno pack->cell, filtruje nulova napeti."""
        result = []
        for pi, cells in enumerate(self.pack_cell_voltages):
            for ci, v in enumerate(cells):
                if v and v > 0:
                    result.append({'pack': pi + 1, 'cell': ci + 1, 'v': round(v, 4)})
        return result


def _make_preshower():
    """Lazy import aby nedoslo k cyklickemu importu (preshower.py importuje state)."""
    from .engine.preshower import PreShowerProgram
    return PreShowerProgram()


@dataclass
class HeatPumpData:
    """v4.2.0 NEW: Tepelne cerpadlo IVT AIR X 70 + Airmodul E9 pres Husdata H66.

    Data citana z H66 MQTT topiku +/HP/{regId}. Konkretni registry zavisi
    na typu Rega regulatoru (Rego1000 / Rego2000-3000 pro Bosch/IVT).

    Po fyzicke instalaci H66 si v jeho web interfacu zjistime presne ID
    registru pro tuto pumpu a doplnime do config.yaml. Tato dataclass je
    obecna - drzi vsechny zname promenne ktere by mohly byt zajimave.
    """
    online: bool = False
    last_update: float = 0.0
    consecutive_failures: int = 0

    # Provozni stav
    operating_mode: Optional[str] = None  # "heat" | "cool" | "hot_water" | "off"
    compressor_running: Optional[bool] = None
    additional_heater_active: Optional[bool] = None  # elektricky dohrev
    fan_speed: Optional[float] = None     # 0-100% nebo RPM podle modelu

    # Teploty
    outdoor_temp_c: Optional[float] = None
    indoor_temp_c: Optional[float] = None
    supply_temp_c: Optional[float] = None      # vystupni voda do topneho okruhu
    return_temp_c: Optional[float] = None      # zpatecka
    hot_water_temp_c: Optional[float] = None   # TUV
    target_room_temp_c: Optional[float] = None
    target_supply_temp_c: Optional[float] = None
    target_hot_water_temp_c: Optional[float] = None

    # Energie
    power_consumption_w: Optional[float] = None  # aktualni spotreba
    energy_today_kwh: Optional[float] = None     # dneska spotrebovano
    cop_estimated: Optional[float] = None        # vypocteny COP

    # Alarmy
    alarm_active: Optional[bool] = None
    alarm_code: Optional[str] = None

    # Nastavovaci registry (zname v softu, bez pristupu na pumpu mohou byt None)
    heating_curve: Optional[float] = None
    additional_heater_blocked: Optional[bool] = None  # blokace elektrickeho dohrevu

    # Manualni control flagy ze SolarGuardu (pro UI a logiku)
    manual_override: bool = False
    manual_override_reason: str = ""

    @property
    def is_stale(self) -> bool:
        """Vic nez 5 minut bez updatu = stale."""
        if self.last_update == 0: return True
        return (time.time() - self.last_update) > 300

    @property
    def has_alarm(self) -> bool:
        return bool(self.alarm_active or self.alarm_code)


@dataclass
class SystemContext:
    victron: VictronData = field(default_factory=VictronData)
    spa: SpaData = field(default_factory=SpaData)
    env: EnvironmentData = field(default_factory=EnvironmentData)
    forecast: ForecastData = field(default_factory=ForecastData)
    plan: DailyPlan = field(default_factory=DailyPlan)
    energy: EnergyTracker = field(default_factory=EnergyTracker)
    cleaning: CleaningProgram = field(default_factory=CleaningProgram)   # NEW
    spot: SpotPriceData = field(default_factory=SpotPriceData)           # v3.6 NEW
    preshower: object = field(default_factory=_make_preshower)           # v3.7 NEW
    heatpump: HeatPumpData = field(default_factory=HeatPumpData)          # v4.2.0 NEW
    seplos: SeplosData = field(default_factory=SeplosData)                # v4.3.2 NEW

    current_state: SystemState = SystemState.IDLE
    state_entered_at: float = field(default_factory=time.time)
    cooldown_until: float = 0.0

    override_active: bool = False
    override_reason: str = ""

    # v4.1.5 FIX: current_scene byl dynamicky nastavovan pres ctx.current_scene = "..."
    # ale nebyl v dataclass -> AttributeError pri prvni decide() po startu
    # Default "solar_auto" = automaticke rizeni podle FVE prebytku
    current_scene: str = "solar_auto"

    # v3.8.1 NEW: kdy uzivatel rucne zapnul topeni pres web/scenu.
    # Spike detection bude tento moment respektovat stejne jako normalni start
    # (ignoruje vlastni startup po dobu spike_ignore_window_sec).
    manual_heater_started_at: float = 0.0

    # v4.4.0 NEW: rucni vypnuti topeni = docasny hold automatiky.
    # Dokud time.time() < manual_heater_off_until, engine sam nezapne topeni.
    # Vyprsi samo, nebo ho zrusi vyber sceny / rucni zapnuti topeni.
    manual_heater_off_until: float = 0.0

    # v4.4.0 NEW: auto-expirace heat_now overridu.
    # override_started_at = kdy byl heat_now aktivovan (web/schedule)
    # heat_now_target_reached_at = kdy voda poprve dosahla cile (one-way latch)
    override_started_at: float = 0.0
    heat_now_target_reached_at: float = 0.0

    load_history: Deque[tuple] = field(default_factory=lambda: deque(maxlen=120))
    surplus_history: Deque[tuple] = field(default_factory=lambda: deque(maxlen=180))
    # v3.4 NEW: per-phase load history pro presnejsi spike detection
    load_l1_history: Deque[tuple] = field(default_factory=lambda: deque(maxlen=120))
    load_l2_history: Deque[tuple] = field(default_factory=lambda: deque(maxlen=120))
    load_l3_history: Deque[tuple] = field(default_factory=lambda: deque(maxlen=120))

    def time_in_state(self) -> float:
        return time.time() - self.state_entered_at

    def transition(self, new_state: SystemState, reason: str = "") -> None:
        if new_state != self.current_state:
            self.current_state = new_state
            self.state_entered_at = time.time()
