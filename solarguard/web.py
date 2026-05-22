"""Web dashboard pro SolarGuard v3.7 - heating curve + pre-shower mode."""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .state import SystemContext
from .auth import (
    require_auth, check_token, get_config as get_auth_config,
    get_user, ROLE_OWNER, ROLE_FAMILY, ROLE_GUEST, VALID_ROLES,
)

log = logging.getLogger("web")


# Cesta ke statickym souborum (manifest, SW, ikony)
STATIC_DIR = Path(__file__).parent / "static"

# v4.3.2 NEW: cesta pro snapshoty napeti clanku pri SOC 99% a 20% (persistence pres restarty)
SNAPSHOT_PATH = Path("data") / "cell_snapshots.jsonl"
SNAPSHOT_FULL_THRESHOLD = 99.0   # SOC% - kdyz vystoupa nad, ulozi snapshot "FULL"
SNAPSHOT_LOW_THRESHOLD  = 20.0   # SOC% - kdyz klesne pod, ulozi snapshot "LOW"
SNAPSHOT_HYSTERESIS     = 5.0    # vrat SOC zpet pres tuto hranici neez znova logovat
SNAPSHOT_MAX_RECORDS    = 100    # in-memory deque cap; soubor neorezavame

tick_history: Deque[Dict[str, Any]] = deque(maxlen=3000)
event_history: Deque[Dict[str, Any]] = deque(maxlen=500)

# v4.3.2 NEW: in-memory cache snapshotu napeti clanku (load z JSONL pri startu)
_seplos_snapshots: Deque[Dict[str, Any]] = deque(maxlen=SNAPSHOT_MAX_RECORDS)
_seplos_last_soc: Optional[float] = None  # pro detekci prechodu pres hranice
_seplos_full_armed: bool = True   # pripraveny logovat FULL? (False kdyz uz logovano, dokud SOC neklesne pod 99-hyst)
_seplos_low_armed: bool = True    # pripraveny logovat LOW?


def _load_snapshots_from_disk() -> None:
    """Pri startu nahraj posledni snapshoty z JSONL souboru do in-memory cache."""
    global _seplos_snapshots
    try:
        if not SNAPSHOT_PATH.exists():
            return
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        # Vezmi jen posledni N
        for line in lines[-SNAPSHOT_MAX_RECORDS:]:
            line = line.strip()
            if not line:
                continue
            try:
                import json as _json
                rec = _json.loads(line)
                _seplos_snapshots.append(rec)
            except Exception:
                pass
        log.info(f"Loaded {len(_seplos_snapshots)} cell snapshots from {SNAPSHOT_PATH}")
    except Exception as e:
        log.warning(f"Snapshot load failed: {e}")


def _write_snapshot(snapshot: dict) -> None:
    """Append snapshot do JSONL a in-memory cache."""
    global _seplos_snapshots
    try:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        with open(SNAPSHOT_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(snapshot, ensure_ascii=False) + "\n")
        _seplos_snapshots.append(snapshot)
        log.info(f"Cell snapshot saved: {snapshot.get('type')} @ SOC {snapshot.get('soc'):.1f}%")
    except Exception as e:
        log.warning(f"Snapshot write failed: {e}")


def _check_snapshot(ctx) -> None:
    """Detekuje prechody SOC pres FULL/LOW hranice a ulozi snapshot napeti vsech clanku.

    FULL snapshot: SOC vystoupa nad SNAPSHOT_FULL_THRESHOLD (99%).
    LOW snapshot:  SOC klesne pod SNAPSHOT_LOW_THRESHOLD (20%).
    Hystereze: dalsi snapshot stejneho typu az kdyz se SOC vrati o SNAPSHOT_HYSTERESIS od hranice.
    """
    global _seplos_last_soc, _seplos_full_armed, _seplos_low_armed
    try:
        sep = ctx.seplos
        soc = ctx.victron.soc_pct
        if soc is None or sep is None or not sep.online or not sep.pack_cell_voltages:
            return

        # Hystereze - rearm flagy
        if soc < SNAPSHOT_FULL_THRESHOLD - SNAPSHOT_HYSTERESIS:
            _seplos_full_armed = True
        if soc > SNAPSHOT_LOW_THRESHOLD + SNAPSHOT_HYSTERESIS:
            _seplos_low_armed = True

        # Detekce prechodu
        trigger_type = None
        if soc >= SNAPSHOT_FULL_THRESHOLD and _seplos_full_armed:
            trigger_type = "FULL"
            _seplos_full_armed = False
        elif soc <= SNAPSHOT_LOW_THRESHOLD and _seplos_low_armed:
            trigger_type = "LOW"
            _seplos_low_armed = False

        _seplos_last_soc = soc
        if trigger_type is None:
            return

        # Sestav snapshot
        flat = sep.all_cells_flat if hasattr(sep, "all_cells_flat") else []
        snapshot = {
            "ts": time.time(),
            "type": trigger_type,
            "soc": soc,
            "battery_power_w": ctx.victron.battery_power_w,
            "pack_count": len(sep.pack_cell_voltages),
            "cells_per_pack": len(sep.pack_cell_voltages[0]) if sep.pack_cell_voltages else 0,
            "all_cells": flat,
            "pack_voltages": list(sep.pack_voltages) if sep.pack_voltages else [],
            "pack_temperatures": list(sep.pack_temperatures) if sep.pack_temperatures else [],
            "min_cell_voltage": sep.min_cell_voltage,
            "max_cell_voltage": sep.max_cell_voltage,
            "min_cell_pack": sep.min_cell_pack,
            "min_cell_index": sep.min_cell_index,
            "max_cell_pack": sep.max_cell_pack,
            "max_cell_index": sep.max_cell_index,
            "spread_mv": round((sep.max_cell_voltage - sep.min_cell_voltage) * 1000, 1)
                if sep.max_cell_voltage and sep.min_cell_voltage else None,
        }
        _write_snapshot(snapshot)
    except Exception as e:
        log.warning(f"_check_snapshot error: {e}")


_spa_controller = None
_cleaning_manager = None
_appliance_evaluator = None
_scheduler = None
_heating_curve = None
_preshower = None
_influx = None
_anomaly = None
_digest = None
_config_ref = None
# v4.3.0 NEW: heat pump
_heatpump_controller = None
_heatpump_engine = None
# v4.3.0 NEW: appliance learning manager
_learning_manager = None
_startup_time = time.time()  # v3.9 NEW: pro /healthz uptime


def set_spa_controller(spa):
    global _spa_controller; _spa_controller = spa

def set_cleaning_manager(cm):
    global _cleaning_manager; _cleaning_manager = cm

def set_appliance_evaluator(ev):
    global _appliance_evaluator; _appliance_evaluator = ev

def set_scheduler(sch):
    global _scheduler; _scheduler = sch

def set_heating_curve(hc):
    global _heating_curve; _heating_curve = hc

def set_preshower(ps):
    global _preshower; _preshower = ps

def set_influx(infl):
    global _influx; _influx = infl

def set_anomaly(a):
    global _anomaly; _anomaly = a

def set_digest(d):
    global _digest; _digest = d

def set_config_ref(cfg):
    global _config_ref; _config_ref = cfg

# v4.3.0 NEW: heat pump setters
def set_heatpump_controller(hp):
    global _heatpump_controller; _heatpump_controller = hp

def set_heatpump_engine(eng):
    global _heatpump_engine; _heatpump_engine = eng

# v4.3.0 NEW: appliance learning
def set_learning_manager(lm):
    global _learning_manager; _learning_manager = lm


# v4.3.2 NEW: vrati nejcastejsi (pack, cell) ktery byl min nebo max za poslednich N hodin
def _seplos_extreme_24h(field_pack: str, field_cell: str, hours: float = 24.0) -> Optional[dict]:
    cutoff = time.time() - hours * 3600
    counts: Dict[tuple, int] = {}
    total = 0
    for tick in tick_history:
        if tick.get("ts", 0) < cutoff:
            continue
        pk = tick.get(field_pack)
        cl = tick.get(field_cell)
        if pk is None or cl is None:
            continue
        key = (pk, cl)
        counts[key] = counts.get(key, 0) + 1
        total += 1
    if not counts or total == 0:
        return None
    (pk, cl), cnt = max(counts.items(), key=lambda kv: kv[1])
    return {"pack": pk, "cell": cl, "count": cnt, "total_samples": total, "pct": round(cnt / total * 100, 1)}


# v4.3.2 NEW: Seplos BMS data serializace pro JSON API (nikdy nehazi vyjimku)
def _seplos_json(s) -> dict:
    try:
        if s is None:
            return {"enabled": False}
        flat = s.all_cells_flat if hasattr(s, 'all_cells_flat') else []
        return {
            "enabled": True,
            "online": bool(getattr(s, 'online', False)),
            "pack_count": len(s.pack_cell_voltages) if s.pack_cell_voltages else 0,
            "cells_per_pack": len(s.pack_cell_voltages[0]) if s.pack_cell_voltages else 0,
            "all_cells": flat,
            "pack_voltages": list(s.pack_voltages) if s.pack_voltages else [],
            "pack_currents": list(s.pack_currents) if s.pack_currents else [],
            "pack_soc": list(s.pack_soc) if s.pack_soc else [],
            "pack_temperatures": list(s.pack_temperatures) if s.pack_temperatures else [],
            "min_cell_voltage": s.min_cell_voltage,
            "max_cell_voltage": s.max_cell_voltage,
            "min_cell_pack": s.min_cell_pack,
            "min_cell_index": s.min_cell_index,
            "max_cell_pack": s.max_cell_pack,
            "max_cell_index": s.max_cell_index,
            # v4.3.2 NEW: Stitek hanby - kdo byl nejcasteji min/max za 24h
            "weakest_24h": _seplos_extreme_24h("sep_min_pack", "sep_min_cell", 24.0),
            "strongest_24h": _seplos_extreme_24h("sep_max_pack", "sep_max_cell", 24.0),
        }
    except Exception as e:
        log.warning(f"_seplos_json error: {e}")
        return {"enabled": False, "error": str(e)}


def record_tick(ctx: SystemContext, decision_reason: str = "") -> None:
    v = ctx.victron; s = ctx.spa; e = ctx.env; p = ctx.plan; c = ctx.cleaning
    entry = {
        "ts": time.time(), "state": ctx.current_state.value,
        "soc": v.soc_pct, "pv": v.pv_power_w, "surplus": v.surplus_w,
        "load": v.load_total_w, "battery_power": v.battery_power_w, "grid": v.grid_total_w,
        "water_temp": s.current_temp_c, "target_temp": s.target_temp_c,
        "heater": s.heater_on, "filter": s.filter_on, "sanitizer": s.sanitizer_on,
        "error": s.error_code, "reason": decision_reason,
        "air_temp": e.air_temp_c, "light_lux": e.light_lux,
        "wind_kmh": e.wind_kmh, "is_raining": e.is_raining,
        "strategy": p.strategy.value if p.strategy else None,
        "cleaning_running": c.is_running,
    }
    # v4.3.2 NEW: Seplos snapshot do historie - pro graf spreadu a "weakest cell"
    try:
        sep = ctx.seplos
        if sep and sep.online and sep.min_cell_voltage is not None and sep.max_cell_voltage is not None:
            entry["sep_min_v"] = round(sep.min_cell_voltage, 4)
            entry["sep_max_v"] = round(sep.max_cell_voltage, 4)
            entry["sep_spread_mv"] = round((sep.max_cell_voltage - sep.min_cell_voltage) * 1000, 1)
            entry["sep_min_pack"] = sep.min_cell_pack
            entry["sep_min_cell"] = sep.min_cell_index
            entry["sep_max_pack"] = sep.max_cell_pack
            entry["sep_max_cell"] = sep.max_cell_index
    except Exception:
        pass
    tick_history.append(entry)
    # v4.3.2 NEW: zkontroluj prechody SOC pro snapshoty napeti clanku
    _check_snapshot(ctx)


def record_event(event_type: str, **fields) -> None:
    event_history.append({"ts": time.time(), "type": event_type, **fields})


class SetBoolRequest(BaseModel):
    value: bool

class SetTempRequest(BaseModel):
    value: int

class OverrideRequest(BaseModel):
    enabled: bool
    reason: Optional[str] = None

class CleaningStartRequest(BaseModel):
    hours: float

# v3.6 schedule
class ScheduleToggleReq(BaseModel):
    enabled: bool

class ScheduleRuleToggleReq(BaseModel):
    rule_index: int
    enabled: bool

# v3.7 heating curve / preshower
class HeatingPredictReq(BaseModel):
    target_temp: float
    current_temp: Optional[float] = None

class PreShowerStartReq(BaseModel):
    target_temp: float = 38.0
    eta_minutes: Optional[int] = None
    eta_iso: Optional[str] = None


def create_app(ctx: SystemContext, config: dict) -> FastAPI:
    app = FastAPI(title="SolarGuard")
    set_config_ref(config)
    # v4.3.2 NEW: nahraj snapshoty napeti clanku z disku (pri startu)
    _load_snapshots_from_disk()

    # v3.9 NEW: dependencies pro auth (write = POST/PUT/DELETE, read = GET)
    def auth_write(request: Request) -> None:
        require_auth(request, write=True)

    def auth_read(request: Request) -> None:
        require_auth(request, write=False)

    # NEW v3.3: Mount /static directory pro PWA assets
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # NEW v3.3: Service Worker MUSI byt servirovany z root scope (/)
    # aby mohl cachovat vsechno pod /. Proto specialni route, ne pres /static/.
    @app.get("/sw.js")
    async def service_worker():
        sw_path = STATIC_DIR / "sw.js"
        if not sw_path.exists():
            raise HTTPException(404, "Service worker not found")
        return FileResponse(
            str(sw_path),
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    # NEW v3.3: manifest.json je take potreba na rootu (i kdyz lze i pres /static)
    @app.get("/manifest.json")
    async def manifest():
        manifest_path = STATIC_DIR / "manifest.json"
        if not manifest_path.exists():
            raise HTTPException(404, "Manifest not found")
        return FileResponse(str(manifest_path), media_type="application/manifest+json")

    @app.get("/api/state")
    async def api_state():
        v = ctx.victron; s = ctx.spa; e = ctx.env; f = ctx.forecast
        p = ctx.plan; en = ctx.energy; c = ctx.cleaning
        return {
            "state": ctx.current_state.value,
            "time_in_state": int(ctx.time_in_state()),
            "dry_run": config["general"].get("dry_run", True),
            "override_active": ctx.override_active,
            "override_reason": ctx.override_reason,
            # NEW v3.2: aktualni scena a cilova teplota
            "current_scene": getattr(ctx, "current_scene", "solar_auto"),
            "target_temp_override": getattr(ctx, "target_temp_override", None),
            "victron": {
                "soc": v.soc_pct, "pv": v.pv_power_w, "surplus": v.surplus_w,
                "load": v.load_total_w, "stale": v.is_stale,
                "battery_power": v.battery_power_w, "grid": v.grid_total_w,
                # v4.3.0 NEW: per-faze loady pro detail na hlavni strane
                "load_l1": v.load_l1_w, "load_l2": v.load_l2_w, "load_l3": v.load_l3_w,
                "grid_l1": v.grid_l1_w, "grid_l2": v.grid_l2_w, "grid_l3": v.grid_l3_w,
                "pv_yield_today_kwh": v.pv_yield_today_kwh,
                "consumption_today_kwh": v.consumption_today_kwh,
                "battery_in_today_kwh": v.battery_in_today_kwh,
                "pv_yield_yesterday_kwh": v.pv_yield_yesterday_kwh,
                "consumption_yesterday_kwh": v.consumption_yesterday_kwh,
            },
            # v4.3.2 NEW: Seplos BMS RS485 - napeti vsech clanku
            "seplos": _seplos_json(ctx.seplos),
            "spa": {
                "current_temp": s.current_temp_c, "target_temp": s.target_temp_c,
                "heater": s.heater_on, "filter": s.filter_on,
                "bubbles": s.bubbles_on, "jets": s.jets_on,
                "sanitizer": s.sanitizer_on, "power": s.power_on,
                "online": s.online, "error": s.error_code,
            },
            "heatpump": {
                "enabled": _heatpump_controller is not None,
                "online": ctx.heatpump.online and not ctx.heatpump.is_stale,
                "operating_mode": ctx.heatpump.operating_mode,
                "compressor_running": ctx.heatpump.compressor_running,
                "power_w": ctx.heatpump.power_consumption_w,
                "outdoor_temp": ctx.heatpump.outdoor_temp_c,
                "indoor_temp": ctx.heatpump.indoor_temp_c,
                "hot_water_temp": ctx.heatpump.hot_water_temp_c,
                "manual_override": ctx.heatpump.manual_override,
            },
            "env": {
                "air_temp": e.air_temp_c, "light_lux": e.light_lux,
                "wind_kmh": e.wind_kmh, "is_raining": e.is_raining,
                "is_sunny": e.is_sunny, "is_overcast": e.is_overcast, "stale": e.is_stale,
            },
            "forecast": {
                "pv_today": f.predicted_pv_kwh_today,
                "pv_remaining": f.predicted_pv_kwh_remaining,
                "sunrise": f.sunrise, "sunset": f.sunset,
                "hourly_times": f.hourly_times,
                "hourly_radiation": f.hourly_radiation,
                "hourly_temp": f.hourly_temp,
                "hourly_cloudcover": f.hourly_cloudcover,
                "hourly_rain_prob": f.hourly_rain_prob,
            },
            "plan": {
                "strategy": p.strategy.value if p.strategy else "unknown",
                "predicted_pv_kwh": p.predicted_pv_kwh,
                "battery_available_kwh": p.battery_available_kwh,
                "baseline_consumption_kwh": p.baseline_consumption_kwh,
                "discretionary_kwh": p.discretionary_kwh,
                "dynamic_surplus_on_w": p.dynamic_surplus_on_w,
                "dynamic_surplus_off_w": p.dynamic_surplus_off_w,
                "reason": p.reason,
                "computed_at": p.computed_at,
            },
            "energy": {
                "session_hours": (time.time() - en.session_start) / 3600.0,
                "pv_produced_kwh": en.pv_produced_kwh,
                "home_consumed_kwh": en.home_consumed_kwh,
                "battery_charged_kwh": en.battery_charged_kwh,
                "battery_discharged_kwh": en.battery_discharged_kwh,
                "grid_in_kwh": en.grid_in_kwh, "grid_out_kwh": en.grid_out_kwh,
            },
            "cleaning": {
                "running": c.is_running,
                "duration_hours": c.duration_hours,
                "elapsed_sec": c.elapsed_sec,
                "remaining_sec": c.remaining_sec,
                "progress_pct": c.progress_pct,
                "started_at": c.started_at if c.is_running else None,
                "ends_at": c.ends_at,
            },
            "spot": {
                "current_price_kc": ctx.spot.current_price_kc(),
                "best_hours_today": ctx.spot.best_hours_today(4),
                "stale": ctx.spot.is_stale,
                "today_date": ctx.spot.today_date,
            },
            "preshower": {
                "running": _preshower.is_running if _preshower else False,
                "state": ctx.preshower.state.value if ctx.preshower and hasattr(ctx.preshower, 'state') else "idle",
                "target_time_iso": (
                    time.strftime("%H:%M", time.localtime(ctx.preshower.target_time))
                    if ctx.preshower and ctx.preshower.target_time else None
                ),
                "target_temp": ctx.preshower.target_temp if ctx.preshower else None,
                "time_remaining_sec": ctx.preshower.time_remaining_sec if ctx.preshower else None,
                "progress_pct": ctx.preshower.progress_pct if ctx.preshower else 0,
            },
            "config": {
                "surplus_on": config["spa"]["surplus_on_w"],
                "surplus_off": config["spa"]["surplus_off_w"],
                "min_soc": config["spa"]["min_soc_pct"],
                "target_temp": config["spa"]["target_temp_c"],
            },
            "ts": time.time(),
        }

    @app.get("/api/history")
    async def api_history():
        return JSONResponse({"ticks": list(tick_history)})

    @app.get("/api/decisions")
    async def api_decisions(limit: int = 200):
        return JSONResponse({"ticks": list(tick_history)[-limit:][::-1]})

    @app.get("/api/engine/status")
    async def api_engine_status():
        """v4.3.0 NEW: Detailni status rozhodovaciho enginu pro tab Rozhodnuti.

        Vraci vse co potrebujes pro pochopeni co engine ted dela:
        - aktualni stav + cas v stavu
        - kolik zbyva do uplynuti min_on/min_off
        - per-phase loady L1/L2/L3 + jejich limity
        - aktivni prahy (on/off) staticke vs dynamicke
        - stable_surplus okno
        - spike detector status
        - posledni dovod rozhodnuti
        """
        v = ctx.victron
        s = ctx.spa
        p = ctx.plan
        e = ctx.env
        cfg_yaml = _config_ref or {}
        cfg = cfg_yaml.get("spa", {})
        now = time.time()

        # Kolik zbyva v aktualnim stavu
        time_in_state = ctx.time_in_state()
        state = ctx.current_state.value
        state_min_required = None
        state_min_label = None
        if state == "idle":
            state_min_required = cfg.get("min_off_time_sec", 300)
            state_min_label = "min_off_time"
        elif state == "heating":
            state_min_required = cfg.get("min_on_time_sec", 600)
            state_min_label = "min_on_time"
        elif state == "cooldown":
            state_min_required = cfg.get("min_off_time_sec", 300)
            state_min_label = "cooldown"
        time_remaining = None
        if state_min_required and time_in_state < state_min_required:
            time_remaining = int(state_min_required - time_in_state)

        # Stable surplus + max za 60s (anti-glitch)
        stab_window = cfg.get("stability_window_sec", 90)
        cutoff_stab = now - stab_window
        recent_stab = [val for t, val in ctx.surplus_history if t >= cutoff_stab]
        stable_surplus = min(recent_stab) if len(recent_stab) >= 3 else None
        off_window = cfg.get("off_stability_window_sec", 60)
        cutoff_off = now - off_window
        recent_off = [val for t, val in ctx.surplus_history if t >= cutoff_off]
        max_surplus_recent = max(recent_off) if recent_off else None

        # Per-phase
        phase_max = cfg.get("phase_max_continuous_w", 3500)
        spa_phase = cfg.get("spa_phase_label", "L2")
        phases = []
        for label, val in [("L1", v.load_l1_w), ("L2", v.load_l2_w), ("L3", v.load_l3_w)]:
            phases.append({
                "label": label,
                "load_w": val,
                "max_w": phase_max,
                "is_spa_phase": label == spa_phase,
                "pct": (val / phase_max * 100) if val is not None else None,
                "overload": val is not None and val > phase_max,
            })

        # Spike detector status
        spike_active = (state == "spike_cool" and ctx.cooldown_until > now)
        spike_remaining = int(ctx.cooldown_until - now) if spike_active else None
        last_spike_reason = getattr(ctx, "_last_spike_reason", None)

        # Aktivni prahy
        on_static = cfg.get("surplus_on_w", 1500)
        off_static = cfg.get("surplus_off_w", 800)
        on_active = p.dynamic_surplus_on_w if p.dynamic_surplus_on_w is not None else on_static
        off_active = p.dynamic_surplus_off_w if p.dynamic_surplus_off_w is not None else off_static

        # BAT-FULL?
        battery_full_pct = cfg.get("battery_full_soc_pct", 90)
        battery_full = v.soc_pct is not None and v.soc_pct >= battery_full_pct

        # Posledni rozhodnuti
        last_tick = list(tick_history)[-1] if tick_history else None
        last_reason = last_tick.get("reason") if last_tick else None

        # Day strategy
        strategy = p.strategy.value if p.strategy else "unknown"

        return {
            "ts": now,
            "state": {
                "current": state,
                "time_in_state_sec": int(time_in_state),
                "min_required_sec": state_min_required,
                "time_remaining_sec": time_remaining,
                "min_label": state_min_label,
                "override_active": ctx.override_active,
                "override_reason": ctx.override_reason,
                "current_scene": getattr(ctx, "current_scene", "solar_auto"),
            },
            "thresholds": {
                "on_static_w": on_static,
                "off_static_w": off_static,
                "on_active_w": on_active,
                "off_active_w": off_active,
                "is_dynamic": p.dynamic_surplus_on_w is not None,
                "strategy": strategy,
                "strategy_reason": p.reason or "",
            },
            "surplus": {
                "current_w": v.surplus_w,
                "stable_w": stable_surplus,
                "max_recent_w": max_surplus_recent,
                "stability_window_sec": stab_window,
                "off_window_sec": off_window,
                "samples_in_window": len(recent_stab),
                "is_above_on": stable_surplus is not None and stable_surplus > on_active,
                "is_below_off": v.surplus_w is not None and v.surplus_w < off_active,
            },
            "phases": phases,
            "spike": {
                "active": spike_active,
                "remaining_sec": spike_remaining,
                "last_reason": last_spike_reason,
                "spa_phase": spa_phase,
                "ignore_window_sec": cfg.get("spike_ignore_window_sec", 90),
                "safety_surplus_w": cfg.get("spike_safety_surplus_w", 500),
            },
            "battery": {
                "soc_pct": v.soc_pct,
                "min_soc_hard": cfg.get("min_soc_pct", 20),
                "battery_full_pct": battery_full_pct,
                "is_battery_full": battery_full,
                "is_under_min": v.soc_pct is not None and v.soc_pct < cfg.get("min_soc_pct", 20),
            },
            "spa": {
                "online": s.online,
                "current_temp_c": s.current_temp_c,
                "target_temp_c": s.target_temp_c,
                "max_temp_c": cfg.get("max_temp_c", 40),
                "heater_on": s.heater_on,
                "error_code": s.error_code,
                "consecutive_failures": s.consecutive_failures,
                "is_at_target": (s.current_temp_c is not None and s.target_temp_c is not None
                                 and s.current_temp_c >= s.target_temp_c),
            },
            "env": {
                "air_temp_c": e.air_temp_c,
                "min_air_temp_c": cfg.get("min_air_temp_c", 2),
                "is_frost": (e.air_temp_c is not None and e.air_temp_c < cfg.get("min_air_temp_c", 2)),
                "wind_kmh": e.wind_kmh,
                "wind_reduce_kmh": cfg.get("wind_reduce_kmh", 25),
            },
            "victron": {
                "stale": v.is_stale,
                "last_update_age_sec": int(now - v.last_update) if v.last_update else None,
            },
            # v4.3.2 NEW: Seplos BMS pro panel napeti clanku v Rozhodnuti tabu
            "seplos": _seplos_json(ctx.seplos),
            "last_decision": {
                "reason": last_reason,
                "ts": last_tick.get("ts") if last_tick else None,
            },
        }

    @app.get("/api/events")
    async def api_events():
        return JSONResponse({"events": list(event_history)[::-1]})

    # v4.3.2 NEW: snapshoty napeti clanku pri SOC 99% (FULL) a 20% (LOW)
    @app.get("/api/seplos/snapshots")
    async def api_seplos_snapshots(limit: int = 20):
        # Vrat nejnovejsi snapshoty (newest first)
        snaps = list(_seplos_snapshots)[-limit:][::-1]
        # Pro UI: najdi posledni FULL a posledni LOW pro porovnani
        last_full = next((s for s in snaps if s.get("type") == "FULL"), None)
        last_low  = next((s for s in snaps if s.get("type") == "LOW"), None)
        return {
            "count": len(_seplos_snapshots),
            "snapshots": snaps,
            "last_full": last_full,
            "last_low": last_low,
        }

    @app.get("/api/appliances")
    async def api_appliances():
        if _appliance_evaluator is None:
            raise HTTPException(503, "Appliance evaluator not initialized")
        verdicts = _appliance_evaluator.evaluate(ctx)
        return {
            "ts": time.time(),
            "surplus_w": ctx.victron.surplus_w,
            "soc_pct": ctx.victron.soc_pct,
            "pv_w": ctx.victron.pv_power_w,
            "load_w": ctx.victron.load_total_w,
            "strategy": ctx.plan.strategy.value if ctx.plan.strategy else "unknown",
            "appliances": [
                {"id": v.id, "name": v.name, "emoji": v.emoji,
                 "status": v.status, "confidence": v.confidence, "message": v.message,
                 "peak_w": v.peak_w, "avg_w": v.avg_w,
                 "cycle_min": v.cycle_min, "cycle_kwh": round(v.cycle_kwh, 2),
                 "surplus_now_w": v.surplus_now_w, "covered_pct": v.covered_pct,
                 # v4.3.0 NEW
                 "pv_now_w": v.pv_now_w,
                 "deficit_kwh": v.deficit_kwh,
                 "from_battery_kwh": v.from_battery_kwh,
                 "from_grid_kwh": v.from_grid_kwh,
                 "energy_source": v.energy_source}
                for v in verdicts
            ],
        }

    # v4.3.0 NEW: Heat Pump API endpointy
    @app.get("/api/heatpump")
    async def api_heatpump():
        """Aktualni stav tepelneho cerpadla."""
        if _heatpump_controller is None:
            return {"enabled": False, "message": "Heat pump module disabled"}
        summary = _heatpump_controller.get_status_summary()
        engine_state = _heatpump_engine.current_state.value if _heatpump_engine else "disabled"
        time_in_state = (time.time() - _heatpump_engine.state_entered_at) if _heatpump_engine else 0
        summary["enabled"] = True
        summary["engine_state"] = engine_state
        summary["time_in_state_sec"] = int(time_in_state)
        return summary

    class HpTempRequest(BaseModel):
        value: float

    class HpModeRequest(BaseModel):
        mode: str  # "heat" | "cool" | "hot_water" | "off"

    class HpBoolRequest(BaseModel):
        value: bool

    class HpOverrideRequest(BaseModel):
        enabled: bool
        reason: Optional[str] = None

    def _check_hp():
        if _heatpump_controller is None:
            raise HTTPException(503, "Heat pump module disabled")
        if not ctx.heatpump.online or ctx.heatpump.is_stale:
            raise HTTPException(503, "Heat pump offline or stale")

    @app.post("/api/heatpump/hot_water_temp", dependencies=[Depends(auth_write)])
    async def hp_set_hw(req: HpTempRequest):
        _check_hp()
        if req.value < 30 or req.value > 65:
            raise HTTPException(400, "Hot water temp must be 30-65 C")
        ok = await _heatpump_controller.set_target_hot_water_temp(req.value, force=True)
        record_event("hp_command", target="hot_water_temp", value=req.value, success=ok)
        return {"success": ok}

    @app.post("/api/heatpump/room_temp", dependencies=[Depends(auth_write)])
    async def hp_set_room(req: HpTempRequest):
        _check_hp()
        if req.value < 16 or req.value > 26:
            raise HTTPException(400, "Room temp must be 16-26 C")
        ok = await _heatpump_controller.set_target_room_temp(req.value, force=True)
        record_event("hp_command", target="room_temp", value=req.value, success=ok)
        return {"success": ok}

    @app.post("/api/heatpump/mode", dependencies=[Depends(auth_write)])
    async def hp_set_mode(req: HpModeRequest):
        _check_hp()
        valid_modes = ["heat", "cool", "hot_water", "off"]
        if req.mode not in valid_modes:
            raise HTTPException(400, f"Mode must be one of {valid_modes}")
        ok = await _heatpump_controller.set_operating_mode(req.mode, force=True)
        record_event("hp_command", target="mode", value=req.mode, success=ok)
        return {"success": ok}

    @app.post("/api/heatpump/block_heater", dependencies=[Depends(auth_write)])
    async def hp_block_heater(req: HpBoolRequest):
        _check_hp()
        ok = await _heatpump_controller.block_additional_heater(req.value, force=True)
        record_event("hp_command", target="block_aux_heater", value=req.value, success=ok)
        return {"success": ok}

    @app.post("/api/heatpump/scene/solar_boost", dependencies=[Depends(auth_write)])
    async def hp_scene_boost():
        _check_hp()
        ctx.heatpump.manual_override = True
        ctx.heatpump.manual_override_reason = "manual: solar_boost"
        await _heatpump_controller.enable_solar_boost()
        record_event("hp_scene", name="solar_boost")
        return {"success": True}

    @app.post("/api/heatpump/scene/cooling", dependencies=[Depends(auth_write)])
    async def hp_scene_cool():
        _check_hp()
        ctx.heatpump.manual_override = True
        ctx.heatpump.manual_override_reason = "manual: cooling"
        await _heatpump_controller.enable_cooling()
        record_event("hp_scene", name="cooling")
        return {"success": True}

    @app.post("/api/heatpump/scene/heating", dependencies=[Depends(auth_write)])
    async def hp_scene_heat():
        _check_hp()
        ctx.heatpump.manual_override = True
        ctx.heatpump.manual_override_reason = "manual: heating"
        await _heatpump_controller.set_operating_mode("heat", force=True)
        record_event("hp_scene", name="heating")
        return {"success": True}

    @app.post("/api/heatpump/scene/auto", dependencies=[Depends(auth_write)])
    async def hp_scene_auto():
        _check_hp()
        ctx.heatpump.manual_override = False
        ctx.heatpump.manual_override_reason = ""
        await _heatpump_controller.disable_solar_boost()
        record_event("hp_scene", name="auto")
        return {"success": True, "manual_override": False}

    @app.post("/api/heatpump/override", dependencies=[Depends(auth_write)])
    async def hp_override(req: HpOverrideRequest):
        _check_hp()
        ctx.heatpump.manual_override = req.enabled
        ctx.heatpump.manual_override_reason = req.reason or ("manual override" if req.enabled else "")
        record_event("hp_override", enabled=req.enabled, reason=ctx.heatpump.manual_override_reason)
        return {"manual_override": ctx.heatpump.manual_override}

    # v4.3.0 NEW: Appliance learning API endpoints
    # User klikne "PUSTIL JSEM" na karte spotrebice -> SolarGuard zacne sledovat,
    # samy detekuje fazi a uci se profil (peak_w, avg_w, cycle_min, cycle_kwh).
    def _check_learning():
        if _learning_manager is None:
            raise HTTPException(503, "Learning manager not initialized")

    @app.post("/api/learning/start/{appliance_id}", dependencies=[Depends(auth_write)])
    async def learning_start(appliance_id: str):
        _check_learning()
        result = await _learning_manager.start_cycle(appliance_id)
        if not result.get("success"):
            raise HTTPException(400, result.get("error", "Cannot start cycle"))
        record_event("learning_start", appliance_id=appliance_id,
                     baseline=result.get("baseline"))
        return result

    @app.post("/api/learning/stop/{appliance_id}", dependencies=[Depends(auth_write)])
    async def learning_stop(appliance_id: str):
        _check_learning()
        result = await _learning_manager.stop_cycle(appliance_id, reason="manual")
        if result is None:
            raise HTTPException(404, "No active cycle for this appliance")
        record_event("learning_stop", appliance_id=appliance_id,
                     duration_min=result.duration_min, peak_w=result.peak_w,
                     avg_w=result.avg_w, kwh=result.kwh,
                     detected_phase=result.detected_phase, sample_count=result.sample_count,
                     interrupted=result.interrupted)
        # Convert dataclass to dict for JSON response
        from dataclasses import asdict
        return {"success": True, "result": asdict(result)}

    @app.get("/api/learning/status")
    async def learning_status():
        _check_learning()
        from dataclasses import asdict
        active = _learning_manager.get_active_cycles()
        profiles = {
            app_id: asdict(p)
            for app_id, p in _learning_manager.get_all_profiles().items()
        }
        recent = [asdict(r) for r in _learning_manager.get_recent_results()]
        return {
            "active_cycles": active,
            "profiles": profiles,
            "recent_results": recent,
        }

    @app.get("/api/export.csv")
    async def api_export_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts","iso","state","strategy","soc","pv","surplus","load",
                    "water_temp","target_temp","heater","filter","sanitizer","error","reason",
                    "air_temp","light_lux","wind_kmh","is_raining","cleaning_running"])
        for t in tick_history:
            iso = datetime.fromtimestamp(t["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow([t["ts"], iso, t["state"], t.get("strategy"),
                        t["soc"], t["pv"], t["surplus"],
                        t["load"], t["water_temp"], t["target_temp"],
                        t["heater"], t["filter"], t.get("sanitizer"),
                        t["error"], t["reason"],
                        t.get("air_temp"), t.get("light_lux"),
                        t.get("wind_kmh"), t.get("is_raining"),
                        t.get("cleaning_running")])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=solarguard_history.csv"})

    @app.get("/api/events.csv")
    async def api_events_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts","iso","type","details"])
        for e in event_history:
            iso = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            details = {k: v for k, v in e.items() if k not in ("ts","type")}
            w.writerow([e["ts"], iso, e["type"], str(details)])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=solarguard_events.csv"})

    async def _check_spa():
        """v3.8.1: pokud spa je offline, zkus jeden refresh - mozna jen TCP padlo
        a uz je obnoveno. Az kdyz to selze, vrat 503.
        """
        if _spa_controller is None:
            raise HTTPException(503, "Spa controller not initialized")
        if not ctx.spa.online:
            # Zkus se quick-reconnect (refresh_status uvnitr ma timeout 10s)
            try:
                ok = await _spa_controller.refresh_status()
                if not ok or not ctx.spa.online:
                    raise HTTPException(503, "Spa is offline (reconnect failed)")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(503, f"Spa is offline ({e})")

    def _check_cleaning():
        if _cleaning_manager is None:
            raise HTTPException(503, "Cleaning manager not initialized")

    @app.post("/api/spa/heater", dependencies=[Depends(auth_write)])
    async def set_heater(req: SetBoolRequest, request: Request):
        await _check_spa()
        user = get_user(request)
        actor = user.name if user else "anonymous"
        if req.value:
            ctx.manual_heater_started_at = time.time()
            from .state import SystemState
            if ctx.current_state != SystemState.HEATING:
                ctx.transition(SystemState.HEATING, f"manual web heater ON by {actor}")
        else:
            ctx.manual_heater_started_at = 0.0
        ok = await _spa_controller.set_heater(req.value, force=True)
        record_event("web_command", target="heater", value=req.value, success=ok, actor=actor)
        return {"success": ok, "heater": ctx.spa.heater_on}

    @app.post("/api/spa/filter", dependencies=[Depends(auth_write)])
    async def set_filter(req: SetBoolRequest, request: Request):
        await _check_spa()
        user = get_user(request)
        actor = user.name if user else "anonymous"
        ok = await _spa_controller.set_filter(req.value, force=True)
        record_event("web_command", target="filter", value=req.value, success=ok, actor=actor)
        return {"success": ok, "filter": ctx.spa.filter_on}

    @app.post("/api/spa/bubbles", dependencies=[Depends(auth_write)])
    async def set_bubbles(req: SetBoolRequest):
        await _check_spa()
        ok = await _spa_controller.set_bubbles(req.value)
        record_event("web_command", target="bubbles", value=req.value, success=ok)
        return {"success": ok, "bubbles": ctx.spa.bubbles_on}

    @app.post("/api/spa/jets", dependencies=[Depends(auth_write)])
    async def set_jets(req: SetBoolRequest):
        await _check_spa()
        ok = await _spa_controller.set_jets(req.value)
        record_event("web_command", target="jets", value=req.value, success=ok)
        return {"success": ok, "jets": ctx.spa.jets_on}

    @app.post("/api/spa/sanitizer", dependencies=[Depends(auth_write)])
    async def set_sanitizer(req: SetBoolRequest):
        await _check_spa()
        # Pokud bezi cleaning, zabran manualnimu vypnuti - uzivatel ma pouzit cleaning stop
        if not req.value and ctx.cleaning.is_running:
            raise HTTPException(409, "Bezi cleaning program - pouzij STOP program.")
        ok = await _spa_controller.set_sanitizer(req.value, force=True)
        record_event("web_command", target="sanitizer", value=req.value, success=ok)
        return {"success": ok, "sanitizer": ctx.spa.sanitizer_on}

    @app.post("/api/spa/temp", dependencies=[Depends(auth_write)])
    async def set_temp(req: SetTempRequest):
        await _check_spa()
        if req.value < 20 or req.value > 40:
            raise HTTPException(400, "Temperature must be 20-40 C")
        ok = await _spa_controller.set_target_temp(req.value)
        # NEW v3.2: pri manualni zmene cile pres +/- "zamrznout" tu hodnotu
        # (jinak by ji decision engine mohl resetovat zpet na config target)
        ctx.target_temp_override = int(req.value)
        record_event("web_command", target="temp", value=req.value, success=ok)
        return {"success": ok, "target_temp": ctx.spa.target_temp_c, "target_override": ctx.target_temp_override}

    @app.get("/api/spa/cleaning/status")
    async def cleaning_status():
        c = ctx.cleaning
        return {
            "running": c.is_running, "duration_hours": c.duration_hours,
            "elapsed_sec": c.elapsed_sec, "remaining_sec": c.remaining_sec,
            "progress_pct": c.progress_pct,
            "started_at": c.started_at if c.is_running else None,
            "ends_at": c.ends_at,
            "sanitizer_on": ctx.spa.sanitizer_on, "filter_on": ctx.spa.filter_on,
        }

    @app.post("/api/spa/cleaning/start", dependencies=[Depends(auth_write)])
    async def cleaning_start(req: CleaningStartRequest):
        await _check_spa(); _check_cleaning()
        if req.hours <= 0 or req.hours > 12:
            raise HTTPException(400, "hours must be 0.1-12")
        result = await _cleaning_manager.start_program(req.hours)
        if not result.get("ok"):
            raise HTTPException(409, result.get("message", "cleaning start failed"))
        return result

    @app.post("/api/spa/cleaning/stop", dependencies=[Depends(auth_write)])
    async def cleaning_stop():
        _check_cleaning()
        result = await _cleaning_manager.stop_program(reason="web_manual")
        if not result.get("ok"):
            raise HTTPException(409, result.get("message", "cleaning stop failed"))
        return result

    @app.post("/api/override", dependencies=[Depends(auth_write)])
    async def set_override(req: OverrideRequest):
        ctx.override_active = req.enabled
        ctx.override_reason = req.reason or ("manual override" if req.enabled else "")
        record_event("override", enabled=req.enabled, reason=ctx.override_reason)
        return {"override_active": ctx.override_active, "reason": ctx.override_reason}

    @app.post("/api/spa/scene/heat_now", dependencies=[Depends(auth_write)])
    async def scene_heat_now():
        await _check_spa()
        ctx.override_active = True
        ctx.override_reason = "scene: heat_now"
        # NEW v3.2: heat_now pouziva config target (typicky 38)
        ctx.target_temp_override = None
        ctx.current_scene = "heat_now"
        # v3.8.1: oznacit ze topeni je nas vlastni (suppress spike protection)
        ctx.manual_heater_started_at = time.time()
        from .state import SystemState
        if ctx.current_state != SystemState.HEATING:
            ctx.transition(SystemState.HEATING, "scene: heat_now")
        await _spa_controller.set_filter(True, force=True)
        await _spa_controller.set_heater(True, force=True)
        target = _config_ref["spa"]["target_temp_c"] if _config_ref else 38
        await _spa_controller.set_target_temp(target)
        record_event("scene", name="heat_now", target_temp=target, heater=True, filter=True)
        return {"success": True, "override_active": True, "target_temp": ctx.spa.target_temp_c, "current_scene": "heat_now"}

    @app.post("/api/spa/scene/solar_auto", dependencies=[Depends(auth_write)])
    async def scene_solar_auto():
        await _check_spa()
        ctx.override_active = False
        ctx.override_reason = ""
        # NEW v3.2: solar_auto reset target na config (typicky 38)
        ctx.target_temp_override = None
        ctx.current_scene = "solar_auto"
        target = _config_ref["spa"]["target_temp_c"] if _config_ref else 38
        await _spa_controller.set_target_temp(int(target))
        await _spa_controller.set_heater(False, force=True)
        record_event("scene", name="solar_auto", target_temp=target)
        return {"success": True, "override_active": False, "target_temp": ctx.spa.target_temp_c, "current_scene": "solar_auto"}

    @app.post("/api/spa/scene/gentle", dependencies=[Depends(auth_write)])
    async def scene_gentle():
        """NEW v3.2: Mirny rezim - drzi vodu kolem 33C pro deti behem dne.

        SolarGuard normalne ridi podle prebytku, ale na nizsi cil.
        Kdyz voda dosahne 33C, topeni se zastavi. Kdyz klesne, zapne se zase
        (pokud je sluniko/prebytek).
        """
        await _check_spa()
        ctx.override_active = False
        ctx.override_reason = ""
        gentle_target = 33
        ctx.target_temp_override = gentle_target
        ctx.current_scene = "gentle"
        await _spa_controller.set_target_temp(gentle_target)
        record_event("scene", name="gentle", target_temp=gentle_target)
        return {"success": True, "override_active": False, "target_temp": ctx.spa.target_temp_c, "current_scene": "gentle"}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return DASHBOARD_HTML

    # ===== v3.6 NEW: Scheduler API =====
    @app.get("/api/schedule")
    async def api_schedule():
        if _scheduler is None:
            return {
                "enabled": False,
                "global_enabled": False,
                "rules": [],
                "next_trigger": None,
            }
        return {
            "enabled": True,
            "global_enabled": _scheduler.global_enabled,
            "rules": _scheduler.get_rules_status(),
            "next_trigger": _scheduler.get_next_trigger(),
        }

    class ScheduleToggleReq_LOCAL_REMOVED:
        pass  # přesunuto na top-level

    @app.post("/api/schedule/global", dependencies=[Depends(auth_write)])
    async def api_schedule_global(req: ScheduleToggleReq):
        if _scheduler is None:
            raise HTTPException(503, "Scheduler not initialized")
        _scheduler.set_global_enabled(req.enabled)
        record_event("schedule_global", enabled=req.enabled)
        return {"global_enabled": _scheduler.global_enabled}

    class ScheduleRuleToggleReq_LOCAL_REMOVED:
        pass  # přesunuto na top-level

    @app.post("/api/schedule/rule", dependencies=[Depends(auth_write)])
    async def api_schedule_rule(req: ScheduleRuleToggleReq):
        if _scheduler is None:
            raise HTTPException(503, "Scheduler not initialized")
        if req.rule_index < 0 or req.rule_index >= len(_scheduler.rules):
            raise HTTPException(400, "Invalid rule_index")
        _scheduler.rules[req.rule_index].enabled = req.enabled
        rule = _scheduler.rules[req.rule_index]
        record_event("schedule_rule", name=rule.name, enabled=req.enabled)
        return {"rule": rule.name, "enabled": rule.enabled}

    # ===== v3.6 NEW: Spot price API =====
    @app.get("/api/spot")
    async def api_spot():
        sp = ctx.spot
        return {
            "today_date": sp.today_date,
            "today_prices_eur": sp.today_prices_eur,
            "today_prices_kc": sp.hourly_prices_kc(with_fee=True),
            "today_prices_kc_clean": sp.hourly_prices_kc(with_fee=False),  # v4.1.2 NEW: cista spot cena
            "tomorrow_date": sp.tomorrow_date,
            "tomorrow_prices_eur": sp.tomorrow_prices_eur,
            "current_price_kc": sp.current_price_kc(with_fee=True),
            "current_price_kc_clean": sp.current_price_kc(with_fee=False),  # v4.1.2 NEW
            "best_hours_today": sp.best_hours_today(4),
            "eur_to_kc": sp.eur_to_kc,
            "fee_kc_per_kwh": sp.fee_kc_per_kwh,
            "stale": sp.is_stale,
            "last_update": sp.last_update,
        }

    # ===== v3.7 NEW: Heating curve API =====
    @app.get("/api/heating-curve")
    async def api_heating_curve():
        if _heating_curve is None:
            return {"available": False}
        # Predikce pro aktualni teplotu
        current = ctx.spa.current_temp_c
        target = ctx.spa.target_temp_c or 38
        prediction = _heating_curve.predict_to_target(
            current if current is not None else 28,
            target,
            ctx.env.air_temp_c,
            ctx.env.wind_kmh,
        ) if current is not None else None
        return {
            "available": True,
            "model": _heating_curve.get_model_info(),
            "current_prediction": prediction,
            "recent_samples": _heating_curve.get_recent_samples(20),
        }

    class HeatingPredictReq_LOCAL_REMOVED:
        pass  # přesunuto na top-level

    @app.post("/api/heating-curve/predict", dependencies=[Depends(auth_write)])
    async def api_heating_predict(req: HeatingPredictReq):
        if _heating_curve is None:
            raise HTTPException(503, "Heating curve not available")
        current = req.current_temp if req.current_temp is not None else ctx.spa.current_temp_c
        if current is None:
            raise HTTPException(400, "No current temp available")
        return _heating_curve.predict_to_target(
            current, req.target_temp,
            ctx.env.air_temp_c, ctx.env.wind_kmh,
        )

    # ===== v3.7 NEW: Pre-shower API =====
    @app.get("/api/preshower")
    async def api_preshower():
        if _preshower is None:
            return {"available": False}
        p = _preshower.program
        return {
            "available": True,
            "running": _preshower.is_running,
            "state": p.state.value if p.state else "idle",
            "target_time": p.target_time,
            "target_time_iso": (
                __import__('datetime').datetime.fromtimestamp(p.target_time).strftime("%H:%M")
                if p.target_time else None
            ),
            "target_temp": p.target_temp,
            "started_at": p.started_at,
            "time_remaining_sec": p.time_remaining_sec,
            "progress_pct": p.progress_pct,
            "failed_reason": p.failed_reason,
        }

    class PreShowerStartReq_LOCAL_REMOVED:
        pass  # přesunuto na top-level

    @app.post("/api/preshower/start", dependencies=[Depends(auth_write)])
    async def api_preshower_start(req: PreShowerStartReq):
        if _preshower is None:
            raise HTTPException(503, "Pre-shower not available")
        result = await _preshower.start_program(
            target_temp=req.target_temp,
            eta_minutes=req.eta_minutes,
            eta_iso=req.eta_iso,
        )
        if not result.get("ok"):
            raise HTTPException(400, result.get("reason", "unknown"))
        return result

    @app.post("/api/preshower/cancel", dependencies=[Depends(auth_write)])
    async def api_preshower_cancel():
        if _preshower is None:
            raise HTTPException(503, "Pre-shower not available")
        result = await _preshower.cancel()
        if not result.get("ok"):
            raise HTTPException(400, result.get("reason", "unknown"))
        return result

    # ===== v3.8 NEW: InfluxDB stats API =====
    @app.get("/api/influx/stats")
    async def api_influx_stats():
        if _influx is None:
            return {"available": False, "configured": False}
        return _influx.stats

    # ===== v3.9 NEW: Auth =====
    @app.get("/api/auth/status")
    async def api_auth_status(request: Request):
        """Vraci jestli je auth zapnuta + role a jmeno aktualniho uzivatele."""
        cfg = get_auth_config()
        if not cfg.enabled:
            return {
                "auth_enabled": False, "authenticated": True,
                "user": "anonymous", "role": ROLE_OWNER,
            }
        user = get_user(request)
        if user is None:
            return {"auth_enabled": True, "authenticated": False}
        return {
            "auth_enabled": True, "authenticated": True,
            "user": user.name, "role": user.role,
        }

    @app.post("/api/auth/login")
    async def api_auth_login(request: Request):
        """v4.1.4 FIX: manualni parse body misto Pydantic kvuli FastAPI/Pydantic conflictu."""
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        token = body.get("token", "").strip() if isinstance(body, dict) else ""
        if not token:
            raise HTTPException(400, "Missing token in body")
        user = check_token(token)
        if not user:
            raise HTTPException(401, "Invalid token")
        resp = JSONResponse({"success": True, "user": user.name, "role": user.role})
        resp.set_cookie(
            "solarguard_token", token,
            max_age=30 * 86400, samesite="lax", httponly=False,
        )
        return resp

    @app.post("/api/auth/logout")
    async def api_auth_logout():
        resp = JSONResponse({"success": True})
        resp.delete_cookie("solarguard_token")
        return resp

    # ===== v4.1 NEW: User management API (owner-only) =====
    # v4.1.4 FIX: manualni parse JSON body misto Pydantic - FastAPI/Pydantic 422 conflict
    @app.get("/api/users")
    async def api_users_list(request: Request):
        require_auth(request, write=True, owner_only=True)
        cfg = get_auth_config()
        current_user = get_user(request)
        return {
            "users": [u.to_public_dict() for u in cfg.list_users()],
            "valid_roles": list(VALID_ROLES),
            "current_user": current_user.name if current_user else None,
            "auth_enabled": cfg.enabled,
        }

    @app.post("/api/users")
    async def api_users_create(request: Request):
        require_auth(request, write=True, owner_only=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be JSON object")
        name = (body.get("name") or "").strip()
        role = (body.get("role") or "").strip()
        token = (body.get("token") or "").strip()
        if not name or not role or not token:
            raise HTTPException(400, "Missing name, role or token")
        cfg = get_auth_config()
        try:
            user = cfg.add_user(name, role, token)
            record_event("user_created", name=name, role=role)
            return {"success": True, "user": user.to_public_dict()}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/users/{name}")
    async def api_users_delete(name: str, request: Request):
        require_auth(request, write=True, owner_only=True)
        cfg = get_auth_config()
        current_user = get_user(request)
        if current_user and current_user.name == name:
            raise HTTPException(400, "Nelze smazat sam sebe")
        try:
            cfg.remove_user(name)
            record_event("user_deleted", name=name)
            return {"success": True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/users/{name}")
    async def api_users_update(name: str, request: Request):
        require_auth(request, write=True, owner_only=True)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be JSON object")
        new_role = (body.get("role") or "").strip()
        if not new_role:
            raise HTTPException(400, "Missing role")
        cfg = get_auth_config()
        current_user = get_user(request)
        if current_user and current_user.name == name and new_role != ROLE_OWNER:
            raise HTTPException(400, "Nelze degradovat sebe sama z owner role")
        try:
            cfg.update_role(name, new_role)
            record_event("user_role_changed", name=name, role=new_role)
            return {"success": True}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/users/{name}/regenerate-token")
    async def api_users_regen_token(name: str, request: Request):
        require_auth(request, write=True, owner_only=True)
        cfg = get_auth_config()
        try:
            new_token = cfg.regenerate_token(name)
            record_event("user_token_regen", name=name)
            current_user = get_user(request)
            self_regen = current_user and current_user.name == name
            return {
                "success": True,
                "new_token": new_token,
                "self_regen": self_regen,
                "message": "Token byl regenerovan - ulozte ho, znovu ho nikde neuvidite!"
            }
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/users/{name}/audit")
    async def api_users_audit(name: str, request: Request, limit: int = 50):
        """Vrati posledni N akci ktere udelal dany user (audit trail)."""
        require_auth(request, write=True, owner_only=True)
        actions = [
            e for e in event_history
            if e.get("actor") == name or
               (e.get("type", "").startswith("user_") and e.get("name") == name)
        ]
        return {"name": name, "actions": list(actions)[-limit:][::-1]}

    # ===== v3.9 NEW: Health check (bez auth, pro monitoring) =====
    @app.get("/healthz")
    async def healthz():
        """Vraci 200 pokud SolarGuard zije a komponenty fungujou.
        503 pokud neco zamrzlo (Victron stale > 5min, spa offline).
        Bez auth - pouziva se pro systemd watchdog a externi monitoring.
        """
        problems = []
        v = ctx.victron
        if v.is_stale:
            problems.append(f"victron MQTT stale (last update {int(time.time() - v.last_update)}s ago)")
        s = ctx.spa
        if not s.online and s.consecutive_failures >= 5:
            problems.append(f"spa offline ({s.consecutive_failures} failures)")
        # Loop check: tick_history musi mit zaznam < 2min stary
        if tick_history:
            last_tick_age = time.time() - tick_history[-1].get("ts", 0)
            if last_tick_age > 120:
                problems.append(f"main loop stuck (last tick {int(last_tick_age)}s ago)")

        body = {
            "status": "ok" if not problems else "degraded",
            "problems": problems,
            "uptime_sec": int(time.time() - _startup_time),
            "victron_age_sec": int(time.time() - v.last_update) if v.last_update else None,
            "spa_online": s.online,
            "last_tick_age_sec": int(time.time() - tick_history[-1].get("ts", 0)) if tick_history else None,
            "version": "v4.3.0",
        }
        if problems:
            return JSONResponse(body, status_code=503)
        return body

    # ===== v4.0 NEW: Anomaly insights =====
    @app.get("/api/insights")
    async def api_insights():
        if _anomaly is None:
            return {"insights": [], "history_days": 0, "configured": False}
        try:
            insights = _anomaly.compute_insights()
            return {
                "insights": [
                    {
                        "severity": i.severity, "icon": i.icon,
                        "title": i.title, "detail": i.detail,
                        "metric": i.metric, "value": i.value,
                        "baseline": i.baseline, "deviation_pct": i.deviation_pct,
                    } for i in insights
                ],
                "history_days": len(_anomaly.history),
                "configured": True,
            }
        except Exception as e:
            return {"insights": [], "error": str(e), "configured": True}

    @app.get("/api/insights/history")
    async def api_insights_history():
        if _anomaly is None:
            return {"history": []}
        return {"history": _anomaly.get_history_dict()}

    # ===== v4.1 NEW: Weekly digest =====
    @app.get("/api/digest/latest")
    async def api_digest_latest():
        if _digest is None:
            return {"digest": None, "configured": False}
        latest = _digest.latest
        if latest is None:
            return {"digest": None, "configured": True, "message": "Žádný digest zatím nebyl vygenerován. První bude v neděli 18:00."}
        from .insights.digest import format_digest
        from dataclasses import asdict
        return {
            "digest": asdict(latest),
            "markdown": format_digest(latest),
            "configured": True,
        }

    @app.post("/api/digest/generate", dependencies=[Depends(auth_write)])
    async def api_digest_generate():
        """Manuální trigger digestu (pro testování)."""
        if _digest is None:
            raise HTTPException(503, "Digest generator not configured")
        try:
            digest = _digest.generate_now()
            _digest.save_digest(digest)
            await _digest.deliver(digest)
            from .insights.digest import format_digest
            from dataclasses import asdict
            return {
                "success": True,
                "digest": asdict(digest),
                "markdown": format_digest(digest),
            }
        except Exception as e:
            log.exception(f"manual digest error: {e}")
            raise HTTPException(500, f"Digest generation failed: {e}")

    return app


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>SolarGuard</title>

<!-- PWA -->
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#2563eb">
<meta name="application-name" content="SolarGuard">

<!-- iOS PWA -->
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SolarGuard">
<link rel="apple-touch-icon" href="/static/icon-180.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/icon-180.png">
<link rel="icon" type="image/svg+xml" href="/static/icon.svg">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">

<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #f3f5f8; --surface: #ffffff; --border: #e2e8f0; --border-strong: #cbd5e1;
  --text: #0f172a; --text-muted: #64748b; --text-dim: #94a3b8;
  --primary: #2563eb; --primary-soft: #dbeafe;
  --success: #16a34a; --success-soft: #dcfce7;
  --warning: #ea580c; --warning-soft: #ffedd5;
  --danger: #dc2626; --danger-soft: #fee2e2;
  --solar: #f59e0b; --solar-soft: #fef3c7;
  --water: #0891b2; --water-soft: #cffafe;
  --purple: #9333ea; --purple-soft: #f3e8ff;
  --teal: #0d9488; --teal-soft: #ccfbf1;
  --shadow-sm: 0 1px 2px rgba(15,23,42,0.04);
  --shadow: 0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
  --mono: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
  --ui: 'Inter', -apple-system, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: var(--ui); font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased; }
.container { max-width: 1200px; margin: 0 auto; padding: max(20px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right)) max(60px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }

/* PWA stale/offline banner */
.offline-banner { display: none; background: var(--warning); color: white; padding: 8px 14px; border-radius: 8px; margin-bottom: 12px; font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: 0.5px; text-align: center; }
.offline-banner.visible { display: block; }
.offline-banner.visible.cached { background: var(--warning-soft); color: var(--warning); border: 1px solid var(--warning); }
header { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; margin-bottom: 18px; background: var(--surface); border-radius: 12px; box-shadow: var(--shadow); flex-wrap: wrap; gap: 12px; }
h1 { font-family: var(--mono); font-size: 22px; font-weight: 800; letter-spacing: -0.5px; color: var(--text); display: flex; align-items: center; }
h1 .mark { color: var(--primary); }
h1 .slash { color: var(--text-dim); margin: 0 6px; font-weight: 400; }
h1 .sub { font-size: 11px; font-weight: 500; color: var(--text-muted); letter-spacing: 2px; text-transform: uppercase; }
.status-pills { display: flex; gap: 8px; flex-wrap: wrap; }
/* v4.3.0 NEW: connection pill */
.conn-pill { display: inline-flex; align-items: center; gap: 6px; cursor: help; }
.conn-pill .conn-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.conn-pill.conn-ok { background: var(--success-soft); color: var(--success); }
.conn-pill.conn-ok .conn-dot { background: var(--success); animation: conn-pulse 2s ease-in-out infinite; }
.conn-pill.conn-warn { background: var(--warning-soft); color: var(--warning); }
.conn-pill.conn-warn .conn-dot { background: var(--warning); }
.conn-pill.conn-fail { background: var(--danger-soft); color: var(--danger); }
.conn-pill.conn-fail .conn-dot { background: var(--danger); animation: conn-blink 0.8s ease-in-out infinite; }
@keyframes conn-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
@keyframes conn-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.2; } }
.pill { display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: 1px; padding: 6px 10px; border-radius: 6px; text-transform: uppercase; }
.pill.state-idle { background: #f1f5f9; color: var(--text-muted); }
.pill.state-heating { background: var(--success-soft); color: var(--success); }
.pill.state-cooldown, .pill.state-spike_cool { background: var(--warning-soft); color: var(--warning); }
.pill.state-safe_mode { background: var(--danger-soft); color: var(--danger); }
.pill.state-night_off { background: #1e293b; color: #cbd5e1; }
.pill.dry { background: var(--warning-soft); color: var(--warning); }
.pill.override { background: var(--purple-soft); color: var(--purple); }
.pill.cleaning { background: var(--teal-soft); color: var(--teal); }
.pill.live { background: var(--success-soft); color: var(--success); }
.pill.bat-full { background: var(--solar-soft); color: #b45309; }
.pill.gentle { background: #e0f2fe; color: #075985; }
.tabs { display: flex; gap: 2px; margin-bottom: 18px; background: var(--surface); padding: 4px; border-radius: 10px; box-shadow: var(--shadow-sm); overflow-x: auto; }
.tab { padding: 9px 16px; cursor: pointer; font-size: 11px; font-weight: 600; color: var(--text-muted); border-radius: 7px; white-space: nowrap; border: none; background: transparent; transition: all 0.15s; user-select: none; font-family: var(--mono); letter-spacing: 0.5px; text-transform: uppercase; }
.tab:hover { color: var(--text); background: #f8fafc; }
.tab.active { color: var(--primary); background: var(--primary-soft); }
.tab-content { display: none; }
.tab-content.active { display: block; }
.section { margin-bottom: 22px; }
.section-title { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.section-title h2 { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--text-muted); letter-spacing: 2px; text-transform: uppercase; }
.download-btn { font-family: var(--mono); font-size: 10px; color: var(--primary); text-decoration: none; padding: 6px 12px; border-radius: 6px; background: var(--primary-soft); font-weight: 700; transition: all 0.15s; letter-spacing: 0.5px; text-transform: uppercase; }
.download-btn:hover { background: #bfdbfe; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.tile { background: var(--surface); border-radius: 10px; padding: 14px 16px; box-shadow: var(--shadow-sm); border: 1px solid var(--border); transition: all 0.15s; position: relative; }
.tile::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; border-radius: 10px 10px 0 0; }
.tile.solar::before { background: var(--solar); }
.tile.battery::before { background: var(--success); }
.tile.water::before { background: var(--water); }
.tile.home::before { background: var(--text-muted); }
.tile.surplus::before { background: var(--primary); }
.tile.spa::before { background: var(--purple); }

/* v4.3.0 NEW: per-phase mini bary v Dum tile */
.tile-home-detail .phase-mini-row { margin-top: 10px; display: flex; flex-direction: column; gap: 4px; }
.phase-mini { display: grid; grid-template-columns: 28px 1fr 50px; gap: 6px; align-items: center; font-family: var(--mono); font-size: 10px; }
.phase-mini-label { color: var(--text-muted); font-weight: 700; letter-spacing: 0.5px; }
.phase-mini-bar { height: 5px; background: #f1f5f9; border-radius: 3px; overflow: hidden; }
.phase-mini-fill { height: 100%; border-radius: 3px; transition: width 0.4s, background 0.3s; }
.phase-mini-val { color: var(--text-muted); text-align: right; font-variant-numeric: tabular-nums; }
.tile-label { font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--text-muted); font-weight: 700; margin-bottom: 6px; }
.tile-value { font-family: var(--mono); font-size: 28px; font-weight: 700; color: var(--text); letter-spacing: -1px; line-height: 1; font-variant-numeric: tabular-nums; }
.tile-unit { font-size: 12px; color: var(--text-muted); margin-left: 3px; font-weight: 500; }
.tile-sub { font-family: var(--mono); font-size: 10px; color: var(--text-muted); margin-top: 6px; letter-spacing: 0.5px; }
.val-pos { color: var(--success); } .val-neg { color: var(--danger); }
.val-solar { color: var(--solar); } .val-primary { color: var(--primary); } .val-water { color: var(--water); }

.plan-card { background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%); border: 1px solid var(--border); border-radius: 14px; padding: 20px; margin-bottom: 20px; }
.plan-card.aggressive { background: linear-gradient(135deg, #fefce8 0%, #fef3c7 100%); }
.plan-card.conservative { background: linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%); }
.plan-card.survive { background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%); }
.plan-card.unknown { background: var(--surface); }
.plan-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 8px; }
.plan-title { font-family: var(--mono); font-size: 11px; color: var(--text-muted); font-weight: 700; letter-spacing: 2px; text-transform: uppercase; }
.plan-strategy { font-family: var(--mono); font-size: 28px; font-weight: 800; color: var(--text); letter-spacing: -1.5px; }
.plan-strategy.aggressive { color: #b45309; } .plan-strategy.normal { color: var(--primary); }
.plan-strategy.conservative { color: var(--warning); } .plan-strategy.survive { color: var(--danger); }
.plan-reason { font-family: var(--mono); font-size: 12px; color: var(--text-muted); margin-bottom: 16px; }
.plan-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 12px; }
.plan-stat-label { font-family: var(--mono); font-size: 9px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--text-muted); font-weight: 700; margin-bottom: 3px; }
.plan-stat-value { font-family: var(--mono); font-size: 18px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
.plan-stat-unit { font-size: 11px; color: var(--text-muted); font-weight: 500; }
.plan-stat-age { font-family: var(--mono); font-size: 9px; color: var(--text-dim); margin-top: 6px; text-align: right; letter-spacing: 0.5px; }

.chart-wrap { background: var(--surface); border-radius: 10px; padding: 16px; box-shadow: var(--shadow-sm); border: 1px solid var(--border); height: 320px; }

.info-card { background: var(--surface); border-radius: 10px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); overflow: hidden; }
.info-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 13px; font-family: var(--mono); }
.info-row:last-child { border-bottom: none; }
.info-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
.info-value { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
.info-note { color: var(--text-dim); font-size: 9px; letter-spacing: 0.5px; margin-left: 6px; }

.scene-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 18px; }
.scene-row.scene-row-3 { grid-template-columns: 1fr 1fr 1fr; }
.scene-btn { background: var(--surface); border: 2px solid var(--border); padding: 18px 20px; border-radius: 12px; cursor: pointer; transition: all 0.2s; text-align: left; font-family: inherit; box-shadow: var(--shadow-sm); }
.scene-btn:hover { border-color: var(--primary); transform: translateY(-1px); box-shadow: var(--shadow); }
.scene-btn.active { border-color: var(--primary); background: var(--primary-soft); }
.scene-title { display: block; font-family: var(--mono); font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 4px; letter-spacing: 0.5px; text-transform: uppercase; }
.scene-desc { font-size: 12px; color: var(--text-muted); }

.control-panel { background: var(--surface); border-radius: 10px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); overflow: hidden; }
.control-row { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 10px; }
.control-row:last-child { border-bottom: none; }
.control-row-full { display: block; padding: 0 18px 14px; border-bottom: 1px solid var(--border); }
.control-label { font-size: 13px; color: var(--text); font-weight: 600; }
.control-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; font-family: var(--mono); }
.btn-group { display: flex; gap: 6px; flex-wrap: wrap; }
.btn { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 7px 16px; border-radius: 7px; font-family: var(--mono); font-size: 11px; font-weight: 700; cursor: pointer; transition: all 0.15s; letter-spacing: 0.5px; position: relative; }
.btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn.on { background: var(--success); border-color: var(--success); color: white; }
.btn.off { background: var(--danger); border-color: var(--danger); color: white; }
.btn.teal { background: var(--teal); border-color: var(--teal); color: white; }
.btn.teal:hover { background: #0f766e; border-color: #0f766e; color: white; }
/* v4.3.0 NEW: Loading state pri kliknuti - zpetna vazba ze command bezi */
.btn.loading { opacity: 0.8; cursor: wait; pointer-events: none; }
.btn.loading::after {
  content: ''; position: absolute; top: 50%; right: 6px;
  width: 10px; height: 10px; margin-top: -5px;
  border: 2px solid currentColor; border-right-color: transparent;
  border-radius: 50%; animation: btn-spin 0.6s linear infinite;
}
@keyframes btn-spin { to { transform: rotate(360deg); } }
/* Pro kratke pulse na stisku (i kdyz neni loading) */
.btn.flash { animation: btn-flash 0.4s ease; }
@keyframes btn-flash { 0% { transform: scale(0.95); } 50% { transform: scale(1.02); } 100% { transform: scale(1); } }

.temp-control { display: flex; align-items: center; gap: 6px; }
.temp-val { font-family: var(--mono); font-size: 20px; font-weight: 700; min-width: 60px; text-align: center; color: var(--text); font-variant-numeric: tabular-nums; }
.temp-btn { width: 32px; height: 32px; background: var(--surface); border: 1px solid var(--border); color: var(--text); cursor: pointer; font-size: 16px; font-weight: 700; border-radius: 6px; }
.temp-btn:hover { border-color: var(--primary); color: var(--primary); }

/* Cleaning progress inline v sanitizer radku */
.clean-progress-inline { margin-top: 10px; }
.clean-prog-head { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 11px; color: var(--teal); font-weight: 700; margin-bottom: 5px; }
.clean-prog-bar { height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; }
.clean-prog-fill { height: 100%; background: linear-gradient(90deg, var(--teal) 0%, #06b6d4 100%); border-radius: 3px; transition: width 1s; }
.clean-prog-times { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 9px; color: var(--text-muted); margin-top: 4px; letter-spacing: 0.5px; }

.table-card { background: var(--surface); border-radius: 10px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); overflow: hidden; }
.dec-row { display: grid; grid-template-columns: 70px 80px 80px 60px 70px 1fr; gap: 10px; padding: 9px 14px; border-bottom: 1px solid var(--border); font-size: 12px; align-items: center; font-family: var(--mono); }
.dec-row.header { font-size: 10px; letter-spacing: 1.5px; color: var(--text-muted); font-weight: 700; background: #f8fafc; text-transform: uppercase; }
.dec-row:not(.header):hover { background: #f8fafc; }
.dec-time { color: var(--text-muted); font-variant-numeric: tabular-nums; font-size: 11px; }
.dec-state-badge { font-size: 9px; padding: 3px 7px; border-radius: 4px; font-weight: 700; display: inline-block; letter-spacing: 1px; text-transform: uppercase; }
.dec-state-idle { background: #f1f5f9; color: var(--text-muted); }
.dec-state-heating { background: var(--success-soft); color: var(--success); }
.dec-state-cooldown, .dec-state-spike_cool { background: var(--warning-soft); color: var(--warning); }
.dec-state-safe_mode { background: var(--danger-soft); color: var(--danger); }
.dec-state-night_off { background: #1e293b; color: #cbd5e1; }
.dec-num { font-variant-numeric: tabular-nums; text-align: right; font-weight: 600; }
.dec-reason { color: var(--text-muted); font-size: 11px; font-family: var(--ui); }

.evt-row { display: grid; grid-template-columns: 70px 110px 1fr; gap: 10px; padding: 9px 14px; border-bottom: 1px solid var(--border); font-size: 12px; font-family: var(--mono); }
/* v4.3.0 NEW: Event filter chips */
.event-filters { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
.ef-chip { background: var(--surface); border: 1px solid var(--border); color: var(--text-muted); padding: 6px 12px; border-radius: 16px; font-family: var(--mono); font-size: 11px; font-weight: 700; cursor: pointer; transition: all 0.15s; letter-spacing: 0.3px; }
.ef-chip:hover { border-color: var(--primary); color: var(--primary); }
.ef-chip.active { background: var(--primary); border-color: var(--primary); color: white; }
.evt-type { font-size: 9px; letter-spacing: 1px; font-weight: 700; padding: 3px 7px; border-radius: 4px; display: inline-block; text-align: center; text-transform: uppercase; }
.evt-type.state_change { background: var(--primary-soft); color: var(--primary); }
.evt-type.heater_command, .evt-type.web_command { background: var(--solar-soft); color: #b45309; }
.evt-type.scene { background: var(--success-soft); color: var(--success); }
.evt-type.override { background: var(--purple-soft); color: var(--purple); }
.evt-type.cleaning_start, .evt-type.cleaning_stop { background: var(--teal-soft); color: var(--teal); }
.evt-details { color: var(--text); font-size: 12px; font-family: var(--ui); }

.flow-wrap { background: var(--surface); border-radius: 14px; padding: 28px; box-shadow: var(--shadow); border: 1px solid var(--border); margin-bottom: 20px; }

/* v4.3.0 NEW: Hero karta + mini cards + consumers list */
.flow-hero { background: var(--surface); border-radius: 14px; border: 1px solid var(--border); padding: 16px; margin-bottom: 12px; box-shadow: var(--shadow-sm); }
.flow-hero-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 14px; }
.flow-hero-block { flex: 1; }
.flow-hero-block.right { text-align: right; }
.flow-hero-label { font-family: var(--mono); font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 4px; }
.flow-hero-value { font-family: var(--mono); font-size: 26px; font-weight: 600; color: var(--text); line-height: 1; font-variant-numeric: tabular-nums; }
.flow-hero-value.flow-pv-color { color: var(--solar); }
.flow-hero-value.pos { color: var(--success); }
.flow-hero-value.neg { color: var(--danger); }

.flow-stack-bar { height: 32px; border-radius: 8px; display: flex; overflow: hidden; margin-bottom: 6px; background: #f1f5f9; }
.flow-stack-seg { display: flex; align-items: center; justify-content: center; color: white; font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: 0.5px; transition: width 0.4s ease; min-width: 0; overflow: hidden; white-space: nowrap; }
.flow-stack-seg.home { background: var(--text-muted); }
.flow-stack-seg.bat { background: var(--success); }
.flow-stack-seg.spa { background: var(--purple); }
.flow-stack-seg.hp { background: var(--warning); }
.flow-stack-seg.grid { background: var(--primary); }
.flow-stack-empty { padding: 8px 12px; font-family: var(--mono); font-size: 11px; color: var(--text-muted); text-align: center; width: 100%; }
.flow-stack-caption { font-family: var(--mono); font-size: 10px; color: var(--text-dim); text-align: center; letter-spacing: 0.5px; }

.flow-mini-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
.flow-mini-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; box-shadow: var(--shadow-sm); }
.flow-mini-head { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
.flow-mini-dot { width: 8px; height: 8px; border-radius: 50%; }
.flow-mini-label { font-family: var(--mono); font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; }
.flow-mini-value { font-family: var(--mono); font-size: 22px; font-weight: 600; color: var(--text); margin-bottom: 3px; line-height: 1; font-variant-numeric: tabular-nums; }
.flow-mini-sub { font-family: var(--mono); font-size: 11px; color: var(--text-muted); }
.flow-mini-sub.in { color: var(--danger); }
.flow-mini-sub.out { color: var(--success); }
.flow-mini-sub.charging { color: var(--success); }
.flow-mini-sub.discharging { color: var(--warning); }

.flow-consumers { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; box-shadow: var(--shadow-sm); margin-bottom: 12px; }
.flow-consumers-head { font-family: var(--mono); font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 10px; }
.flow-consumer-row { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border); }
.flow-consumer-row:last-child { border-bottom: none; }
.flow-consumer-left { display: flex; align-items: center; gap: 10px; }
.flow-consumer-emoji { font-size: 18px; }
.flow-consumer-name { font-size: 13px; color: var(--text); font-weight: 500; }
.flow-consumer-name-sub { font-family: var(--mono); font-size: 10px; color: var(--text-muted); margin-top: 1px; }
.flow-consumer-right { text-align: right; }
.flow-consumer-watt { font-family: var(--mono); font-size: 13px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
.flow-consumer-source { font-family: var(--mono); font-size: 10px; margin-top: 1px; }
.flow-consumer-source.solar { color: var(--success); }
.flow-consumer-source.bat { color: var(--success); }
.flow-consumer-source.grid { color: var(--danger); }
.flow-consumer-source.idle { color: var(--text-dim); }

.flow-phases { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; box-shadow: var(--shadow-sm); margin-bottom: 12px; }
.flow-phases-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.flow-phases-head > span:first-child { font-family: var(--mono); font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; }
.flow-phases-limit { font-family: var(--mono); font-size: 9px; color: var(--text-dim); }
.flow-phase-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.flow-phase-label { font-family: var(--mono); font-size: 11px; font-weight: 700; min-width: 32px; color: var(--text); }
.flow-phase-label.l2 { color: var(--purple); }
.flow-phase-bar-wrap { flex: 1; height: 6px; background: #f1f5f9; border-radius: 3px; overflow: hidden; }
.flow-phase-bar-fill { height: 100%; transition: width 0.4s, background 0.3s; border-radius: 3px; }
.flow-phase-watt { font-family: var(--mono); font-size: 11px; min-width: 60px; text-align: right; font-variant-numeric: tabular-nums; color: var(--text); font-weight: 600; }
.flow-phase-watt.warn { color: var(--warning); }
.flow-phase-watt.danger { color: var(--danger); }

.flow-diagram-wrap { margin-top: 10px; }
.flow-diagram-wrap summary { font-family: var(--mono); font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.5px; cursor: pointer; padding: 8px 12px; border-radius: 8px; background: var(--surface); border: 1px solid var(--border); margin-bottom: 8px; user-select: none; transition: background 0.15s; }
.flow-diagram-wrap summary:hover { background: #f8fafc; }
.flow-diagram-wrap[open] summary { margin-bottom: 8px; }

/* HP node v diagramu - novy */
.flow-node.hp { fill: #fff7ed; stroke: var(--warning); stroke-width: 2; }
.flow-line.hp { stroke: var(--warning); }
.flow-dot.hp { fill: var(--warning); }
.flow-svg { width: 100%; max-width: 700px; height: auto; margin: 0 auto; display: block; }
.flow-node { fill: var(--surface); stroke: var(--border); stroke-width: 2; transition: all 0.3s; }
.flow-node.active { stroke-width: 2.5; }
.flow-node.solar.active { stroke: var(--solar); fill: var(--solar-soft); }
.flow-node.battery.active { stroke: var(--success); fill: var(--success-soft); }
.flow-node.home.active { stroke: var(--text-muted); fill: #f1f5f9; }
.flow-node.grid.active { stroke: var(--danger); fill: var(--danger-soft); }
.flow-node.spa.active { stroke: var(--purple); fill: var(--purple-soft); }
.flow-icon { font-size: 24px; text-anchor: middle; }
.flow-label { font-family: var(--mono); font-size: 10px; font-weight: 700; fill: var(--text-muted); text-anchor: middle; letter-spacing: 1px; text-transform: uppercase; }
.flow-value { font-family: var(--mono); font-size: 14px; font-weight: 700; fill: var(--text); text-anchor: middle; font-variant-numeric: tabular-nums; }
.flow-sub { font-family: var(--mono); font-size: 9px; font-weight: 500; fill: var(--text-muted); text-anchor: middle; }
.flow-line { fill: none; stroke: var(--border); stroke-width: 2; transition: all 0.3s; }
.flow-line.active { stroke-width: 3; }
.flow-line.solar.active { stroke: var(--solar); } .flow-line.battery.active { stroke: var(--success); }
.flow-line.home.active { stroke: var(--text-muted); } .flow-line.grid.active { stroke: var(--danger); }
.flow-line.spa.active { stroke: var(--purple); }
.flow-dot { opacity: 0; transition: opacity 0.3s; }
.flow-dot.active { opacity: 1; }
.flow-dot.solar { fill: var(--solar); } .flow-dot.battery { fill: var(--success); }
.flow-dot.home { fill: var(--text-muted); } .flow-dot.grid { fill: var(--danger); } .flow-dot.spa { fill: var(--purple); }

.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat-card { background: var(--surface); border-radius: 12px; padding: 18px 20px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); }
.stat-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.stat-card-label { font-family: var(--mono); font-size: 10px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; letter-spacing: 1.5px; }
.stat-card-badge { font-family: var(--mono); font-size: 9px; padding: 3px 7px; background: var(--primary-soft); color: var(--primary); border-radius: 4px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; }
.stat-value { font-family: var(--mono); font-size: 32px; font-weight: 800; color: var(--text); letter-spacing: -1px; line-height: 1; font-variant-numeric: tabular-nums; margin-bottom: 4px; }
.stat-unit { font-size: 14px; color: var(--text-muted); font-weight: 500; margin-left: 3px; }
.stat-sub { font-family: var(--mono); font-size: 11px; color: var(--text-muted); margin-top: 6px; letter-spacing: 0.3px; }
.stat-bar { margin-top: 10px; height: 6px; background: #f1f5f9; border-radius: 3px; overflow: hidden; }
.stat-bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.stat-bar-fill.solar { background: var(--solar); } .stat-bar-fill.success { background: var(--success); }

/* Appliances (wife widget) - v4.3.0 redesign: kompaktni grid */
.app-header-card { background: var(--surface); border-radius: 12px; padding: 14px 18px; box-shadow: var(--shadow-sm); border: 1px solid var(--border); margin-bottom: 14px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.app-header-label { font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--text-muted); font-weight: 700; margin-bottom: 2px; }
.app-header-value { font-family: var(--mono); font-size: 20px; font-weight: 800; color: var(--text); font-variant-numeric: tabular-nums; }
/* v4.3.0: hustsi grid - 280px misto 240px ale 2 sloupce bezne */
.app-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px; }
.app-card { background: var(--surface); border-radius: 12px; padding: 14px 16px; border: 2px solid var(--border); box-shadow: var(--shadow-sm); transition: all 0.2s; position: relative; overflow: hidden; }
.app-card.green { border-color: var(--success); background: linear-gradient(135deg, #ffffff 0%, #f0fdf4 100%); }
.app-card.amber { border-color: var(--warning); background: linear-gradient(135deg, #ffffff 0%, #fff7ed 100%); }
.app-card.red { border-color: var(--danger); background: linear-gradient(135deg, #ffffff 0%, #fef2f2 100%); }
.app-card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.app-emoji { font-size: 26px; line-height: 1; flex-shrink: 0; }
.app-name-block { flex: 1; min-width: 0; }
.app-name { font-size: 14px; font-weight: 700; color: var(--text); line-height: 1.2; }
.app-name-meta { font-family: var(--mono); font-size: 10px; color: var(--text-muted); margin-top: 1px; }
.app-verdict { font-family: var(--mono); font-size: 11px; font-weight: 800; padding: 4px 9px; border-radius: 6px; letter-spacing: 0.8px; text-transform: uppercase; flex-shrink: 0; }
.app-verdict.green { background: var(--success); color: white; }
.app-verdict.amber { background: var(--warning); color: white; }
.app-verdict.red { background: var(--danger); color: white; }
.app-msg { font-family: var(--mono); font-size: 11px; color: var(--text-muted); line-height: 1.45; padding: 8px 10px; background: rgba(15,23,42,0.03); border-radius: 7px; margin-bottom: 8px; }
.app-coverage-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.app-coverage-bar { flex: 1; height: 6px; background: #f1f5f9; border-radius: 3px; overflow: hidden; }
.app-coverage-fill { height: 100%; border-radius: 3px; transition: width 0.4s; }
.app-coverage-fill.green { background: var(--success); }
.app-coverage-fill.amber { background: var(--warning); }
.app-coverage-fill.red { background: var(--danger); }
.app-coverage-text { font-family: var(--mono); font-size: 10px; color: var(--text-muted); font-weight: 700; min-width: 70px; text-align: right; }
.app-params { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; padding-top: 8px; border-top: 1px solid rgba(100,116,139,0.15); }
.app-param-label { font-family: var(--mono); font-size: 9px; color: var(--text-muted); letter-spacing: 0.5px; text-transform: uppercase; }
.app-param-val { font-family: var(--mono); font-size: 12px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
/* v4.3.0 NEW: energy flow pills - kde energie jde */
.app-energy-flow { display: flex; gap: 4px; flex-wrap: wrap; padding-top: 8px; border-top: 1px solid rgba(100,116,139,0.15); align-items: center; }
.ef-pill { font-family: var(--mono); font-size: 11px; font-weight: 700; padding: 4px 8px; border-radius: 5px; letter-spacing: 0.3px; }
.ef-pill.ef-solar { background: var(--solar-soft); color: #b45309; }
.ef-pill.ef-bat { background: var(--success-soft); color: var(--success); }
.ef-pill.ef-grid { background: var(--danger-soft); color: var(--danger); }

/* v4.3.0 NEW: Appliance learning - tlacitko + active stav */
.app-track-row { padding-top: 8px; margin-top: 6px; border-top: 1px solid rgba(100,116,139,0.15); display: flex; }
.app-track-btn { flex: 1; padding: 7px 10px; font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: 0.5px; border-radius: 7px; background: var(--surface); border: 1px solid var(--border); color: var(--text-muted); cursor: pointer; transition: all 0.15s; user-select: none; }
.app-track-btn:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-soft); }
.app-track-btn.stop { background: var(--danger); border-color: var(--danger); color: white; }
.app-track-btn.stop:hover { background: #b91c1c; border-color: #b91c1c; }
.app-card.is-tracking { border-color: var(--primary); border-width: 2px; box-shadow: 0 0 0 2px var(--primary-soft); }
.app-msg.learning-active { background: var(--primary-soft); color: var(--primary); font-family: var(--mono); font-weight: 600; padding: 6px 8px; border-radius: 6px; }
.app-profile-badge { display: inline-block; margin-left: 6px; font-family: var(--mono); font-size: 9px; font-weight: 700; padding: 2px 6px; border-radius: 4px; background: var(--success-soft); color: var(--success); letter-spacing: 0.3px; vertical-align: 1px; }

/* v4.3.0 NEW: Phase detail grid v Toky tabu */
.phase-detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.phase-detail-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; box-shadow: var(--shadow-sm); position: relative; overflow: hidden; }
.phase-detail-card.is-spa { border-color: var(--purple); border-width: 2px; }
.phase-detail-card.is-overload { border-color: var(--danger); border-width: 2px; }
.phase-detail-card::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
.phase-detail-card.phase-l1::before { background: var(--solar); }
.phase-detail-card.phase-l2::before { background: var(--purple); }
.phase-detail-card.phase-l3::before { background: var(--water); }
.phase-detail-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.phase-detail-label { font-family: var(--mono); font-size: 13px; font-weight: 800; color: var(--text); letter-spacing: 1.5px; }
.phase-detail-spa-badge { font-size: 9px; padding: 2px 6px; border-radius: 4px; background: var(--purple-soft); color: var(--purple); font-family: var(--mono); font-weight: 700; letter-spacing: 0.5px; }
.phase-detail-overload { font-size: 9px; padding: 2px 7px; border-radius: 4px; background: var(--danger); color: white; font-family: var(--mono); font-weight: 700; letter-spacing: 1px; animation: conn-blink 1s ease-in-out infinite; }
.phase-detail-load { font-family: var(--mono); font-size: 26px; font-weight: 700; color: var(--text); margin-bottom: 4px; line-height: 1; font-variant-numeric: tabular-nums; }
.phase-detail-load .pdl-unit { font-size: 14px; color: var(--text-muted); margin-left: 2px; }
.phase-detail-bar { height: 8px; background: #f1f5f9; border-radius: 4px; overflow: hidden; margin: 10px 0 6px; }
.phase-detail-bar-fill { height: 100%; transition: width 0.4s, background 0.3s; }
.phase-detail-meta { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 10px; color: var(--text-muted); margin-top: 6px; }
.phase-detail-grid-row { display: flex; justify-content: space-between; padding-top: 8px; margin-top: 8px; border-top: 1px solid var(--border); font-family: var(--mono); font-size: 11px; }
.phase-detail-grid-label { color: var(--text-muted); }
.phase-detail-grid-val { font-weight: 700; }
.phase-detail-grid-val.in { color: var(--danger); }
.phase-detail-grid-val.out { color: var(--success); }
.phase-detail-grid-val.zero { color: var(--text-muted); }

footer { text-align: center; color: var(--text-dim); font-size: 10px; margin-top: 30px; padding-top: 16px; font-family: var(--mono); letter-spacing: 1px; }

/* ============= v3.5 MOBILE FIRST REDESIGN ============= */
@media (max-width: 768px) {
  /* Container - víc místa, méně paddingu na bocích */
  .container {
    padding: max(10px, env(safe-area-inset-top)) 12px max(80px, calc(env(safe-area-inset-bottom) + 70px)) 12px;
  }

  /* Header - sticky, kompaktní */
  header {
    position: sticky;
    top: env(safe-area-inset-top);
    z-index: 100;
    padding: 10px 14px;
    margin-bottom: 12px;
    backdrop-filter: blur(10px);
    background: rgba(255, 255, 255, 0.92);
  }
  h1 { font-size: 17px; gap: 4px; }
  h1 .slash { margin: 0 4px; }
  h1 .sub { display: none; }  /* "BOJANOVICE" ulozit prostor */
  .status-pills { gap: 4px; }
  .pill { font-size: 9px; padding: 4px 8px; }

  /* TABS - skrýt horní, použít bottom nav */
  .tabs {
    display: none;  /* horní tab navigace skryta na mobile */
  }

  /* Bottom nav bar - hlavní mobile navigace */
  .mobile-nav {
    display: flex !important;
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 1000;
    background: rgba(255, 255, 255, 0.95);
    backdrop-filter: blur(20px);
    border-top: 1px solid var(--border);
    padding: 6px 0 max(6px, env(safe-area-inset-bottom)) 0;
    justify-content: space-around;
  }
  .mobile-nav-item {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    padding: 6px 4px;
    border: none;
    background: transparent;
    color: var(--text-muted);
    font-family: var(--mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
    min-height: 50px;
    border-radius: 8px;
    transition: all 0.15s;
  }
  .mobile-nav-item .nav-icon {
    font-size: 22px;
    line-height: 1;
  }
  .mobile-nav-item.active {
    color: var(--primary);
    background: var(--primary-soft);
  }
  .mobile-nav-item:active {
    transform: scale(0.95);
  }

  /* Section spacing - kompaktnější */
  .section { margin-bottom: 14px; }
  .section-title { margin-bottom: 6px; }
  .section-title h2 { font-size: 10px; letter-spacing: 1.5px; }

  /* Grid - 2 sloupce na mobile */
  .grid { grid-template-columns: 1fr 1fr; gap: 8px; }
  .tile { padding: 11px 13px; border-radius: 12px; }
  .tile::before { height: 4px; border-radius: 12px 12px 0 0; }
  .tile-label { font-size: 9px; margin-bottom: 4px; letter-spacing: 1px; }
  .tile-value { font-size: 24px; letter-spacing: -0.5px; }
  .tile-unit { font-size: 11px; }
  .tile-sub { font-size: 9px; margin-top: 4px; }

  /* Plan card - velké strategické tlačítko */
  .plan-card { padding: 14px 16px; border-radius: 14px; margin-bottom: 14px; }
  .plan-title { font-size: 9px; letter-spacing: 1.5px; }
  .plan-strategy { font-size: 24px; letter-spacing: -1px; }
  .plan-reason { font-size: 11px; margin-bottom: 12px; line-height: 1.4; }
  .plan-stats { grid-template-columns: 1fr 1fr; gap: 8px; }
  .plan-stat-label { font-size: 8px; letter-spacing: 1px; }
  .plan-stat-value { font-size: 16px; }
  .plan-stat-unit { font-size: 10px; }

  /* Charts - menší výška */
  .chart-wrap { height: 240px; padding: 10px; border-radius: 12px; }

  /* Control panel - velká tap targets */
  .scene-row { grid-template-columns: 1fr; gap: 8px; margin-bottom: 14px; }
  .scene-btn { padding: 14px 16px; border-radius: 12px; }
  .scene-title { font-size: 14px; margin-bottom: 4px; }
  .scene-desc { font-size: 11px; }

  /* Control rows - velké tlačítka, vertical layout */
  .control-panel { border-radius: 12px; }
  .control-row {
    flex-direction: row;  /* horizontal na mobile pro lepsi spacing */
    align-items: center;
    padding: 12px 14px;
    flex-wrap: wrap;
    gap: 8px;
  }
  .control-row > div:first-child { flex: 1; min-width: 130px; }
  .control-label { font-size: 13px; }
  .control-sub { font-size: 10px; margin-top: 1px; }
  .btn-group { gap: 5px; }
  .btn {
    padding: 10px 14px;
    font-size: 12px;
    min-height: 38px;
    min-width: 50px;
  }
  .temp-control { gap: 4px; }
  .temp-btn {
    width: 38px;
    height: 38px;
    font-size: 18px;
    border-radius: 8px;
  }
  .temp-val { font-size: 22px; min-width: 64px; }

  /* Decisions - karta místo tabulky */
  .dec-row {
    grid-template-columns: 1fr;
    gap: 4px;
    padding: 12px 14px;
    font-size: 12px;
  }
  .dec-row.header { display: none; }  /* hlavička skrytá */
  .dec-row > * { display: block !important; }  /* override předchozího display:none */
  .dec-row::before {
    content: '';
    display: grid;
    grid-template-columns: auto auto auto auto;
    gap: 6px;
  }
  .dec-row {
    display: grid;
    grid-template-areas:
      "time state"
      "metrics metrics"
      "reason reason";
    grid-template-columns: 60px 1fr;
  }
  .dec-row > *:nth-child(1) { grid-area: time; }
  .dec-row > *:nth-child(2) { grid-area: state; }
  .dec-row > *:nth-child(3),
  .dec-row > *:nth-child(4),
  .dec-row > *:nth-child(5) {
    display: inline-block !important;
    margin-right: 12px;
    font-size: 11px;
  }
  .dec-row > *:nth-child(3) { grid-area: metrics; }
  .dec-row > *:nth-child(4),
  .dec-row > *:nth-child(5) { display: none !important; }
  .dec-row > *:nth-child(6) { grid-area: reason; }
  .dec-reason { font-size: 11px; padding-top: 2px; line-height: 1.4; }

  /* Events - kompaktní */
  .evt-row {
    grid-template-columns: 56px 1fr;
    gap: 8px;
    padding: 10px 14px;
    font-size: 11px;
  }
  .evt-row > *:nth-child(2) { display: none; }
  .evt-details { font-size: 12px; line-height: 1.4; }
  .evt-row > *:first-child { font-size: 10px; }

  /* Flow diagram - menší, kompaktní */
  .flow-wrap { padding: 14px 8px; border-radius: 12px; }
  .flow-svg { max-width: 100%; }

  /* Stats grid - 2 sloupce */
  .stat-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .stat-card { padding: 14px 16px; border-radius: 12px; }
  .stat-card-label { font-size: 9px; letter-spacing: 1px; }
  .stat-value { font-size: 24px; }
  .stat-unit { font-size: 12px; }
  .stat-sub { font-size: 10px; }

  /* App-header-card (Lze pustit?) */
  .app-header-card { padding: 12px 14px; gap: 8px; border-radius: 12px; }
  .app-header-label { font-size: 9px; }
  .app-header-value { font-size: 18px; }

  /* v4.3.0 NEW: App cards - 2 sloupce i na telefonu, max kompaktni */
  .app-grid { grid-template-columns: 1fr 1fr; gap: 8px; }
  .app-card { padding: 10px 12px; border-radius: 12px; }
  .app-emoji { font-size: 22px; }
  .app-verdict { font-size: 10px; padding: 3px 7px; letter-spacing: 0.5px; }
  .app-name { font-size: 13px; }
  .app-name-meta { font-size: 9px; }
  .app-msg { font-size: 10px; padding: 6px 8px; line-height: 1.35; }
  .app-coverage-text { font-size: 9px; min-width: 60px; }
  .ef-pill { font-size: 10px; padding: 3px 6px; }
  .app-card-head { gap: 6px; margin-bottom: 6px; }

  /* Sanitizer 4-button group - lepší spacing */
  .control-row .btn-group {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .clean-progress-inline { margin-top: 8px; }
  .clean-prog-head { font-size: 10px; }
  .clean-prog-times { font-size: 8px; }

  /* Footer */
  footer { padding-top: 12px; font-size: 9px; }

  /* Offline banner - víc viditelný */
  .offline-banner { font-size: 10px; padding: 10px 14px; border-radius: 10px; }
}

/* v3.5 NEW: More menu (rozcestník v "Více" tabu) */
.more-menu {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.more-item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 16px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  cursor: pointer;
  font-family: inherit;
  text-align: left;
  width: 100%;
  transition: all 0.15s;
  box-shadow: var(--shadow-sm);
}
.more-item:hover, .more-item:active {
  border-color: var(--primary);
  transform: translateY(-1px);
  box-shadow: var(--shadow);
}
.more-icon { font-size: 26px; line-height: 1; }
.more-text { flex: 1; }
.more-title { font-size: 14px; font-weight: 700; color: var(--text); margin-bottom: 2px; }
.more-desc { font-size: 11px; color: var(--text-muted); font-family: var(--mono); }
.more-arrow { font-size: 22px; color: var(--text-dim); font-weight: 300; }

/* v3.7.3 NEW: Aktuální režim hero card */
.mode-card {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 16px;
  align-items: center;
  padding: 18px 20px;
  border-radius: 14px;
  background: var(--surface);
  border: 2px solid var(--border);
  box-shadow: var(--shadow-sm);
  transition: all 0.3s;
}
.mode-card.mode-solar {
  background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
  border-color: var(--solar);
}
.mode-card.mode-gentle {
  background: linear-gradient(135deg, #ffedd5 0%, #fed7aa 100%);
  border-color: var(--warning);
}
.mode-card.mode-heat {
  background: linear-gradient(135deg, #fee2e2 0%, #fecaca 100%);
  border-color: var(--danger);
}
.mode-card.mode-preshower {
  background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%);
  border-color: var(--primary);
}
.mode-card.mode-cleaning {
  background: linear-gradient(135deg, #f3e8ff 0%, #e9d5ff 100%);
  border-color: var(--purple);
}
.mode-card.mode-off {
  background: var(--surface);
  border-color: var(--border);
}
.mode-card-icon {
  font-size: 56px;
  text-align: center;
  line-height: 1;
}
.mode-card-title {
  font-family: var(--mono);
  font-size: 22px;
  font-weight: 800;
  color: var(--text);
  margin-bottom: 4px;
  letter-spacing: -0.5px;
}
.mode-card-desc {
  font-size: 13px;
  color: var(--text);
  margin-bottom: 6px;
  line-height: 1.4;
}
.mode-card-meta {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.3px;
}

/* Scheduler status bar - pod scenami */
.scheduler-status-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  margin-top: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  font-family: var(--mono);
  font-size: 11px;
}
.scheduler-status-text {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  letter-spacing: 0.3px;
}
.scheduler-status-bar.active .scheduler-status-text {
  color: var(--success);
  font-weight: 700;
}
.scheduler-status-bar.paused .scheduler-status-text {
  color: var(--warning);
}
.scheduler-next-inline {
  margin-left: 8px;
  color: var(--text-muted);
  font-weight: 400;
}
.btn-link {
  background: none;
  border: none;
  color: var(--primary);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  padding: 4px 8px;
  letter-spacing: 0.5px;
  border-radius: 4px;
}
.btn-link:hover { background: var(--primary-soft); }

/* Schedule help card - vysvetluje co plan dela */
.schedule-help-card {
  background: var(--primary-soft);
  border: 1px solid var(--primary);
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 14px;
  font-size: 12px;
  color: var(--text);
  line-height: 1.5;
}
.schedule-help-card strong { color: var(--primary); }

/* v4.1 NEW: User pill */
.pill.user-pill {
  background: #f1f5f9;
  color: var(--text);
  border: 1px solid var(--border);
}
.pill.user-pill:hover {
  background: var(--danger-soft);
  color: var(--danger);
}

/* v4.1.3 NEW: User cards */
.user-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 10px;
  box-shadow: var(--shadow-sm);
}
.user-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  flex-wrap: wrap;
  gap: 8px;
}
.user-card-name {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.user-name {
  font-family: var(--mono);
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
}
.user-badge-me {
  font-family: var(--mono);
  font-size: 9px;
  padding: 3px 7px;
  border-radius: 4px;
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 700;
  letter-spacing: 1px;
}
.user-role-badge {
  font-family: var(--mono);
  font-size: 9px;
  padding: 3px 7px;
  border-radius: 4px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.user-card-actions {
  display: flex;
  gap: 4px;
}
.btn-mini {
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.btn-mini:hover:not(:disabled) {
  border-color: var(--primary);
  background: var(--primary-soft);
}
.btn-mini:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}
.btn-mini-danger:hover {
  border-color: var(--danger) !important;
  background: var(--danger-soft) !important;
  color: var(--danger);
}
.user-card-info {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-muted);
}
.user-card-info > div {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.info-key {
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 1px;
  font-size: 10px;
  min-width: 100px;
}
.info-note {
  color: var(--text-dim);
  font-style: italic;
}
.user-role-select {
  font-family: var(--mono);
  font-size: 11px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
}
.user-role-select:hover {
  border-color: var(--primary);
}

/* Token show modal (jednou zobrazi nove vytvoreny token) */
.token-show-content {
  background: var(--surface);
  border-radius: 14px;
  padding: 24px;
  max-width: 500px;
  width: 100%;
  box-shadow: 0 10px 40px rgba(0,0,0,0.3);
}
.token-show-info {
  display: grid;
  grid-template-columns: 100px 1fr;
  gap: 8px;
  font-family: var(--mono);
  font-size: 12px;
  margin: 16px 0;
}
.token-show-info-key {
  color: var(--text-muted);
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 1px;
}
.token-show-value-box {
  background: #f8fafc;
  border: 2px dashed var(--primary);
  border-radius: 8px;
  padding: 14px;
  margin: 16px 0;
  font-family: var(--mono);
  font-size: 13px;
  word-break: break-all;
  color: var(--text);
  user-select: all;
}
.token-show-buttons {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
.token-show-buttons button {
  flex: 1;
  padding: 12px;
  border-radius: 8px;
  font-family: var(--mono);
  font-weight: 700;
  border: 1px solid var(--border);
  background: var(--surface);
  cursor: pointer;
  font-size: 12px;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.token-show-buttons .btn-primary {
  background: var(--primary);
  color: white;
  border-color: var(--primary);
}
.token-show-buttons .btn-primary:hover {
  background: #1d4ed8;
}

/* v4.0 NEW: Insights cards */
.insight-card {
  display: grid;
  grid-template-columns: 50px 1fr;
  gap: 14px;
  padding: 14px 16px;
  border-radius: 12px;
  margin-bottom: 8px;
  border: 1px solid var(--border);
}
.insight-card.severity-info {
  background: var(--primary-soft);
  border-color: #93c5fd;
}
.insight-card.severity-warn {
  background: var(--warning-soft);
  border-color: #fdba74;
}
.insight-card.severity-alert {
  background: var(--danger-soft);
  border-color: #fca5a5;
}
.insight-icon {
  font-size: 32px;
  text-align: center;
  line-height: 1;
}
.insight-title {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 4px;
  letter-spacing: 0.3px;
}
.insight-detail {
  font-size: 12px;
  color: var(--text);
  line-height: 1.5;
}

/* Digest content */
.digest-card {
  background: var(--surface);
  border-radius: 12px;
  padding: 20px 24px;
  border: 1px solid var(--border);
  box-shadow: var(--shadow-sm);
  margin-bottom: 14px;
}
.digest-header {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 700;
  color: var(--primary);
  margin-bottom: 6px;
  letter-spacing: 0.3px;
}
.digest-period {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 18px;
  letter-spacing: 0.5px;
}
.digest-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.digest-stat {
  background: #f8fafc;
  border-radius: 8px;
  padding: 12px 14px;
}
.digest-stat-label {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--text-muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  font-weight: 700;
  margin-bottom: 4px;
}
.digest-stat-value {
  font-family: var(--mono);
  font-size: 22px;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -0.5px;
  font-variant-numeric: tabular-nums;
}
.digest-stat-delta {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  margin-top: 3px;
}
.digest-stat-delta.up { color: var(--success); }
.digest-stat-delta.down { color: var(--warning); }
.digest-insights {
  background: var(--primary-soft);
  border-radius: 8px;
  padding: 14px 16px;
  border-left: 3px solid var(--primary);
}
.digest-insights-title {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 700;
  color: var(--primary);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.digest-insights-list {
  margin: 0; padding-left: 20px;
  font-size: 12px;
  color: var(--text);
  line-height: 1.6;
}

/* v3.9 NEW: Login overlay & modal */
.login-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(15, 23, 42, 0.85);
  backdrop-filter: blur(8px);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.login-modal {
  background: var(--surface);
  border-radius: 16px;
  padding: 32px;
  max-width: 380px;
  width: 100%;
  box-shadow: 0 20px 60px rgba(0,0,0,0.4);
}
.login-header h2 {
  font-family: var(--mono);
  font-size: 22px;
  margin: 0 0 4px 0;
  color: var(--text);
  letter-spacing: -0.5px;
}
.login-header p {
  margin: 0 0 22px 0;
  color: var(--text-muted);
  font-size: 13px;
}
.login-input {
  width: 100%;
  padding: 12px 14px;
  border: 2px solid var(--border);
  border-radius: 10px;
  font-family: var(--mono);
  font-size: 14px;
  margin-bottom: 12px;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}
.login-input:focus { border-color: var(--primary); }
.login-btn {
  width: 100%;
  padding: 12px;
  background: var(--primary);
  color: white;
  border: none;
  border-radius: 10px;
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.login-btn:hover { background: #1d4ed8; }
.login-error {
  margin-top: 10px;
  padding: 10px 12px;
  background: var(--danger-soft);
  color: var(--danger);
  border-radius: 8px;
  font-size: 12px;
  font-family: var(--mono);
}
.login-help {
  margin-top: 16px;
  padding: 12px;
  background: #f8fafc;
  border-radius: 8px;
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.5;
}
.login-help code {
  background: #e2e8f0;
  padding: 1px 5px;
  border-radius: 3px;
  font-family: var(--mono);
  font-size: 10px;
}

/* v3.7 NEW: Pre-shower UI */
.preshower-quick {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.btn-quick {
  flex: 1;
  min-width: 100px;
  padding: 14px 18px;
  background: var(--surface);
  border: 2px solid var(--border);
  border-radius: 10px;
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  cursor: pointer;
  transition: all 0.15s;
  letter-spacing: 0.5px;
}
.btn-quick:hover {
  border-color: var(--primary);
  color: var(--primary);
  transform: translateY(-1px);
}
.btn-quick:active { transform: scale(0.98); }
.btn-cancel {
  width: 100%;
  padding: 12px 16px;
  background: var(--danger-soft);
  border: 1px solid var(--danger);
  color: var(--danger);
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}
.btn-cancel:hover { background: var(--danger); color: white; }

.preshower-progress-wrap { margin-top: 14px; }
.preshower-progress-bar {
  height: 10px;
  background: rgba(255, 255, 255, 0.4);
  border-radius: 5px;
  overflow: hidden;
  margin-bottom: 10px;
}
.preshower-progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--solar) 0%, var(--success) 100%);
  border-radius: 5px;
  transition: width 1s linear;
  width: 0%;
}
.preshower-stages {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 4px;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  text-align: center;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}
.preshower-stage {
  padding: 8px 4px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.5);
  color: var(--text-muted);
  transition: all 0.3s;
}
.preshower-stage.active {
  background: var(--primary);
  color: white;
}
.preshower-stage.done {
  background: var(--success);
  color: white;
}

/* v3.6 NEW: Schedule UI */
.schedule-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}
.schedule-toggle input { display: none; }
.schedule-toggle-slider {
  position: relative;
  width: 38px;
  height: 22px;
  background: #cbd5e1;
  border-radius: 11px;
  transition: background 0.2s;
}
.schedule-toggle-slider::before {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  background: white;
  border-radius: 50%;
  transition: transform 0.2s;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}
.schedule-toggle input:checked + .schedule-toggle-slider {
  background: var(--success);
}
.schedule-toggle input:checked + .schedule-toggle-slider::before {
  transform: translateX(16px);
}
.schedule-toggle-label {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  color: var(--text);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.schedule-rule {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  margin-bottom: 8px;
  box-shadow: var(--shadow-sm);
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  align-items: center;
}
.schedule-rule.disabled { opacity: 0.5; }
.schedule-rule-name {
  font-family: var(--ui);
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 4px;
}
.schedule-rule-detail {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.3px;
}
.schedule-rule-scene {
  display: inline-block;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  margin-left: 6px;
}
.schedule-rule-scene.gentle { background: var(--solar-soft); color: #b45309; }
.schedule-rule-scene.solar_auto { background: var(--primary-soft); color: var(--primary); }
.schedule-rule-scene.heat_now { background: var(--purple-soft); color: var(--purple); }
.schedule-rule-scene.off { background: #f1f5f9; color: var(--text-muted); }

/* v3.6 NEW: Spot price */
.spot-bar-row {
  display: grid;
  grid-template-columns: 40px 1fr 60px;
  gap: 10px;
  align-items: center;
  padding: 6px 14px;
  border-bottom: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11px;
}
.spot-bar-row:last-child { border-bottom: none; }
.spot-bar-row.now { background: var(--primary-soft); font-weight: 700; }
.spot-bar-row.best { background: var(--success-soft); }
.spot-hour { color: var(--text-muted); }
.spot-bar-wrap {
  height: 18px;
  background: #f1f5f9;
  border-radius: 4px;
  overflow: hidden;
  position: relative;
}
.spot-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--success) 0%, var(--solar) 70%, var(--danger) 100%);
  border-radius: 4px;
  transition: width 0.3s;
}
.spot-bar-fill.cheap { background: var(--success); }
.spot-bar-fill.medium { background: var(--solar); }
.spot-bar-fill.expensive { background: var(--danger); }
.spot-price-val {
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--text);
}

/* Hide mobile nav on desktop */
.mobile-nav { display: none; }

/* Extra small phones (< 380px) - ještě kompaktnější */
@media (max-width: 380px) {
  .grid { grid-template-columns: 1fr; gap: 6px; }
  .stat-grid { grid-template-columns: 1fr; }
  .plan-stats { grid-template-columns: 1fr; }
  .tile-value { font-size: 22px; }
  .mobile-nav-item { font-size: 8px; }
  .mobile-nav-item .nav-icon { font-size: 19px; }
  h1 .slash { display: none; }
  /* App grid zustane 2 sloupce i tady - 380px / 2 = 190px na kartu, kompaktni layout to zvladne */
}
</style>
</head>
<body>
<!-- v3.9 NEW: Login modal (zobrazi se pokud auth_enabled a nejsme prihlaseni) -->
<div id="loginOverlay" class="login-overlay" style="display:none">
  <div class="login-modal">
    <div class="login-header">
      <h2>🔐 SolarGuard</h2>
      <p>Tato instance vyžaduje API token</p>
    </div>
    <input id="loginTokenInput" type="password" class="login-input"
           placeholder="Vlož API token..." autocomplete="current-password">
    <button class="login-btn" onclick="doLogin()">Přihlásit</button>
    <div id="loginError" class="login-error" style="display:none"></div>
    <div class="login-help">
      Token najdeš v <code>config.yaml</code> v sekci <code>api.token</code> nebo
      v journalctl po restartu (pokud nebyl konfigurován, vygeneroval se náhodný).
    </div>
  </div>
</div>

<div class="container">

<header>
  <h1><span>Solar</span><span class="mark">Guard</span><span class="slash">//</span><span class="sub">Bojanovice</span></h1>
  <div class="status-pills">
    <!-- v4.3.0 NEW: connection indicator - status MQTT/spa/api -->
    <span id="connectionPill" class="pill conn-pill conn-ok" title="Stav spojení">
      <span class="conn-dot"></span>
      <span id="connectionLabel">připojeno</span>
    </span>
    <span id="stateBadge" class="pill state-idle">—</span>
    <span id="dryRun" class="pill dry" style="display:none">DRY RUN</span>
    <span id="liveTag" class="pill live" style="display:none">● LIVE</span>
    <span id="overrideTag" class="pill override" style="display:none">OVERRIDE</span>
    <span id="cleaningTag" class="pill cleaning" style="display:none">🧼 CLEANING</span>
    <span id="gentleTag" class="pill gentle" style="display:none">🧒 MÍRNÝ 33°C</span>
    <span id="batFullTag" class="pill bat-full" style="display:none">🔋 FULL</span>
    <span id="userPill" class="pill user-pill" style="display:none; cursor: pointer;" onclick="logout()">👤 —</span>
  </div>
</header>

<!-- NEW v3.3: Banner pro offline / stale data -->
<div id="offlineBanner" class="offline-banner">⚠ Offline · zobrazena poslední data</div>

<nav class="tabs">
  <button class="tab active" data-tab="overview">Přehled</button>
  <button class="tab" data-tab="appliances">Lze pustit?</button>
  <button class="tab" data-tab="spot">Spot ceny</button>
  <button class="tab" data-tab="flow">Toky</button>
  <button class="tab" data-tab="stats">Stats</button>
  <button class="tab" data-tab="control">Vířivka</button>
  <button class="tab" data-tab="heatpump">Čerpadlo</button>
  <button class="tab" data-tab="schedule">Plán</button>
  <button class="tab" data-tab="digest">Týdenní</button>
  <button class="tab" data-tab="decisions">Rozhodnutí</button>
  <button class="tab" data-tab="events">Události</button>
</nav>

<div class="tab-content active" id="tab-overview">
  <!-- v4.0 NEW: Insights kartička - zobrazí se jen pokud máme insights -->
  <div id="insightsContainer" style="display:none; margin-bottom: 14px;"></div>
  <div id="planCard" class="plan-card unknown">
    <div class="plan-header">
      <span class="plan-title">Strategie dne</span>
      <span id="planStrategy" class="plan-strategy unknown">—</span>
    </div>
    <div id="planReason" class="plan-reason">Čekám na data…</div>
    <div class="plan-stats" id="planStats"></div>
    <div id="planAge" class="plan-stat-age"></div>
  </div>
  <div class="section"><div class="section-title"><h2>Aktuální stav</h2></div><div class="grid" id="tiles"></div></div>
  <div class="section"><div class="section-title"><h2>Počasí · Loxone meteostanice</h2></div><div class="grid" id="envTiles"></div></div>
  <div class="section"><div class="section-title"><h2>Historie</h2></div><div class="chart-wrap"><canvas id="chart"></canvas></div></div>
</div>

<div class="tab-content" id="tab-appliances">
  <div class="app-header-card">
    <div><div class="app-header-label">Přebytek</div><div class="app-header-value" id="appSurplus">—</div></div>
    <div><div class="app-header-label">SOC baterie</div><div class="app-header-value" id="appSOC">—</div></div>
    <div><div class="app-header-label">Strategie</div><div class="app-header-value" id="appStrat">—</div></div>
  </div>
  <div class="app-grid" id="appGrid"><div style="padding:20px;color:var(--text-muted);">Načítám…</div></div>
  <div style="text-align:center;margin-top:20px;color:var(--text-muted);font-family:var(--mono);font-size:10px;letter-spacing:1px;">
    🟢 ZELENÁ = jed · 🟠 ORANŽOVÁ = opatrně · 🔴 ČERVENÁ = radši počkat
  </div>
</div>

<!-- v4.3.0 NEW: Toky energie - kompletni redesign s hero kartou + cerpadlo -->
<div class="tab-content" id="tab-flow">
  <div class="section-title" style="margin-bottom: 8px;">
    <h2>Toky energie</h2>
    <span id="flowUpdateTime" style="font-family: var(--mono); font-size: 11px; color: var(--text-muted);">—</span>
  </div>

  <!-- HERO KARTA: aktuální výroba + stack-bar kam jde -->
  <div class="flow-hero">
    <div class="flow-hero-row">
      <div class="flow-hero-block">
        <div class="flow-hero-label">Aktuální výroba</div>
        <div class="flow-hero-value flow-pv-color" id="flowHeroPv">— W</div>
      </div>
      <div class="flow-hero-block right">
        <div class="flow-hero-label">Přebytek</div>
        <div class="flow-hero-value" id="flowHeroSurplus">— W</div>
      </div>
    </div>
    <div class="flow-stack-bar" id="flowStackBar">
      <div class="flow-stack-empty">Žádná aktuální produkce</div>
    </div>
    <div class="flow-stack-caption">kam aktuální FVE produkce směřuje</div>
  </div>

  <!-- BATTERY + GRID dvojkartou -->
  <div class="flow-mini-grid">
    <div class="flow-mini-card" id="flowMiniBat">
      <div class="flow-mini-head">
        <span class="flow-mini-dot" style="background: var(--success);"></span>
        <span class="flow-mini-label">Baterie</span>
      </div>
      <div class="flow-mini-value" id="flowBatSoc">—</div>
      <div class="flow-mini-sub" id="flowBatStatus">—</div>
    </div>
    <div class="flow-mini-card" id="flowMiniGrid">
      <div class="flow-mini-head">
        <span class="flow-mini-dot" style="background: var(--primary);"></span>
        <span class="flow-mini-label">Síť</span>
      </div>
      <div class="flow-mini-value" id="flowGridValue">—</div>
      <div class="flow-mini-sub" id="flowGridStatus">—</div>
    </div>
  </div>

  <!-- AKTIVNÍ SPOTŘEBIČE -->
  <div class="flow-consumers">
    <div class="flow-consumers-head">Aktivní spotřebiče</div>
    <div id="flowConsumersList"></div>
  </div>

  <!-- PER-FÁZE detail -->
  <div class="flow-phases">
    <div class="flow-phases-head">
      <span>Detail po fázích</span>
      <span class="flow-phases-limit" id="flowPhasesLimit">limit 3 500 W / fáze</span>
    </div>
    <div id="flowPhasesList"></div>
  </div>

  <!-- DIAGRAM (collapsable - zachovaný pro nostalgii a debug) -->
  <details class="flow-diagram-wrap">
    <summary>Schématický diagram</summary>
    <div class="flow-wrap">
      <svg class="flow-svg" viewBox="0 0 700 450" xmlns="http://www.w3.org/2000/svg">
        <path id="line-solar-battery" class="flow-line" d="M 210,100 Q 210,200 180,280" />
        <path id="line-solar-home" class="flow-line" d="M 280,100 Q 350,180 450,100" />
        <path id="line-solar-grid" class="flow-line" d="M 220,100 Q 150,150 90,100" />
        <path id="line-battery-home" class="flow-line" d="M 250,330 Q 350,330 450,160" />
        <path id="line-grid-home" class="flow-line" d="M 130,100 Q 250,80 440,100" />
        <path id="line-home-spa" class="flow-line" d="M 520,140 Q 580,200 580,290" />
        <path id="line-home-hp" class="flow-line" d="M 510,140 Q 460,250 380,330" />
        <circle id="dot-solar-battery" r="5" class="flow-dot solar"><animateMotion dur="2s" repeatCount="indefinite" begin="0s"><mpath href="#line-solar-battery"/></animateMotion></circle>
        <circle id="dot-solar-home" r="5" class="flow-dot solar"><animateMotion dur="2s" repeatCount="indefinite" begin="0.3s"><mpath href="#line-solar-home"/></animateMotion></circle>
        <circle id="dot-solar-grid" r="5" class="flow-dot solar"><animateMotion dur="2s" repeatCount="indefinite" begin="0.6s"><mpath href="#line-solar-grid"/></animateMotion></circle>
        <circle id="dot-battery-home" r="5" class="flow-dot battery"><animateMotion dur="2s" repeatCount="indefinite" begin="0s"><mpath href="#line-battery-home"/></animateMotion></circle>
        <circle id="dot-grid-home" r="5" class="flow-dot grid"><animateMotion dur="2s" repeatCount="indefinite" begin="0s"><mpath href="#line-grid-home"/></animateMotion></circle>
        <circle id="dot-home-spa" r="5" class="flow-dot spa"><animateMotion dur="2.5s" repeatCount="indefinite" begin="0s"><mpath href="#line-home-spa"/></animateMotion></circle>
        <circle id="dot-home-hp" r="5" class="flow-dot hp"><animateMotion dur="2.5s" repeatCount="indefinite" begin="0.5s"><mpath href="#line-home-hp"/></animateMotion></circle>
        <g id="node-solar"><rect class="flow-node solar" x="180" y="20" width="140" height="80" rx="10"/><text class="flow-icon" x="250" y="42">☀</text><text class="flow-label" x="250" y="58">Fotovoltaika</text><text class="flow-value" id="flow-pv" x="250" y="82">—</text><text class="flow-sub" id="flow-pv-sub" x="250" y="96">0 W</text></g>
        <g id="node-grid"><rect class="flow-node grid" x="20" y="20" width="120" height="80" rx="10"/><text class="flow-icon" x="80" y="42">⚡</text><text class="flow-label" x="80" y="58">Síť</text><text class="flow-value" id="flow-grid" x="80" y="82">—</text><text class="flow-sub" id="flow-grid-sub" x="80" y="96">0 W</text></g>
        <g id="node-home"><rect class="flow-node home" x="440" y="20" width="140" height="120" rx="10"/><text class="flow-icon" x="510" y="48">🏠</text><text class="flow-label" x="510" y="66">Spotřeba</text><text class="flow-value" id="flow-home" x="510" y="92">—</text><text class="flow-sub" id="flow-home-sub" x="510" y="108">0 W</text></g>
        <g id="node-battery"><rect class="flow-node battery" x="120" y="280" width="160" height="100" rx="10"/><text class="flow-icon" x="200" y="306">🔋</text><text class="flow-label" x="200" y="322">Baterie</text><text class="flow-value" id="flow-bat" x="200" y="348">—</text><text class="flow-sub" id="flow-bat-sub" x="200" y="364">0 W</text></g>
        <g id="node-spa"><rect class="flow-node spa" x="510" y="290" width="120" height="70" rx="10"/><text class="flow-icon" x="570" y="316">♨</text><text class="flow-label" x="570" y="332">Vířivka</text><text class="flow-value" id="flow-spa" x="570" y="350">—</text></g>
        <g id="node-hp"><rect class="flow-node hp" x="320" y="320" width="120" height="70" rx="10"/><text class="flow-icon" x="380" y="346">🔥</text><text class="flow-label" x="380" y="362">Čerpadlo</text><text class="flow-value" id="flow-hp" x="380" y="380">—</text></g>
      </svg>
    </div>
  </details>
</div>

<div class="tab-content" id="tab-stats">
  <div class="section"><div class="section-title"><h2>Dnes · VRM</h2></div><div class="stat-grid" id="statsTodayGrid"></div></div>
  <div class="section"><div class="section-title"><h2>Včera</h2></div><div class="stat-grid" id="statsYesterdayGrid"></div></div>
  <div class="section"><div class="section-title"><h2>Aktuální session</h2></div><div class="stat-grid" id="statsSessionGrid"></div></div>
  <div class="section"><div class="section-title"><h2>Předpověď radiace (dnes)</h2></div><div class="chart-wrap"><canvas id="forecastChart"></canvas></div></div>
</div>

<div class="tab-content" id="tab-control">
  <!-- v3.7.3 NEW: Aktuální režim hero card -->
  <div class="section">
    <div class="section-title"><h2>Aktuální režim</h2></div>
    <div id="currentModeCard" class="mode-card mode-solar">
      <div class="mode-card-icon" id="currentModeIcon">☀</div>
      <div class="mode-card-body">
        <div class="mode-card-title" id="currentModeTitle">Solar auto</div>
        <div class="mode-card-desc" id="currentModeDesc">Standard 38°C podle FVE přebytku</div>
        <div class="mode-card-meta" id="currentModeMeta">voda 28°C → cíl 38°C</div>
      </div>
    </div>
    <div class="scene-row scene-row-3" style="margin-top: 10px;">
      <button class="scene-btn" onclick="sceneSolarAuto()" id="sceneAutoBtn"><span class="scene-title">☀ Solar auto</span><span class="scene-desc">FVE řízeno · 38°C</span></button>
      <button class="scene-btn" onclick="sceneGentle()" id="sceneGentleBtn"><span class="scene-title">🧒 Mírný</span><span class="scene-desc">Pro děti · 33°C</span></button>
      <button class="scene-btn" onclick="sceneHeatNow()" id="sceneHeatBtn"><span class="scene-title">♨ Ohřát hned</span><span class="scene-desc">Override · 38°C</span></button>
    </div>
    <div id="schedulerStatusBar" class="scheduler-status-bar">
      <div class="scheduler-status-text">
        <span id="schedulerStatusIcon">⏸</span>
        <span id="schedulerStatusLabel">Plánovač pozastaven</span>
        <span id="schedulerNextTriggerInline" class="scheduler-next-inline"></span>
      </div>
      <button class="btn-link" onclick="showTab('schedule')">Pravidla →</button>
    </div>
  </div>

  <!-- v3.7 Pre-shower mode card - prejmenovano na "Jednorazova akce" -->
  <div class="section">
    <div class="section-title">
      <h2>🛁 Jednorázově: připrav na konkrétní čas</h2>
      <span id="preshowerHeatingPred" style="font-family: var(--mono); font-size: 11px; color: var(--text-muted);">—</span>
    </div>
    <div id="preshowerCard" class="plan-card unknown" style="margin-bottom: 14px;">
      <div id="preshowerIdle">
        <div class="plan-reason" style="margin-bottom: 12px;">Naplánuj přípravu vířivky - SolarGuard zapne topení s předstihem podle predikce a v T-5 min spustí bublinky.</div>
        <div class="preshower-quick">
          <button class="btn-quick" onclick="preshowerStart(60)">Za 1 h</button>
          <button class="btn-quick" onclick="preshowerStart(180)">Za 3 h</button>
          <button class="btn-quick" onclick="preshowerStart(360)">Za 6 h</button>
          <button class="btn-quick" onclick="preshowerStart(600)">Za 10 h</button>
          <button class="btn-quick" onclick="preshowerCustomTime()">Konkrétní čas…</button>
        </div>
        <div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted); margin-top: 12px; line-height: 1.5; letter-spacing: 0.3px;">
          ℹ Vířivka 1098 L potřebuje na ohřev 1°C cca 60 min. Pokud je voda 28°C a chceš 38°C,
          počítej s 8-12 hodinami podle počasí.
        </div>
      </div>
      <div id="preshowerRunning" style="display: none;">
        <div class="plan-header">
          <span class="plan-title">Připravuji vířivku</span>
          <span id="preshowerStateBadge" class="plan-strategy">—</span>
        </div>
        <div id="preshowerCountdown" class="plan-reason">—</div>
        <div class="preshower-progress-wrap">
          <div class="preshower-progress-bar">
            <div id="preshowerProgressFill" class="preshower-progress-fill"></div>
          </div>
          <div class="preshower-stages">
            <div class="preshower-stage" id="stageWarming">🔥 Topit</div>
            <div class="preshower-stage" id="stageBubbles">💨 Bublinky</div>
            <div class="preshower-stage" id="stageReady">✓ Hotovo</div>
          </div>
        </div>
        <button class="btn-cancel" onclick="preshowerCancel()" style="margin-top: 14px;">Zrušit přípravu</button>
      </div>
    </div>
  </div>

  <div class="section"><div class="section-title"><h2>Manuální ovládání</h2></div>
    <div class="control-panel">
      <div class="control-row">
        <div><div class="control-label">Topení ♨</div><div class="control-sub">Aktuální: <span id="heaterStatus">—</span></div></div>
        <div class="btn-group"><button class="btn" onclick="setHeater(true, event)" id="heaterOnBtn">ON</button><button class="btn" onclick="setHeater(false, event)" id="heaterOffBtn">OFF</button></div>
      </div>
      <div class="control-row">
        <div><div class="control-label">Filtrace</div><div class="control-sub">Aktuální: <span id="filterStatus">—</span></div></div>
        <div class="btn-group"><button class="btn" onclick="setFilter(true, event)" id="filterOnBtn">ON</button><button class="btn" onclick="setFilter(false, event)" id="filterOffBtn">OFF</button></div>
      </div>

      <!-- SANITIZER UVC - integrovaný s cleaning programy -->
      <div class="control-row">
        <div>
          <div class="control-label">Sanitizer UVC 🧼</div>
          <div class="control-sub">Aktuální: <span id="sanStatus">—</span></div>
        </div>
        <div class="btn-group">
          <button class="btn" onclick="setSanitizerOff()" id="sanOffBtn">OFF</button>
          <button class="btn" onclick="startCleaning(3)" id="clean3Btn">3h</button>
          <button class="btn" onclick="startCleaning(5)" id="clean5Btn">5h</button>
          <button class="btn" onclick="startCleaning(8)" id="clean8Btn">8h</button>
        </div>
      </div>
      <!-- Progress se zobrazí pouze při běžícím programu -->
      <div class="control-row-full" id="sanProgressRow" style="display:none">
        <div class="clean-progress-inline">
          <div class="clean-prog-head">
            <span id="sanProgLabel">3h program</span>
            <span id="sanProgRemaining">zbývá —</span>
          </div>
          <div class="clean-prog-bar"><div id="sanProgFill" class="clean-prog-fill" style="width:0%"></div></div>
          <div class="clean-prog-times">
            <span>start: <span id="sanProgStart">—</span></span>
            <span>konec: <span id="sanProgEnd">—</span></span>
          </div>
        </div>
      </div>

      <div class="control-row">
        <div><div class="control-label">Bublinky</div><div class="control-sub">Aktuální: <span id="bubblesStatus">—</span></div></div>
        <div class="btn-group"><button class="btn" onclick="setBubbles(true, event)" id="bubblesOnBtn">ON</button><button class="btn" onclick="setBubbles(false, event)" id="bubblesOffBtn">OFF</button></div>
      </div>
      <div class="control-row">
        <div><div class="control-label">Trysky</div><div class="control-sub">Aktuální: <span id="jetsStatus">—</span></div></div>
        <div class="btn-group"><button class="btn" onclick="setJets(true, event)" id="jetsOnBtn">ON</button><button class="btn" onclick="setJets(false, event)" id="jetsOffBtn">OFF</button></div>
      </div>
      <div class="control-row">
        <div><div class="control-label">Cílová teplota</div><div class="control-sub">Aktuální: <span id="tempStatus">—</span></div></div>
        <div class="temp-control"><button class="temp-btn" onclick="changeTemp(-1, event)">−</button><div class="temp-val" id="tempVal">—</div><button class="temp-btn" onclick="changeTemp(1, event)">+</button></div>
      </div>
    </div>
  </div>

  <div class="section"><div class="section-title"><h2>Konfigurace</h2></div><div id="cfgInfo" class="info-card"></div></div>
</div>

<div class="tab-content" id="tab-decisions">
  <!-- v4.3.0 NEW: Live engine status panel -->
  <div class="section">
    <div class="section-title"><h2>Stav rozhodovacího enginu</h2><span id="engineUpdateTime" style="font-family: var(--mono); font-size: 11px; color: var(--text-muted);">—</span></div>
    <div id="engineStatusGrid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px;">
      <!-- vyplni JS -->
    </div>
  </div>

  <!-- v4.3.0 NEW: Phases panel -->
  <div class="section">
    <div class="section-title"><h2>Zatížení fází (per-fáze ochrana)</h2></div>
    <div id="phasesPanel" class="info-card" style="padding: 16px;">
      <!-- vyplni JS -->
    </div>
  </div>

  <!-- v4.3.2 NEW: Napeti clanku z Seplos BMS RS485 -->
  <div class="section">
    <div class="section-title"><h2>Napětí článků baterie (Seplos BMS)</h2></div>
    <div id="cellsPanel" class="info-card" style="padding: 16px;">
      <div style="color:var(--text-muted);text-align:center;">načítám…</div>
    </div>
  </div>

  <!-- v4.3.2 NEW: Snapshoty napeti clanku pri SOC 99% (FULL) a 20% (LOW) -->
  <div class="section">
    <div class="section-title"><h2>Snapshoty FULL ↔ LOW <span style="font-size:10px;color:var(--text-muted);font-weight:normal">(odhalení slabého článku)</span></h2></div>
    <div id="snapshotsPanel" class="info-card" style="padding: 16px;">
      <div style="color:var(--text-muted);text-align:center;font-size:12px;">načítám snapshoty…</div>
    </div>
  </div>

  <!-- Existujici - posledni rozhodnuti -->
  <div class="section">
    <div class="section-title"><h2>Posledních 200 rozhodnutí</h2><a href="/api/export.csv" download class="download-btn">CSV</a></div>
    <div class="table-card">
      <div class="dec-row header"><div>Čas</div><div>Stav</div><div class="dec-num">Přebytek</div><div class="dec-num">SOC</div><div class="dec-num">Voda</div><div>Důvod</div></div>
      <div id="decisionsList"></div>
    </div>
  </div>
</div>

<!-- v4.3.0 NEW: Tab Cerpadlo - IVT AIR X 70 + Airmodul E9 pres Husdata H66 -->
<div class="tab-content" id="tab-heatpump">
  <div id="hpDisabled" style="display:none; padding: 20px; background: var(--surface); border-radius: 12px; border: 1px solid var(--border); text-align: center; color: var(--text-muted);">
    <div style="font-size: 36px; margin-bottom: 8px;">🔌</div>
    <div style="font-weight: 600; margin-bottom: 6px;">Tepelné čerpadlo není nakonfigurováno</div>
    <div style="font-size: 12px;">V config.yaml zapni <code>heatpump.enabled: true</code> a doplň MQTT broker + Husdata H66 mac adresu + register mapping.</div>
  </div>

  <div id="hpContent" style="display:none">
    <!-- Hlavni stav karta -->
    <div class="section">
      <div class="section-title">
        <h2>Stav čerpadla</h2>
        <span id="hpEngineState" style="font-family: var(--mono); font-size: 11px; color: var(--text-muted);">—</span>
      </div>

      <div class="grid" id="hpStateGrid">
        <!-- vyplni JS: outdoor, indoor, supply, return, hot_water, power -->
      </div>
    </div>

    <!-- Scény ovládání -->
    <div class="section">
      <div class="section-title"><h2>Scény</h2></div>
      <div class="scene-row" style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
        <button class="scene-btn" onclick="hpSceneAuto()" id="hpSceneAutoBtn">
          <span class="scene-title">☀ Auto (solar)</span>
          <span class="scene-desc">SolarGuard řídí podle slunce a baterie</span>
        </button>
        <button class="scene-btn" onclick="hpSceneBoost()" id="hpSceneBoostBtn">
          <span class="scene-title">🚀 Solar boost</span>
          <span class="scene-desc">Zvýš TUV + topení, blokuj el. dohřev</span>
        </button>
        <button class="scene-btn" onclick="hpSceneHeat()" id="hpSceneHeatBtn">
          <span class="scene-title">🔥 Topení</span>
          <span class="scene-desc">Klasický topný režim</span>
        </button>
        <button class="scene-btn" onclick="hpSceneCool()" id="hpSceneCoolBtn">
          <span class="scene-title">❄ Chlazení</span>
          <span class="scene-desc">Letní režim - klimatizace</span>
        </button>
      </div>
    </div>

    <!-- Manualni ovladani -->
    <div class="section">
      <div class="section-title"><h2>Manuální ovládání</h2></div>
      <div class="control-panel">
        <div class="control-row">
          <div>
            <div class="control-label">Cílová TUV (°C)</div>
            <div class="control-sub">Aktuální: <span id="hpHwTarget">—</span> · měřená: <span id="hpHwActual">—</span></div>
          </div>
          <div class="temp-control">
            <button class="temp-btn" onclick="hpChangeHw(-1, event)">−</button>
            <div class="temp-val" id="hpHwVal">—</div>
            <button class="temp-btn" onclick="hpChangeHw(1, event)">+</button>
          </div>
        </div>

        <div class="control-row">
          <div>
            <div class="control-label">Cílová pokoj (°C)</div>
            <div class="control-sub">Aktuální: <span id="hpRoomTarget">—</span> · měřená: <span id="hpRoomActual">—</span></div>
          </div>
          <div class="temp-control">
            <button class="temp-btn" onclick="hpChangeRoom(-0.5, event)">−</button>
            <div class="temp-val" id="hpRoomVal">—</div>
            <button class="temp-btn" onclick="hpChangeRoom(0.5, event)">+</button>
          </div>
        </div>

        <div class="control-row">
          <div>
            <div class="control-label">Elektrický dohřev</div>
            <div class="control-sub">Status: <span id="hpAuxStatus">—</span></div>
          </div>
          <div class="btn-group">
            <button class="btn" onclick="hpBlockAux(true, event)" id="hpAuxBlockBtn">BLOKOVAT</button>
            <button class="btn" onclick="hpBlockAux(false, event)" id="hpAuxAllowBtn">POVOLIT</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Detailni info -->
    <div class="section">
      <div class="section-title"><h2>Provozní data</h2></div>
      <div class="info-card" id="hpInfoCard">
        <!-- vyplni JS: COP, energy today, mode, alarm -->
      </div>
    </div>
  </div>
</div>

<!-- v3.6 NEW: Plán scén (v3.7.3 redesign) -->
<div class="tab-content" id="tab-schedule">
  <div class="section">
    <div class="section-title">
      <h2>Automatické přepínání režimů</h2>
      <label class="schedule-toggle" id="scheduleToggleWrap" style="display:none">
        <input type="checkbox" id="scheduleGlobalToggle">
        <span class="schedule-toggle-slider"></span>
        <span class="schedule-toggle-label">Aktivní</span>
      </label>
    </div>
    <div class="schedule-help-card">
      ℹ Plánovač automaticky přepíná stejné režimy které najdeš v tabu <strong>Vířivka</strong> v daný čas a den.
      Když je vypnutý, režim se mění jen ručně. Override "Ohřát hned" má vždy přednost před plánem.
    </div>
    <div id="scheduleStatus" class="info-card" style="margin-bottom: 14px; padding: 14px 16px;">
      <div style="font-family: var(--mono); font-size: 11px; color: var(--text-muted); letter-spacing: 0.5px;">načítám...</div>
    </div>
    <div id="scheduleRules"></div>
    <div id="scheduleEmpty" style="display:none; padding: 28px 20px; text-align: center; background: var(--surface); border-radius: 12px; border: 1px dashed var(--border);">
      <div style="font-size: 36px; margin-bottom: 8px;">📅</div>
      <div style="font-family: var(--mono); font-size: 12px; color: var(--text-muted); line-height: 1.6;">
        Plánovač není nakonfigurován.<br>
        Přidej pravidla v <code>config.yaml</code> sekce <code>schedule:</code>.
      </div>
    </div>
  </div>
</div>

<!-- v3.6 NEW: Spot ceny -->
<div class="tab-content" id="tab-spot">
  <div class="section">
    <div class="section-title"><h2>Spotová cena dnes</h2></div>
    <div id="spotCurrentCard" class="plan-card unknown" style="margin-bottom: 14px;">
      <div class="plan-header">
        <span class="plan-title">Aktuální cena</span>
        <span id="spotPriceNow" class="plan-strategy">—</span>
      </div>
      <div id="spotPriceReason" class="plan-reason">načítám OTE-CR…</div>
      <div class="plan-stats" id="spotStats"></div>
    </div>
  </div>
  <div class="section">
    <div class="section-title"><h2>Hodinové ceny dnes (Kč/kWh)</h2></div>
    <div class="chart-wrap"><canvas id="spotChart"></canvas></div>
  </div>
  <div class="section">
    <div class="section-title"><h2>Nejlepší hodiny dnes</h2></div>
    <div id="bestHoursList" class="info-card"></div>
  </div>
</div>

<!-- v4.1 NEW: Uživatelé (owner only) -->
<div class="tab-content" id="tab-users">
  <div class="section">
    <div class="section-title">
      <h2>Správa uživatelů</h2>
      <button class="download-btn" onclick="showCreateUserDialog()">+ Přidat</button>
    </div>
    <div class="schedule-help-card">
      ℹ <strong>Owner</strong> = plný přístup vč. správy uživatelů.
      <strong>Family</strong> = ovládá vířivku + vidí data.
      <strong>Guest</strong> = jen čtení (žádné commands).
      Každý uživatel má vlastní token, který lze kdykoli regenerovat nebo smazat.
    </div>
    <div id="usersList"></div>
    <div id="createUserModal" class="login-overlay" style="display:none">
      <div class="login-modal">
        <div class="login-header">
          <h2>+ Přidat uživatele</h2>
          <p>Token byl vygenerován automaticky. Můžeš ho přepsat, ale doporučujeme nechat.</p>
        </div>
        <input id="newUserName" type="text" class="login-input" placeholder="Jméno (jen a-Z, 0-9, _, -)" autocomplete="off" maxlength="32">
        <select id="newUserRole" class="login-input" style="font-family: var(--ui);">
          <option value="family">family - ovládá vířivku, vidí data</option>
          <option value="guest">guest - jen čtení</option>
          <option value="owner">owner - plný přístup</option>
        </select>
        <div style="display: flex; gap: 8px; margin-bottom: 12px;">
          <input id="newUserToken" type="text" class="login-input" placeholder="Token..." autocomplete="off" style="margin-bottom: 0; flex: 1; font-family: var(--mono); font-size: 11px;">
          <button class="btn" onclick="generateNewUserToken()" style="white-space: nowrap;">↻</button>
        </div>
        <div style="display: flex; gap: 8px;">
          <button class="login-btn" onclick="createUser()" style="flex: 1;">Přidat</button>
          <button class="login-btn" onclick="hideCreateUserDialog()" style="flex: 1; background: var(--text-muted);">Zrušit</button>
        </div>
        <div id="createUserError" class="login-error" style="display:none"></div>
      </div>
    </div>
    <!-- v4.1.3 NEW: Token show modal (zobrazi nove vytvoreny token s copy buttonem) -->
    <div id="tokenShowModal" class="login-overlay" style="display:none">
      <div class="token-show-content">
        <h2 id="tokenShowTitle" style="font-family: var(--mono); font-size: 18px; font-weight: 800; color: var(--text); margin: 0 0 4px 0;">✓ Uživatel vytvořen</h2>
        <p style="font-size: 12px; color: var(--text-muted); margin: 0;">Token vidíš jen tady. SolarGuard ho ukládá jen jako hash.</p>
        <div class="token-show-info">
          <div class="token-show-info-key">Jméno:</div>
          <div id="tokenShowName" style="font-weight: 700;">—</div>
          <div class="token-show-info-key">Role:</div>
          <div id="tokenShowRole">—</div>
        </div>
        <div class="token-show-info-key" style="font-family: var(--mono); font-size: 10px;">Token (klepni na text, vybere se celý):</div>
        <div id="tokenShowValue" class="token-show-value-box">—</div>
        <div id="tokenShowExtra" style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;"></div>
        <div class="token-show-buttons">
          <button id="tokenCopyBtn" class="btn-primary" onclick="copyTokenToClipboard()">📋 Kopírovat</button>
          <button id="tokenShowCloseBtn" onclick="hideTokenDialog()">Zavřít</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- v4.1 NEW: Týdenní digest -->
<div class="tab-content" id="tab-digest">
  <div class="section">
    <div class="section-title">
      <h2>Týdenní souhrn</h2>
      <button class="download-btn" onclick="generateDigestNow()">⚡ Generovat teď</button>
    </div>
    <div class="schedule-help-card">
      ℹ Souhrn se generuje automaticky každou neděli v 18:00 (poslední 7 dní).
      Můžeš ho pustit ručně tlačítkem výše.
    </div>
    <div id="digestContent">
      <div style="padding: 28px 20px; text-align: center; background: var(--surface); border-radius: 12px; border: 1px dashed var(--border);">
        <div style="font-size: 36px; margin-bottom: 8px;">📊</div>
        <div style="font-family: var(--mono); font-size: 12px; color: var(--text-muted); line-height: 1.6;">
          Načítám digest...
        </div>
      </div>
    </div>
  </div>
</div>

<div class="tab-content" id="tab-events">
  <div class="section">
    <div class="section-title">
      <h2>Události</h2>
      <a href="/api/events.csv" download class="download-btn">CSV</a>
    </div>
    <!-- v4.3.0 NEW: Filter chips -->
    <div id="eventFilters" class="event-filters">
      <button class="ef-chip active" data-filter="all">Vše</button>
      <button class="ef-chip" data-filter="state_change">Stav</button>
      <button class="ef-chip" data-filter="heater_command">Topení</button>
      <button class="ef-chip" data-filter="web_command">Ovládání</button>
      <button class="ef-chip" data-filter="scene">Scény</button>
      <button class="ef-chip" data-filter="cleaning_start,cleaning_stop">Čištění</button>
      <button class="ef-chip" data-filter="preshower_start,preshower_ready,preshower_end">Příprava</button>
      <button class="ef-chip" data-filter="override">Override</button>
    </div>
    <div class="table-card" id="eventsList"></div>
  </div>
</div>

<!-- v3.5 NEW: Mobile "Více" tab - rozcestník -->
<div class="tab-content" id="tab-more">
  <div class="section">
    <div class="section-title"><h2>Detaily</h2></div>
    <div class="more-menu">
      <button class="more-item" data-tab="stats">
        <span class="more-icon">📊</span>
        <div class="more-text">
          <div class="more-title">Statistiky</div>
          <div class="more-desc">Dnes, včera, session - výroba a spotřeba</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
      <button class="more-item" data-tab="flow">
        <span class="more-icon">⚡</span>
        <div class="more-text">
          <div class="more-title">Toky energie</div>
          <div class="more-desc">Realtime diagram FV / baterie / dům</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
      <button class="more-item" data-tab="schedule">
        <span class="more-icon">📅</span>
        <div class="more-text">
          <div class="more-title">Časový plán <span style="font-size: 9px; color: var(--text-muted); font-weight: 400;">pokročilé</span></div>
          <div class="more-desc">Automatické přepínání režimů v daný čas</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
      <button class="more-item" data-tab="heatpump">
        <span class="more-icon">🔥</span>
        <div class="more-text">
          <div class="more-title">Tepelné čerpadlo</div>
          <div class="more-desc">IVT AIR X 70 - topení, chlazení, TUV</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
      <button class="more-item" data-tab="decisions">
        <span class="more-icon">🧠</span>
        <div class="more-text">
          <div class="more-title">Rozhodnutí</div>
          <div class="more-desc">Posledních 200 ticků s důvody</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
      <button class="more-item" data-tab="events">
        <span class="more-icon">📋</span>
        <div class="more-text">
          <div class="more-title">Události</div>
          <div class="more-desc">State changes, scény, příkazy</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
      <!-- v4.1 NEW: Uzivatele - owner only, JS schova pokud neni owner -->
      <button class="more-item owner-only" data-tab="users" id="moreItemUsers" style="display:none">
        <span class="more-icon">👥</span>
        <div class="more-text">
          <div class="more-title">Uživatelé <span style="font-size: 9px; color: var(--text-muted); font-weight: 400;">owner</span></div>
          <div class="more-desc">Spravuj rodinné a hostovské tokeny</div>
        </div>
        <span class="more-arrow">›</span>
      </button>
    </div>
  </div>

  <div class="section">
    <div class="section-title"><h2>Konfigurace</h2></div>
    <div id="cfgInfoMobile" class="info-card"></div>
  </div>

  <!-- v3.8 NEW: InfluxDB status -->
  <div class="section">
    <div class="section-title"><h2>🗄 Long-term storage</h2></div>
    <div id="influxStatusCard" class="info-card"></div>
  </div>

  <div class="section" style="text-align: center; margin-top: 24px;">
    <div style="font-family: var(--mono); font-size: 9px; color: var(--text-dim); letter-spacing: 1px;">
      SolarGuard v4.3.0 · Bojanovice
    </div>
    <div style="font-family: var(--mono); font-size: 9px; color: var(--text-dim); margin-top: 4px;">
      auto-refresh 5 s
    </div>
  </div>
</div>

<footer>SolarGuard v4.3.0 · Bojanovice · auto-refresh 5 s</footer>
</div>

<!-- v3.5 NEW: Mobile bottom navigation -->
<nav class="mobile-nav" id="mobileNav">
  <button class="mobile-nav-item active" data-tab="overview">
    <span class="nav-icon">🏠</span>
    <span>Přehled</span>
  </button>
  <button class="mobile-nav-item" data-tab="appliances">
    <span class="nav-icon">🧺</span>
    <span>Pustit</span>
  </button>
  <button class="mobile-nav-item" data-tab="control">
    <span class="nav-icon">♨</span>
    <span>Vířivka</span>
  </button>
  <button class="mobile-nav-item" data-tab="spot">
    <span class="nav-icon">💰</span>
    <span>Spot</span>
  </button>
  <button class="mobile-nav-item" data-tab="more">
    <span class="nav-icon">⚙</span>
    <span>Více</span>
  </button>
</nav>

<script>
// NEW v3.3: PWA Service Worker registrace
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .then(reg => console.log('SW registered, scope:', reg.scope))
      .catch(err => console.error('SW registration failed:', err));
  });
}

// NEW v3.3: Online/offline indikátor
function updateOfflineBanner(isOffline, isStale, ageSec) {
  const b = document.getElementById('offlineBanner');
  if (!b) return;
  if (isOffline) {
    b.textContent = '⚠ Offline · zobrazena poslední data' + (ageSec != null ? ` (před ${Math.round(ageSec/60)} min)` : '');
    b.className = 'offline-banner visible cached';
  } else if (isStale) {
    b.textContent = '⏱ Připojení obnoveno · načítám čerstvá data…';
    b.className = 'offline-banner visible';
    setTimeout(() => { b.className = 'offline-banner'; }, 2000);
  } else {
    b.className = 'offline-banner';
  }
}

window.addEventListener('online', () => updateOfflineBanner(false, true));
window.addEventListener('offline', () => updateOfflineBanner(true, false));

const fmt = (v, unit, digits) => {
  unit = unit || ''; digits = digits == null ? 0 : digits;
  if (v == null) return '—';
  if (typeof v === 'number') return v.toFixed(digits) + unit;
  return v;
};
const fmtTime = ts => {
  const d = new Date(ts * 1000);
  return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0') + ':' + d.getSeconds().toString().padStart(2,'0');
};
const fmtTimeShort = ts => {
  const d = new Date(ts * 1000);
  return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
};
const fmtDuration = sec => {
  if (sec == null || sec <= 0) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
};
const fmtAge = sec => {
  if (sec == null) return '';
  if (sec < 60) return `před ${Math.round(sec)}s`;
  if (sec < 3600) return `před ${Math.round(sec/60)}m`;
  return `před ${Math.round(sec/3600)}h`;
};

const stateNames = { idle: 'v klidu', heating: 'topí', cooldown: 'chladne', spike_cool: 'cooldown', safe_mode: 'bezpeč', night_off: 'noc' };
const strategyNames = { aggressive: 'AGRESIVNÍ', normal: 'NORMÁL', conservative: 'OPATRNÝ', survive: 'ŠETŘÍM', unknown: 'ČEKÁM' };
const verdictText = { green: 'JEĎ', amber: 'OPATRNĚ', red: 'POČKEJ' };

let chart = null, forecastChart = null;
// v4.3.0 NEW: PWA shortcuts deep link - precti ?tab=X z URL
const _initTab = (() => {
  try {
    const urlParams = new URLSearchParams(window.location.search);
    const t = urlParams.get('tab');
    const validTabs = ['overview', 'flow', 'appliances', 'control', 'heatpump', 'spot', 'stats', 'decisions', 'events', 'schedule', 'digest', 'users'];
    if (t && validTabs.includes(t)) return t;
  } catch (e) {}
  return 'overview';
})();
let activeTab = _initTab;
let lastState = null;

// v3.5: Společná funkce pro switch tabů (desktop tabs + mobile nav + more menu)
function switchTab(tabName) {
  // Desktop tabs
  document.querySelectorAll('.tab').forEach(x => {
    x.classList.toggle('active', x.dataset.tab === tabName);
  });
  // Mobile nav items
  document.querySelectorAll('.mobile-nav-item').forEach(x => {
    x.classList.toggle('active', x.dataset.tab === tabName);
  });
  // Tab content
  document.querySelectorAll('.tab-content').forEach(x => {
    x.classList.toggle('active', x.id === 'tab-' + tabName);
  });
  activeTab = tabName;
  refresh();
  // Scroll na top
  window.scrollTo(0, 0);
}

document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});

// v3.5 NEW: Mobile bottom nav
document.querySelectorAll('.mobile-nav-item').forEach(t => {
  t.addEventListener('click', () => {
    // Mapovat "Vířivka" tab na control (pretty name na uživatele)
    let target = t.dataset.tab;
    switchTab(target);
  });
});

// v3.5 NEW: More menu items (rozcestník)
document.querySelectorAll('.more-item').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});

// v3.9 NEW: Token management (v4.1: + role)
let _authToken = localStorage.getItem('solarguard_token') || null;
let _authChecked = false;
let _currentUser = null;   // {name, role}

function getAuthHeaders() {
  const headers = {'Content-Type': 'application/json'};
  if (_authToken) headers['Authorization'] = 'Bearer ' + _authToken;
  return headers;
}

function showLoginModal(errorMsg) {
  document.getElementById('loginOverlay').style.display = 'flex';
  const err = document.getElementById('loginError');
  if (errorMsg) {
    err.textContent = errorMsg;
    err.style.display = 'block';
  } else {
    err.style.display = 'none';
  }
  setTimeout(() => document.getElementById('loginTokenInput').focus(), 100);
}
function hideLoginModal() {
  document.getElementById('loginOverlay').style.display = 'none';
  document.getElementById('loginError').style.display = 'none';
}

async function doLogin() {
  const token = document.getElementById('loginTokenInput').value.trim();
  if (!token) { showLoginModal('Zadej token'); return; }
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token})
    });
    if (!r.ok) {
      const txt = await r.text();
      showLoginModal('Neplatný token: ' + txt);
      return;
    }
    const data = await r.json();
    _authToken = token;
    _currentUser = {name: data.user, role: data.role};
    localStorage.setItem('solarguard_token', token);
    localStorage.setItem('solarguard_user', JSON.stringify(_currentUser));
    hideLoginModal();
    applyRoleVisibility();
    refresh();
  } catch (e) {
    showLoginModal('Chyba: ' + e.message);
  }
}

async function checkAuth() {
  try {
    const r = await fetch('/api/auth/status', { headers: getAuthHeaders() });
    const data = await r.json();
    _authChecked = true;
    if (data.auth_enabled && !data.authenticated) {
      _currentUser = null;
      showLoginModal();
      return false;
    }
    if (data.user) {
      _currentUser = {name: data.user, role: data.role};
      localStorage.setItem('solarguard_user', JSON.stringify(_currentUser));
    }
    hideLoginModal();
    applyRoleVisibility();
    return true;
  } catch (e) {
    return true;  // pokud check selze, zkus normalne
  }
}

// v4.1 NEW: schova UI prvky které vyžadují vyšší roli
function applyRoleVisibility() {
  const role = (_currentUser && _currentUser.role) || 'guest';
  // Owner-only prvky (správa uživatelů)
  document.querySelectorAll('.owner-only').forEach(el => {
    el.style.display = (role === 'owner') ? '' : 'none';
  });
  // Guest = read-only - zakaž write tlačítka (heater, scenes, atd)
  const isGuest = role === 'guest';
  document.querySelectorAll('button.btn, .scene-btn, button.btn-quick, button.temp-btn').forEach(b => {
    if (isGuest && !b.classList.contains('login-btn') && !b.classList.contains('download-btn')) {
      b.disabled = true;
      b.title = 'Guest role - read only';
    }
  });
}

async function logout() {
  if (!confirm('Odhlásit z SolarGuard?')) return;
  try {
    await fetch('/api/auth/logout', {method: 'POST'});
  } catch (e) {}
  _authToken = null;
  _currentUser = null;
  localStorage.removeItem('solarguard_token');
  localStorage.removeItem('solarguard_user');
  location.reload();
}

async function apiPost(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: getAuthHeaders(),
      body: body ? JSON.stringify(body) : '{}'
    });
    if (r.status === 401) {
      _authToken = null;
      localStorage.removeItem('solarguard_token');
      showLoginModal('Token vypršel, přihlas se znovu');
      throw new Error('Unauthorized');
    }
    if (!r.ok) { const txt = await r.text(); throw new Error('HTTP ' + r.status + ': ' + txt); }
    const data = await r.json();
    setTimeout(refresh, 500);
    return data;
  } catch (e) {
    if (e.message !== 'Unauthorized') toast('Chyba: ' + e.message, 'error');
  }
}

// v4.3.0 NEW: Optimistic UI pro vsechny spa toggly.
// Drive jsi musel cekat 5s na refresh aby UI ukazalo novy stav -
// virivka uz reagovala (slyset bublinky) ale tlacitko stale OFF.
// Reseni: hned aktualizujeme lastState + UI, pak posleme request a po
// uspesnem returnu volame plny refresh pro overeni.
function optimisticSpaUpdate(field, value) {
  if (!lastState || !lastState.spa) return;
  lastState.spa[field] = value;
  // Re-render control panelu (nezavisle na refresh tick)
  try { renderControl(lastState); } catch(e) {}
  try { renderHeader(lastState); } catch(e) {}
  // Re-render hlavni stranky (tile vyrivka)
  if (activeTab === 'overview') {
    try { renderOverview(lastState); } catch(e) {}
  }
}

// v4.3.0 NEW: Toast notifikace - moderní neblokující alternativa k alert()
const _toastQueue = [];
let _toastContainer = null;
function _ensureToastContainer() {
  if (_toastContainer) return _toastContainer;
  _toastContainer = document.createElement('div');
  _toastContainer.id = 'toastContainer';
  _toastContainer.style.cssText = 'position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%); z-index: 10000; display: flex; flex-direction: column; gap: 8px; pointer-events: none; max-width: 90vw;';
  document.body.appendChild(_toastContainer);
  return _toastContainer;
}
function toast(message, type) {
  type = type || 'info';
  const c = _ensureToastContainer();
  const t = document.createElement('div');
  const colors = {
    info:    'background: var(--text); color: white;',
    success: 'background: var(--success); color: white;',
    error:   'background: var(--danger); color: white;',
    warning: 'background: var(--warning); color: white;',
  };
  const icons = { info: 'ℹ', success: '✓', error: '⚠', warning: '⚠' };
  t.style.cssText = 'pointer-events: auto; padding: 12px 18px; border-radius: 10px; font-family: var(--ui); font-size: 13px; font-weight: 500; box-shadow: 0 4px 12px rgba(15,23,42,0.18); display: flex; align-items: center; gap: 10px; min-width: 220px; max-width: 90vw; opacity: 0; transform: translateY(20px); transition: opacity 0.25s, transform 0.25s; ' + (colors[type] || colors.info);
  t.innerHTML = '<span style="font-size: 16px;">' + (icons[type] || icons.info) + '</span><span style="flex:1;">' + message + '</span><span style="cursor: pointer; opacity: 0.7; font-size: 16px; padding: 0 4px;">×</span>';
  t.querySelector('span:last-child').onclick = () => _dismissToast(t);
  c.appendChild(t);
  requestAnimationFrame(() => { t.style.opacity = '1'; t.style.transform = 'translateY(0)'; });
  const timeout = type === 'error' ? 6000 : 3500;
  setTimeout(() => _dismissToast(t), timeout);
}
function _dismissToast(t) {
  if (!t || !t.parentNode) return;
  t.style.opacity = '0';
  t.style.transform = 'translateY(20px)';
  setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
}

// v4.3.0 NEW: Loading + flash state na clicked tlacitku
function flashButton(evt) {
  const b = evt && evt.currentTarget;
  if (!b) return null;
  b.classList.add('flash');
  setTimeout(() => b.classList.remove('flash'), 400);
  b.classList.add('loading');
  return b;
}
function unflashButton(b) {
  if (b) b.classList.remove('loading');
}

async function setHeater(v, evt) {
  const b = flashButton(evt);
  optimisticSpaUpdate('heater', v);
  await apiPost('/api/spa/heater', {value: v});
  unflashButton(b);
}
async function setFilter(v, evt) {
  const b = flashButton(evt);
  optimisticSpaUpdate('filter', v);
  await apiPost('/api/spa/filter', {value: v});
  unflashButton(b);
}
async function setBubbles(v, evt) {
  const b = flashButton(evt);
  optimisticSpaUpdate('bubbles', v);
  await apiPost('/api/spa/bubbles', {value: v});
  unflashButton(b);
}
async function setJets(v, evt) {
  const b = flashButton(evt);
  optimisticSpaUpdate('jets', v);
  await apiPost('/api/spa/jets', {value: v});
  unflashButton(b);
}
async function changeTemp(delta, evt) {
  if (!lastState || lastState.spa.target_temp == null) return;
  const newTemp = lastState.spa.target_temp + delta;
  if (newTemp < 20 || newTemp > 40) return;
  const b = flashButton(evt);
  document.getElementById('tempVal').textContent = newTemp + ' °C';
  optimisticSpaUpdate('target_temp', newTemp);
  await apiPost('/api/spa/temp', {value: newTemp});
  unflashButton(b);
}
async function sceneHeatNow() {
  if (!confirm('Zapnout Ohřát hned? SolarGuard přestane řídit vířivku.')) return;
  await apiPost('/api/spa/scene/heat_now');
}
async function sceneSolarAuto() { await apiPost('/api/spa/scene/solar_auto'); }

// v3.7 NEW: Pre-shower funkce
async function preshowerStart(eta_minutes) {
  const targetTemp = lastState && lastState.spa.target_temp ? lastState.spa.target_temp : 38;
  // Volitelně načti predikci a varuj uživatele pokud je margin negativní
  try {
    const pred = await fetch('/api/heating-curve/predict', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target_temp: targetTemp})
    }).then(r => r.json());
    const margin = eta_minutes - (pred.minutes || 0);
    if (margin < 0) {
      const msg = `Pozor: predikce ohřevu je ${pred.minutes} min, do cíle máš jen ${eta_minutes} min.\nVoda nemusí dosáhnout ${targetTemp}°C. Pokračovat?`;
      if (!confirm(msg)) return;
    } else if (margin < 10) {
      const msg = `Předpověď ohřevu: ${pred.minutes} min, máš ${eta_minutes} min. Bude to těsné. Pokračovat?`;
      if (!confirm(msg)) return;
    }
  } catch (e) {
    console.warn('heating-curve predict failed', e);
  }

  await apiPost('/api/preshower/start', {
    target_temp: targetTemp,
    eta_minutes: eta_minutes,
  });
}

async function preshowerCustomTime() {
  const t = prompt('Kdy má být vířivka připravena? (formát HH:MM, např. 19:30)', '19:30');
  if (!t) return;
  if (!/^\d{1,2}:\d{2}$/.test(t)) {
    toast('Špatný formát. Použij HH:MM, např. 19:30', 'warning');
    return;
  }
  const targetTemp = lastState && lastState.spa.target_temp ? lastState.spa.target_temp : 38;
  await apiPost('/api/preshower/start', {
    target_temp: targetTemp,
    eta_iso: t,
  });
}

async function preshowerCancel() {
  if (!confirm('Zrušit přípravu vířivky? Topení se vypne, override skončí.')) return;
  await apiPost('/api/preshower/cancel');
}

// Načti heating curve predikci a zobraz jako hint pod tlačítky
async function updateHeatingPrediction() {
  if (!lastState || !lastState.spa.current_temp || !lastState.spa.target_temp) return;
  try {
    const data = await fetch('/api/heating-curve').then(r => r.json());
    if (!data.available) return;
    const pred = data.current_prediction;
    const model = data.model;
    const el = document.getElementById('preshowerHeatingPred');
    if (el && pred && pred.minutes > 0) {
      const conf = model.confidence === 'high' ? '✓' : model.confidence === 'medium' ? '~' : '?';
      el.textContent = `${conf} predikce: ${pred.minutes} min na ${lastState.spa.target_temp}°C (${model.n_samples} vzorků)`;
    } else if (el) {
      el.textContent = '';
    }
  } catch (e) { /* ignore */ }
}
async function sceneGentle() {
  if (!confirm('Přepnout na mírný režim 33°C? SolarGuard bude udržovat vodu na 33°C podle přebytku.')) return;
  await apiPost('/api/spa/scene/gentle');
}

// Sanitizer OFF - pokud bezi cleaning, zastav ho. Jinak vypni sanitizer.
async function setSanitizerOff() {
  if (!lastState) return;
  if (lastState.cleaning && lastState.cleaning.running) {
    if (!confirm('Zastavit běžící čisticí program?')) return;
    await apiPost('/api/spa/cleaning/stop');
  } else {
    await apiPost('/api/spa/sanitizer', {value: false});
  }
}

async function startCleaning(hours) {
  if (lastState && lastState.cleaning && lastState.cleaning.running) {
    if (!confirm(`Nahradit běžící program za ${hours}h? Nejprve ho zastavím.`)) return;
    await fetch('/api/spa/cleaning/stop', { method: 'POST' });
    await new Promise(r => setTimeout(r, 500));
  } else {
    if (!confirm(`Spustit ${hours}h čisticí program? Zapne sanitizer + filtraci, automaticky skončí po ${hours}h.`)) return;
  }
  await apiPost('/api/spa/cleaning/start', {hours: hours});
}

async function refresh() {
  try {
    const s = await fetch('/api/state').then(r => r.json());
    lastState = s;
    // NEW v3.3: pokud SW vratil stale data, ukaz banner
    if (s._stale) {
      updateOfflineBanner(true, false, s._stale_age_sec);
    } else {
      updateOfflineBanner(false, false);
    }
    renderHeader(s);
    renderControl(s);
    // v3.7: heating prediction (jen na control tabu)
    if (activeTab === 'control') {
      updateHeatingPrediction();
    }
    if (activeTab === 'overview') {
      renderPlan(s); renderOverview(s);
      const h = await fetch('/api/history').then(r => r.json());
      renderChart(h.ticks);
      // v4.0 NEW: insights
      try {
        const ins = await fetch('/api/insights').then(r => r.json());
        renderInsights(ins);
      } catch (e) { console.warn('insights load failed', e); }
    } else if (activeTab === 'appliances') {
      // v4.3.0 NEW: paralelne nactu eval + learning status
      const [a, _] = await Promise.all([
        fetch('/api/appliances').then(r => r.json()),
        fetchLearningStatus(),
      ]);
      renderAppliances(a);
    } else if (activeTab === 'heatpump') {
      const hp = await fetch('/api/heatpump').then(r => r.json()).catch(() => null);
      if (hp) renderHeatpump(hp);
    } else if (activeTab === 'flow') {
      renderFlow(s);
    } else if (activeTab === 'stats') {
      renderStats(s);
    } else if (activeTab === 'decisions') {
      const [d, engineStatus] = await Promise.all([
        fetch('/api/decisions?limit=200').then(r => r.json()),
        fetch('/api/engine/status').then(r => r.json()).catch(() => null),
      ]);
      renderDecisions(d.ticks);
      if (engineStatus) renderEngineStatus(engineStatus);
      // v4.3.2 NEW: snapshoty napeti clanku (samostatny fetch, v try aby nezablokoval zbytek)
      renderSnapshots();
    } else if (activeTab === 'events') {
      const e = await fetch('/api/events').then(r => r.json());
      renderEvents(e.events);
    } else if (activeTab === 'schedule') {
      const sched = await fetch('/api/schedule').then(r => r.json());
      renderSchedule(sched);
    } else if (activeTab === 'spot') {
      const sp = await fetch('/api/spot').then(r => r.json());
      renderSpot(sp);
    } else if (activeTab === 'digest') {
      // v4.1 NEW: digest tab
      try {
        const dig = await fetch('/api/digest/latest').then(r => r.json());
        renderDigest(dig);
      } catch (e) { console.warn('digest load failed', e); }
    } else if (activeTab === 'users') {
      // v4.1 NEW: users tab (owner only)
      loadUsers();
    } else if (activeTab === 'more') {
      // v3.5 NEW: "Více" rozcestnik - jen update cfgInfo (overview to udela)
      renderOverview(s);
    }
  } catch (e) {
    console.error(e);
    // NEW v3.3: hard offline (nic v cache) -> show banner
    updateOfflineBanner(true, false, null);
  }
}

function renderHeader(s) {
  const badge = document.getElementById('stateBadge');
  badge.textContent = (stateNames[s.state] || s.state).toUpperCase();
  badge.className = 'pill state-' + s.state;
  document.getElementById('dryRun').style.display = s.dry_run ? 'inline-flex' : 'none';
  document.getElementById('liveTag').style.display = !s.dry_run ? 'inline-flex' : 'none';
  document.getElementById('overrideTag').style.display = s.override_active ? 'inline-flex' : 'none';
  document.getElementById('cleaningTag').style.display = (s.cleaning && s.cleaning.running) ? 'inline-flex' : 'none';
  document.getElementById('gentleTag').style.display = (s.current_scene === 'gentle') ? 'inline-flex' : 'none';
  document.getElementById('batFullTag').style.display = (s.victron && s.victron.soc != null && s.victron.soc >= 90) ? 'inline-flex' : 'none';

  // v4.3.0 NEW: Connection indicator
  const connPill = document.getElementById('connectionPill');
  const connLabel = document.getElementById('connectionLabel');
  if (connPill && connLabel) {
    const victronStale = s.victron && s.victron.stale;
    const spaOnline = s.spa && s.spa.online;
    const envStale = s.env && s.env.stale;
    let cls = 'conn-ok', label = 'připojeno', title = 'MQTT, vířivka i meteostanice OK';

    if (victronStale && !spaOnline) {
      cls = 'conn-fail'; label = 'OFFLINE';
      title = 'Victron MQTT i vířivka offline';
    } else if (victronStale) {
      cls = 'conn-fail'; label = 'MQTT';
      title = 'Victron MQTT nedostupný (>120s) - bez dat o FVE/baterce';
    } else if (!spaOnline) {
      cls = 'conn-warn'; label = 'vířivka';
      title = 'Vířivka offline (5+ chyb) - ovládání nedostupné';
    } else if (envStale) {
      cls = 'conn-warn'; label = 'meteo';
      title = 'Loxone meteostanice nedostupná (>5min)';
    }
    connPill.className = 'pill conn-pill ' + cls;
    connLabel.textContent = label;
    connPill.title = title;
  }

  const userPill = document.getElementById('userPill');
  if (_currentUser && _currentUser.name && _currentUser.name !== 'anonymous') {
    userPill.style.display = 'inline-flex';
    userPill.textContent = '👤 ' + _currentUser.name;
    userPill.title = 'Role: ' + _currentUser.role + ' · klepni pro odhlášení';
  } else {
    userPill.style.display = 'none';
  }
}

function renderPlan(s) {
  const p = s.plan || {};
  const strat = p.strategy || 'unknown';
  const card = document.getElementById('planCard');
  card.className = 'plan-card ' + strat;
  const stratEl = document.getElementById('planStrategy');
  stratEl.textContent = strategyNames[strat] || strat.toUpperCase();
  stratEl.className = 'plan-strategy ' + strat;
  document.getElementById('planReason').textContent = p.reason || '—';
  const stats = document.getElementById('planStats');

  // v3.4: fallback - když Open-Meteo selže, ukaž aspoň VRM yield_today
  const pvForecast = s.forecast ? s.forecast.pv_today : null;
  const pvActual = s.victron ? s.victron.pv_yield_today_kwh : null;
  // Pokud máme VRM data, ukaž je s ✓ checkmarkem (= reálná měřená data)
  let pvTodayHtml;
  if (pvForecast != null) {
    pvTodayHtml = '<div class="plan-stat-label">FV dnes (předp.)</div><div class="plan-stat-value">' + fmt(pvForecast, '', 1) + '<span class="plan-stat-unit"> kWh</span></div>';
    if (pvActual != null) {
      pvTodayHtml += '<div style="font-size:9px;color:var(--text-dim);margin-top:2px;">vyrobeno: ' + fmt(pvActual, ' kWh', 1) + '</div>';
    }
  } else if (pvActual != null) {
    pvTodayHtml = '<div class="plan-stat-label">FV vyrobeno ✓</div><div class="plan-stat-value">' + fmt(pvActual, '', 1) + '<span class="plan-stat-unit"> kWh</span></div>';
  } else {
    pvTodayHtml = '<div class="plan-stat-label">FV dnes</div><div class="plan-stat-value">—<span class="plan-stat-unit"> kWh</span></div>';
  }

  stats.innerHTML =
    '<div>' + pvTodayHtml + '</div>' +
    '<div><div class="plan-stat-label">FV zbývá</div><div class="plan-stat-value">' + fmt(p.predicted_pv_kwh, '', 1) + '<span class="plan-stat-unit"> kWh</span></div></div>' +
    '<div><div class="plan-stat-label">Baterie dostupná</div><div class="plan-stat-value">' + fmt(p.battery_available_kwh, '', 1) + '<span class="plan-stat-unit"> kWh</span></div></div>' +
    '<div><div class="plan-stat-label">Spotřeba do večera</div><div class="plan-stat-value">' + fmt(p.baseline_consumption_kwh, '', 1) + '<span class="plan-stat-unit"> kWh</span></div></div>' +
    '<div><div class="plan-stat-label">Volná energie</div><div class="plan-stat-value">' + fmt(p.discretionary_kwh, '', 1) + '<span class="plan-stat-unit"> kWh</span></div></div>' +
    '<div><div class="plan-stat-label">Aktivní práh</div><div class="plan-stat-value">' + fmt(p.dynamic_surplus_on_w, '', 0) + '<span class="plan-stat-unit"> W</span></div></div>';
  // Age badge - jak staré je plánování
  const ageEl = document.getElementById('planAge');
  if (p.computed_at) {
    const age = (s.ts - p.computed_at);
    ageEl.textContent = 'aktualizováno ' + fmtAge(age);
  } else {
    ageEl.textContent = '';
  }
}

function renderOverview(s) {
  const heaterColor = s.spa.heater ? 'val-pos' : '';
  const surplusColor = (s.victron.surplus != null && s.victron.surplus > 0) ? 'val-pos' : 'val-neg';
  const p = s.plan || {};
  const activeThr = p.dynamic_surplus_on_w != null ? p.dynamic_surplus_on_w : s.config.surplus_on;
  // v3.6 NEW: Spot price tile (jen pokud máme data)
  const sp = s.spot || {};
  let spotTile = '';
  if (sp.current_price_kc != null) {
    let spotColor = 'val-pos';
    let spotIcon = '✓';
    if (sp.current_price_kc > 4) { spotColor = 'val-neg'; spotIcon = '⚠'; }
    else if (sp.current_price_kc > 2.5) { spotColor = ''; spotIcon = '·'; }
    spotTile =
      '<div class="tile" style="--accent: var(--success);"><div class="tile-label">Spot teď</div>' +
      '<div class="tile-value ' + spotColor + '">' + sp.current_price_kc.toFixed(2) + '<span class="tile-unit">Kč/kWh</span></div>' +
      '<div class="tile-sub">' + spotIcon + ' ' + (sp.best_hours_today && sp.best_hours_today.length ? 'next: ' + String(sp.best_hours_today[0]).padStart(2,'0') + ':00' : 'OTE-CR') + '</div></div>';
  }
  // v4.3.0 NEW: Tile Dum s L1/L2/L3 mini-bary
  const v = s.victron;
  let phasesMiniHtml = '';
  if (v.load_l1 != null && v.load_l2 != null && v.load_l3 != null) {
    const phaseMax = 3500; // Multiplus II 5000 limit
    const spaPhase = 'L2';
    const phases = [
      { label: 'L1', val: v.load_l1 },
      { label: 'L2', val: v.load_l2 },
      { label: 'L3', val: v.load_l3 },
    ];
    phasesMiniHtml = '<div class="phase-mini-row">' + phases.map(p => {
      const pct = Math.min(100, (p.val / phaseMax) * 100);
      const isOver = p.val > phaseMax;
      const isHot = p.val > phaseMax * 0.7;
      const isSpa = p.label === spaPhase;
      const barColor = isOver ? 'var(--danger)' : (isHot ? 'var(--warning)' : (isSpa ? 'var(--purple)' : 'var(--text-muted)'));
      const labelStyle = isSpa ? 'color: var(--purple); font-weight: 800;' : '';
      return '<div class="phase-mini">'
        + '<div class="phase-mini-label" style="' + labelStyle + '">' + p.label + (isSpa ? '♨' : '') + '</div>'
        + '<div class="phase-mini-bar"><div class="phase-mini-fill" style="width:' + pct + '%; background:' + barColor + ';"></div></div>'
        + '<div class="phase-mini-val">' + p.val.toFixed(0) + 'W</div>'
        + '</div>';
    }).join('') + '</div>';
  }

  document.getElementById('tiles').innerHTML =
    '<div class="tile battery"><div class="tile-label">SOC</div><div class="tile-value">' + fmt(s.victron.soc, '', 0) + '<span class="tile-unit">%</span></div></div>' +
    '<div class="tile solar"><div class="tile-label">FV</div><div class="tile-value val-solar">' + fmt(s.victron.pv, '', 0) + '<span class="tile-unit">W</span></div></div>' +
    '<div class="tile surplus"><div class="tile-label">Přebytek</div><div class="tile-value ' + surplusColor + '">' + fmt(s.victron.surplus, '', 0) + '<span class="tile-unit">W</span></div><div class="tile-sub">práh ' + fmt(activeThr, ' W', 0) + '</div></div>' +
    '<div class="tile home tile-home-detail"><div class="tile-label">Dům</div><div class="tile-value">' + fmt(s.victron.load, '', 0) + '<span class="tile-unit">W</span></div>' + phasesMiniHtml + '</div>' +
    '<div class="tile water"><div class="tile-label">Voda</div><div class="tile-value val-water">' + fmt(s.spa.current_temp, '', 0) + '<span class="tile-unit">°C</span></div><div class="tile-sub">cíl ' + fmt(s.spa.target_temp, ' °C', 0) + '</div></div>' +
    '<div class="tile spa"><div class="tile-label">Topení</div><div class="tile-value ' + heaterColor + '">' + (s.spa.heater == null ? '—' : (s.spa.heater ? 'ON' : 'off')) + '</div><div class="tile-sub">filtr ' + (s.spa.filter == null ? '—' : (s.spa.filter ? 'on' : 'off')) + ' · UVC ' + (s.spa.sanitizer == null ? '—' : (s.spa.sanitizer ? 'on' : 'off')) + '</div></div>' +
    spotTile;

  const e = s.env || {};
  let sky = '—';
  if (e.is_sunny) sky = '☀ slunečno';
  else if (e.is_overcast) sky = '☁ zataženo';
  else if (e.light_lux != null && e.light_lux > 0) sky = '⛅ polojasno';
  else if (e.light_lux != null) sky = '🌙 tma';
  const rainText = e.is_raining === true ? '🌧 prší' : (e.is_raining === false ? 'sucho' : '—');
  const airColor = (e.air_temp != null && e.air_temp < 5) ? 'val-primary' : '';
  document.getElementById('envTiles').innerHTML =
    '<div class="tile"><div class="tile-label">Teplota</div><div class="tile-value ' + airColor + '">' + fmt(e.air_temp, '', 1) + '<span class="tile-unit">°C</span></div></div>' +
    '<div class="tile"><div class="tile-label">Jas</div><div class="tile-value val-solar">' + fmt(e.light_lux, '', 0) + '<span class="tile-unit">Lx</span></div><div class="tile-sub">' + sky + '</div></div>' +
    '<div class="tile"><div class="tile-label">Vítr</div><div class="tile-value">' + fmt(e.wind_kmh, '', 1) + '<span class="tile-unit">km/h</span></div></div>' +
    '<div class="tile"><div class="tile-label">Srážky</div><div class="tile-value" style="font-size: 18px;">' + rainText + '</div></div>';

  const stratLabel = strategyNames[p.strategy] || '?';
  const cfgHtml =
    '<div class="info-row"><span class="info-label">Aktivní práh zap</span><span class="info-value" style="color: var(--primary)">' + fmt(p.dynamic_surplus_on_w, ' W', 0) + ' <span class="info-note">(' + stratLabel + ')</span></span></div>' +
    '<div class="info-row"><span class="info-label">Aktivní práh vyp</span><span class="info-value" style="color: var(--primary)">' + fmt(p.dynamic_surplus_off_w, ' W', 0) + '</span></div>' +
    '<div class="info-row"><span class="info-label">Fallback (config)</span><span class="info-value" style="color: var(--text-muted)">' + s.config.surplus_on + ' / ' + s.config.surplus_off + ' W</span></div>' +
    '<div class="info-row"><span class="info-label">Min SOC (hard)</span><span class="info-value">' + s.config.min_soc + ' %</span></div>' +
    '<div class="info-row"><span class="info-label">Cílová teplota (config)</span><span class="info-value">' + s.config.target_temp + ' °C</span></div>';
  const c = document.getElementById('cfgInfo');
  if (c) c.innerHTML = cfgHtml;
  // v3.5 NEW: stejny config i v mobile "Více" tabu
  const cm = document.getElementById('cfgInfoMobile');
  if (cm) cm.innerHTML = cfgHtml;
  // v3.8 NEW: InfluxDB status (jen pokud jsme na "more" tabu)
  if (activeTab === 'more') {
    renderInfluxStatus();
  }
}

// v3.8 NEW: Render InfluxDB status badge v Více tabu
async function renderInfluxStatus() {
  const card = document.getElementById('influxStatusCard');
  if (!card) return;
  try {
    const stats = await fetch('/api/influx/stats').then(r => r.json());
    if (!stats.configured) {
      card.innerHTML =
        '<div class="info-row"><span class="info-label">Stav</span>' +
        '<span class="info-value" style="color: var(--text-muted)">⚪ nenakonfigurováno</span></div>' +
        '<div style="padding: 12px 16px; font-family: var(--mono); font-size: 10px; color: var(--text-muted); line-height: 1.5;">' +
        'InfluxDB není zapnutá. Pro dlouhodobé grafy přes Grafanu zapni v <code>config.yaml</code> sekci <code>influxdb:</code>. Návod: <code>DEPLOY_v3.8.md</code></div>';
      return;
    }
    const available = stats.available;
    const badge = available
      ? '<span style="color: var(--success); font-weight: 700;">● PŘIPOJENO</span>'
      : '<span style="color: var(--warning); font-weight: 700;">● ODPOJENO (retry každou minutu)</span>';
    card.innerHTML =
      '<div class="info-row"><span class="info-label">Stav</span><span class="info-value">' + badge + '</span></div>' +
      '<div class="info-row"><span class="info-label">URL</span><span class="info-value" style="font-size: 10px;">' + (stats.url || '—') + '</span></div>' +
      '<div class="info-row"><span class="info-label">Zapsáno bodů</span><span class="info-value">' + (stats.total_written || 0).toLocaleString() + '</span></div>' +
      '<div class="info-row"><span class="info-label">Buffer</span><span class="info-value">' + (stats.buffer_size || 0) + ' / 1000</span></div>' +
      (stats.total_dropped ? '<div class="info-row"><span class="info-label">Zahozeno</span><span class="info-value" style="color: var(--warning)">' + stats.total_dropped + '</span></div>' : '') +
      (stats.consecutive_failures ? '<div class="info-row"><span class="info-label">Pokusy o reconnect</span><span class="info-value" style="color: var(--warning)">' + stats.consecutive_failures + '</span></div>' : '');
  } catch (e) {
    card.innerHTML = '<div style="padding: 12px 16px; color: var(--text-muted); font-family: var(--mono); font-size: 11px;">Načítání selhalo: ' + e.message + '</div>';
  }
}

// v4.3.0 NEW: cache aktivnich cyklu pro UI render (refreshovano kazdy refresh)
let _activeCycles = {};       // appliance_id -> active cycle data
let _learnedProfiles = {};    // appliance_id -> learned profile

async function fetchLearningStatus() {
  try {
    const r = await fetch('/api/learning/status');
    if (!r.ok) return;
    const data = await r.json();
    _activeCycles = {};
    (data.active_cycles || []).forEach(c => { _activeCycles[c.appliance_id] = c; });
    _learnedProfiles = data.profiles || {};
  } catch (e) { /* silent */ }
}

function renderAppliances(a) {
  document.getElementById('appSurplus').textContent = fmt(a.surplus_w, ' W', 0);
  document.getElementById('appSOC').textContent = fmt(a.soc_pct, ' %', 0);
  document.getElementById('appStrat').textContent = strategyNames[a.strategy] || a.strategy;
  const grid = document.getElementById('appGrid');
  if (!a.appliances || !a.appliances.length) {
    grid.innerHTML = '<div style="padding:20px;color:var(--text-muted);">Žádné spotřebiče definovány.</div>';
    return;
  }
  // v4.3.0: serad podle priority - JEĎ první, pak OPATRNĚ, POČKEJ na konci.
  // Aktivni cykly maji zvlastni prioritu -> nahoru
  const order = { green: 0, amber: 1, red: 2 };
  const sorted = [...a.appliances].sort((x, y) => {
    const xActive = _activeCycles[x.id] ? -1 : 0;
    const yActive = _activeCycles[y.id] ? -1 : 0;
    if (xActive !== yActive) return xActive - yActive;
    return (order[x.status] ?? 9) - (order[y.status] ?? 9);
  });

  // v4.3.0 NEW: kompaktni layout. Obsah kazde karty:
  //  HEAD: emoji + nazev + kWh + verdict + tlacitko START/STOP
  //  MSG: jednoradkova zprava (proc) NEBO progress aktivniho cyklu
  //  ENERGY: pillsy odkud energie pojde
  //  PROFIL: kolik už je naučených cyklů (pokud nějaký)
  grid.innerHTML = sorted.map(ap => {
    const active = _activeCycles[ap.id];
    const profile = _learnedProfiles[ap.id];

    // Energy source pill
    let sourcePill = '';
    if (ap.energy_source === 'solar') sourcePill = '<span class="ef-pill ef-solar">☀ z FVE</span>';
    else if (ap.energy_source === 'solar+battery') sourcePill = '<span class="ef-pill ef-solar">☀</span><span class="ef-pill ef-bat">🔋</span>';
    else if (ap.energy_source === 'battery') sourcePill = '<span class="ef-pill ef-bat">🔋 baterka</span>';
    else if (ap.energy_source === 'grid') sourcePill = '<span class="ef-pill ef-grid">⚡ síť</span>';

    // Track button - "PUSTIL JSEM" / "STOP" + aktivni cyklus stav
    let trackButton, msgArea, profileBadge = '';
    if (active) {
      // Aktivni cyklus
      const elapsedMin = Math.floor(active.elapsed_sec / 60);
      const phaseTxt = active.detected_phase ? ('L' + active.detected_phase) : 'detekuji…';
      msgArea = `<div class="app-msg learning-active">⏱ ${elapsedMin} min · ${phaseTxt} · ${active.current_phase_w.toFixed(0)} W</div>`;
      trackButton = `<button class="app-track-btn stop" onclick="stopLearning('${ap.id}', event)">■ STOP</button>`;
    } else {
      msgArea = `<div class="app-msg">${ap.message}</div>`;
      trackButton = `<button class="app-track-btn" onclick="startLearning('${ap.id}', event)" title="Klikni když pustíš spotřebič - SolarGuard se naučí jeho profil">▶ PUSTIL JSEM</button>`;
    }
    if (profile && profile.sample_count >= 1) {
      profileBadge = `<span class="app-profile-badge" title="Naučeno z ${profile.sample_count} cyklů">📊 ${profile.sample_count}×</span>`;
    }

    return `
    <div class="app-card ${ap.status}${active ? ' is-tracking' : ''}">
      <div class="app-card-head">
        <span class="app-emoji">${ap.emoji}</span>
        <div class="app-name-block">
          <div class="app-name">${ap.name}${profileBadge}</div>
          <div class="app-name-meta">${ap.cycle_kwh.toFixed(1)} kWh · ${ap.cycle_min}min</div>
        </div>
        <span class="app-verdict ${ap.status}">${verdictText[ap.status] || ap.status}</span>
      </div>
      ${msgArea}
      <div class="app-energy-flow">${sourcePill}</div>
      <div class="app-track-row">${trackButton}</div>
    </div>`;
  }).join('');
}

async function startLearning(applianceId, evt) {
  const btn = flashButton(evt);
  try {
    const r = await fetch('/api/learning/start/' + applianceId, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
    });
    if (!r.ok) {
      const err = await r.text();
      toast('Chyba: ' + err, 'error');
      unflashButton(btn);
      return;
    }
    toast('Sleduji cyklus - klikni STOP až dokončí', 'success');
    await fetchLearningStatus();
    refresh();  // okamzite renderni
  } catch (e) {
    toast('Chyba sítě: ' + e.message, 'error');
  }
  unflashButton(btn);
}

async function stopLearning(applianceId, evt) {
  if (!confirm('Ukončit sledování cyklu? Profil se aktualizuje.')) return;
  const btn = flashButton(evt);
  try {
    const r = await fetch('/api/learning/stop/' + applianceId, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
    });
    if (!r.ok) {
      const err = await r.text();
      toast('Chyba: ' + err, 'error');
      unflashButton(btn);
      return;
    }
    const data = await r.json();
    const res = data.result;
    let msg = `Cyklus dokončen: ${res.duration_min}min · ${res.peak_w}W peak · ${res.kwh}kWh`;
    if (res.detected_phase) msg += ` · L${res.detected_phase}`;
    if (res.interrupted) msg += ' ⚠ rušeno jiným spotřebičem';
    toast(msg, res.interrupted ? 'warning' : 'success');
    await fetchLearningStatus();
    refresh();
  } catch (e) {
    toast('Chyba sítě: ' + e.message, 'error');
  }
  unflashButton(btn);
}

// v4.3.0 NEW: Heat Pump UI
const hpEngineStateNames = {
  disabled: 'vypnuto',
  idle: 'v klidu',
  solar_boost: '☀ solar boost',
  cooling: '❄ chlazení',
  night_saving: '🌙 noční úspora',
  survive: '🛡 šetřím',
  manual: '✋ manuální',
  alarm: '⚠ alarm',
};

const hpModeNames = {
  off: 'vypnuto', heat: 'topí', cool: 'chladí', hot_water: 'TUV',
};

function renderHeatpump(hp) {
  const disabled = document.getElementById('hpDisabled');
  const content = document.getElementById('hpContent');
  if (!hp.enabled) {
    disabled.style.display = 'block';
    content.style.display = 'none';
    return;
  }
  disabled.style.display = 'none';
  content.style.display = 'block';

  // Engine state v hlavicce
  const eng = document.getElementById('hpEngineState');
  if (eng) eng.textContent = hpEngineStateNames[hp.engine_state] || hp.engine_state;

  // Stav grid - 6 dlazdic
  const formatTemp = (t) => t == null ? '—' : t.toFixed(1) + '°C';
  document.getElementById('hpStateGrid').innerHTML =
    '<div class="tile water"><div class="tile-label">Venku</div><div class="tile-value">' + formatTemp(hp.outdoor_temp) + '</div></div>' +
    '<div class="tile spa"><div class="tile-label">Pokoj</div><div class="tile-value">' + formatTemp(hp.indoor_temp) + '</div><div class="tile-sub">cíl ' + formatTemp(hp.target_room_temp) + '</div></div>' +
    '<div class="tile solar"><div class="tile-label">TUV</div><div class="tile-value">' + formatTemp(hp.hot_water_temp) + '</div><div class="tile-sub">cíl ' + formatTemp(hp.target_hot_water) + '</div></div>' +
    '<div class="tile surplus"><div class="tile-label">Výstup vody</div><div class="tile-value">' + formatTemp(hp.supply_temp) + '</div><div class="tile-sub">zpátečka ' + formatTemp(hp.return_temp) + '</div></div>' +
    '<div class="tile home"><div class="tile-label">Spotřeba</div><div class="tile-value">' + (hp.power_w == null ? '—' : hp.power_w.toFixed(0) + 'W') + '</div><div class="tile-sub">dnes ' + (hp.energy_today_kwh == null ? '—' : hp.energy_today_kwh.toFixed(1) + ' kWh') + '</div></div>' +
    '<div class="tile battery"><div class="tile-label">Režim</div><div class="tile-value" style="font-size: 18px;">' + (hpModeNames[hp.operating_mode] || hp.operating_mode || '—') + '</div><div class="tile-sub">kompresor ' + (hp.compressor_running == null ? '—' : (hp.compressor_running ? 'BĚŽÍ' : 'off')) + '</div></div>';

  // Manual control values
  const setText = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  setText('hpHwTarget', formatTemp(hp.target_hot_water));
  setText('hpHwActual', formatTemp(hp.hot_water_temp));
  setText('hpRoomTarget', formatTemp(hp.target_room_temp));
  setText('hpRoomActual', formatTemp(hp.indoor_temp));
  setText('hpHwVal', hp.target_hot_water == null ? '—' : hp.target_hot_water + ' °C');
  setText('hpRoomVal', hp.target_room_temp == null ? '—' : hp.target_room_temp + ' °C');

  // Aux heater status
  let auxText = '—';
  if (hp.add_heater_blocked === true) auxText = 'BLOKOVÁN';
  else if (hp.add_heater_active === true) auxText = 'AKTIVNÍ';
  else if (hp.add_heater_active === false) auxText = 'neaktivní';
  setText('hpAuxStatus', auxText);

  // Active scene button highlight
  const setActive = (id, active) => { const el = document.getElementById(id); if (el) el.classList.toggle('active', active); };
  setActive('hpSceneAutoBtn', !hp.manual_override);
  setActive('hpSceneBoostBtn', hp.manual_override && hp.manual_override_reason.includes('solar_boost'));
  setActive('hpSceneCoolBtn', hp.manual_override && hp.manual_override_reason.includes('cooling'));
  setActive('hpSceneHeatBtn', hp.manual_override && hp.manual_override_reason.includes('heating'));

  // Info card - detail
  const card = document.getElementById('hpInfoCard');
  if (card) {
    const rows = [];
    rows.push(['Online', hp.online ? 'ano' : 'NE']);
    rows.push(['Engine stav', hpEngineStateNames[hp.engine_state] || hp.engine_state]);
    if (hp.cop != null) rows.push(['COP odhad', hp.cop.toFixed(2)]);
    if (hp.alarm_active) rows.push(['⚠ Alarm', hp.alarm_code || 'aktivní']);
    if (hp.manual_override) rows.push(['Override', hp.manual_override_reason || 'aktivní']);
    rows.push(['Dohřev blokován', hp.add_heater_blocked == null ? '—' : (hp.add_heater_blocked ? 'ANO' : 'ne')]);
    card.innerHTML = rows.map(([k, v]) =>
      '<div class="info-row"><span class="info-label">' + k + '</span><span class="info-value">' + v + '</span></div>'
    ).join('');
  }
}

// Heat pump action handlers
async function hpChangeHw(delta, evt) {
  if (!lastState) return;
  // Read current value from the input or default 50
  const valEl = document.getElementById('hpHwVal');
  const cur = parseFloat(valEl.textContent) || 50;
  const next = Math.max(30, Math.min(65, cur + delta));
  const b = flashButton(evt);
  valEl.textContent = next + ' °C';
  await apiPost('/api/heatpump/hot_water_temp', { value: next });
  unflashButton(b);
}
async function hpChangeRoom(delta, evt) {
  const valEl = document.getElementById('hpRoomVal');
  const cur = parseFloat(valEl.textContent) || 21;
  const next = Math.max(16, Math.min(26, cur + delta));
  const b = flashButton(evt);
  valEl.textContent = next.toFixed(1) + ' °C';
  await apiPost('/api/heatpump/room_temp', { value: next });
  unflashButton(b);
}
async function hpBlockAux(blocked, evt) {
  const b = flashButton(evt);
  await apiPost('/api/heatpump/block_heater', { value: blocked });
  unflashButton(b);
}
async function hpSceneAuto() {
  await apiPost('/api/heatpump/scene/auto');
  toast('Čerpadlo: auto režim', 'success');
}
async function hpSceneBoost() {
  if (!confirm('Zapnout solar boost? Zvýší TUV a topení, blokuje el. dohřev.')) return;
  await apiPost('/api/heatpump/scene/solar_boost');
  toast('Čerpadlo: solar boost ON', 'success');
}
async function hpSceneHeat() {
  await apiPost('/api/heatpump/scene/heating');
  toast('Čerpadlo: topení', 'success');
}
async function hpSceneCool() {
  if (!confirm('Přepnout do režimu chlazení?')) return;
  await apiPost('/api/heatpump/scene/cooling');
  toast('Čerpadlo: chlazení ON', 'success');
}

function renderFlow(s) {
  const v = s.victron || {};
  const pv = v.pv || 0;
  const bat = v.battery_power || 0;
  const home = v.load || 0;
  const grid = v.grid || 0;
  const surplus = v.surplus || 0;
  const heaterW = (s.spa && s.spa.heater) ? 2200 : 0;
  // v4.3.0 NEW: TC actual power (z H66 dat - pokud je modul aktivni)
  const hpW = (s.heatpump && s.heatpump.power_w) ? s.heatpump.power_w : 0;
  const hpActive = hpW > 50;

  // ============= HERO KARTA =============
  document.getElementById('flowHeroPv').textContent = pv.toFixed(0) + ' W';
  const surplusEl = document.getElementById('flowHeroSurplus');
  if (surplus > 0) {
    surplusEl.textContent = '+' + surplus.toFixed(0) + ' W';
    surplusEl.className = 'flow-hero-value pos';
  } else {
    surplusEl.textContent = surplus.toFixed(0) + ' W';
    surplusEl.className = 'flow-hero-value neg';
  }

  // Stack-bar: kam jde aktualni FVE produkce.
  // Pri PV > 0: rozdel mezi: dum (z FVE), baterie (do baterky), virivka, TC, sit (prodej).
  // Pri PV = 0: bar prazdny.
  const stackBar = document.getElementById('flowStackBar');
  if (pv < 50) {
    stackBar.innerHTML = '<div class="flow-stack-empty">FVE neprodukuje</div>';
  } else {
    // Logika: kolik z FVE produkce jde kam
    // home_from_pv = min(home, pv) - kolik z PV pokrývá dům
    // bat_charge = max(0, bat) - kolik jde do baterky
    // grid_export = max(0, -grid) - kolik prodáváme
    const home_from_pv = Math.min(home, pv);
    const bat_charge = Math.max(0, bat);
    const grid_export = Math.max(0, -grid);
    // virivka a TC jsou součástí home, takže je extrahujeme
    const home_general = Math.max(0, home_from_pv - heaterW - hpW);
    const home_spa = Math.min(home_from_pv, heaterW);
    const home_hp = Math.min(home_from_pv - home_spa, hpW);

    const total = home_general + home_spa + home_hp + bat_charge + grid_export;
    if (total < 50) {
      stackBar.innerHTML = '<div class="flow-stack-empty">FVE neprodukuje</div>';
    } else {
      const segs = [];
      const pushSeg = (val, label, cls) => {
        if (val < total * 0.02) return;  // <2% nezobrazujeme
        const pct = (val / total) * 100;
        segs.push('<div class="flow-stack-seg ' + cls + '" style="width: ' + pct.toFixed(1) + '%">' +
                  (pct > 12 ? label : '') + '</div>');
      };
      pushSeg(home_general, 'DŮM ' + Math.round((home_general/total)*100) + '%', 'home');
      pushSeg(bat_charge, '🔋 ' + Math.round((bat_charge/total)*100) + '%', 'bat');
      pushSeg(home_spa, '♨ ' + Math.round((home_spa/total)*100) + '%', 'spa');
      pushSeg(home_hp, '🔥 ' + Math.round((home_hp/total)*100) + '%', 'hp');
      pushSeg(grid_export, '↑ ' + Math.round((grid_export/total)*100) + '%', 'grid');
      stackBar.innerHTML = segs.join('');
    }
  }

  // ============= MINI KARTY: Baterie + Sit =============
  document.getElementById('flowBatSoc').textContent = (v.soc != null ? v.soc.toFixed(0) : '—') + '%';
  const batStatusEl = document.getElementById('flowBatStatus');
  if (bat > 50) {
    batStatusEl.textContent = '↓ nabíjí ' + bat.toFixed(0) + ' W';
    batStatusEl.className = 'flow-mini-sub charging';
  } else if (bat < -50) {
    batStatusEl.textContent = '↑ vybíjí ' + Math.abs(bat).toFixed(0) + ' W';
    batStatusEl.className = 'flow-mini-sub discharging';
  } else {
    batStatusEl.textContent = 'v klidu';
    batStatusEl.className = 'flow-mini-sub';
  }

  document.getElementById('flowGridValue').textContent = Math.abs(grid).toFixed(0) + ' W';
  const gridStatusEl = document.getElementById('flowGridStatus');
  if (grid > 50) {
    gridStatusEl.textContent = '↓ nákup ze sítě';
    gridStatusEl.className = 'flow-mini-sub in';
  } else if (grid < -50) {
    gridStatusEl.textContent = '↑ prodej do sítě';
    gridStatusEl.className = 'flow-mini-sub out';
  } else {
    gridStatusEl.textContent = 'neaktivní';
    gridStatusEl.className = 'flow-mini-sub';
  }

  // ============= AKTIVNÍ SPOTŘEBIČE =============
  // Sestavíme list: vždy Dům (zbytek po vyčlenění specifických), pak Vířivka pokud aktivní, pak TČ
  const consumers = [];
  const home_other = Math.max(0, home - heaterW - hpW);
  consumers.push({
    emoji: '🏠', name: 'Dům',
    sub: 'L1+L2+L3',
    watt: home_other,
    active: home_other > 50,
    sourceFromPv: pv > 0 ? Math.min(home_other, Math.max(0, pv - heaterW - hpW)) : 0,
  });
  if (s.spa && s.spa.heater) {
    consumers.push({
      emoji: '♨', name: 'Vířivka',
      sub: 'topí · L2',
      watt: heaterW,
      active: true,
      sourceFromPv: Math.min(heaterW, pv),
    });
  } else if (s.spa) {
    consumers.push({
      emoji: '♨', name: 'Vířivka',
      sub: 'stand-by',
      watt: 0, active: false,
      sourceFromPv: 0,
    });
  }
  if (s.heatpump && s.heatpump.enabled !== false) {
    if (hpActive) {
      const mode = s.heatpump.operating_mode || 'běží';
      consumers.push({
        emoji: '🔥', name: 'Čerpadlo',
        sub: mode,
        watt: hpW,
        active: true,
        sourceFromPv: Math.min(hpW, Math.max(0, pv - heaterW)),
      });
    } else if (s.heatpump.online) {
      consumers.push({
        emoji: '🔥', name: 'Čerpadlo',
        sub: 'stand-by',
        watt: 0, active: false,
        sourceFromPv: 0,
      });
    }
  }
  document.getElementById('flowConsumersList').innerHTML = consumers.map(c => {
    let sourceTxt, sourceCls;
    if (!c.active) {
      sourceTxt = '—';
      sourceCls = 'idle';
    } else if (pv >= c.watt) {
      const pct = Math.min(100, (c.sourceFromPv / c.watt) * 100);
      sourceTxt = pct.toFixed(0) + '% z FVE';
      sourceCls = 'solar';
    } else if (bat < -50) {
      sourceTxt = 'z baterky';
      sourceCls = 'bat';
    } else if (grid > 50) {
      sourceTxt = 'ze sítě';
      sourceCls = 'grid';
    } else {
      sourceTxt = 'kombinace';
      sourceCls = 'solar';
    }
    return `
    <div class="flow-consumer-row">
      <div class="flow-consumer-left">
        <span class="flow-consumer-emoji">${c.emoji}</span>
        <div>
          <div class="flow-consumer-name">${c.name}</div>
          <div class="flow-consumer-name-sub">${c.sub}</div>
        </div>
      </div>
      <div class="flow-consumer-right">
        <div class="flow-consumer-watt">${c.active ? c.watt.toFixed(0) + ' W' : '—'}</div>
        <div class="flow-consumer-source ${sourceCls}">${sourceTxt}</div>
      </div>
    </div>`;
  }).join('');

  // ============= PER-FÁZE =============
  if (v.load_l1 != null) {
    const phaseMax = 3500;
    const phases = [
      { label: 'L1', load: v.load_l1 || 0, isSpa: false },
      { label: 'L2', load: v.load_l2 || 0, isSpa: true },
      { label: 'L3', load: v.load_l3 || 0, isSpa: false },
    ];
    document.getElementById('flowPhasesList').innerHTML = phases.map(p => {
      const pct = Math.min(100, (p.load / phaseMax) * 100);
      const overload = p.load > phaseMax;
      const warn = p.load > phaseMax * 0.8;
      const barColor = overload ? 'var(--danger)' : (warn ? 'var(--warning)' : (p.isSpa ? 'var(--purple)' : 'var(--success)'));
      const wattCls = overload ? ' danger' : (warn ? ' warn' : '');
      const labelCls = p.isSpa ? ' l2' : '';
      return `
      <div class="flow-phase-row">
        <span class="flow-phase-label${labelCls}">${p.label}${p.isSpa ? ' ♨' : ''}</span>
        <div class="flow-phase-bar-wrap">
          <div class="flow-phase-bar-fill" style="width: ${pct}%; background: ${barColor};"></div>
        </div>
        <span class="flow-phase-watt${wattCls}">${p.load.toFixed(0)} W</span>
      </div>`;
    }).join('');
  } else {
    document.getElementById('flowPhasesList').innerHTML =
      '<div style="padding: 8px 0; color: var(--text-muted); font-family: var(--mono); font-size: 11px;">Per-fáze data nejsou dostupná</div>';
  }

  // ============= SVG DIAGRAM (collapsable - pokud je vidět, updatuj) =============
  const T = 50;
  setActive('node-solar', pv > T);
  setActive('node-battery', Math.abs(bat) > T);
  setActive('node-home', home > T);
  setActive('node-grid', Math.abs(grid) > T);
  setActive('node-spa', heaterW > 0);
  setActive('node-hp', hpActive);
  setLineActive('solar-battery', bat > T && pv > T);
  setLineActive('solar-home', pv > T && home > T);
  setLineActive('solar-grid', grid < -T);
  setLineActive('battery-home', bat < -T);
  setLineActive('grid-home', grid > T);
  setLineActive('home-spa', heaterW > 0);
  setLineActive('home-hp', hpActive);
  document.getElementById('flow-pv').textContent = fmt(pv, ' W', 0);
  document.getElementById('flow-pv-sub').textContent = 'výroba';
  document.getElementById('flow-bat').textContent = fmt(v.soc, ' %', 0);
  const batPower = Math.abs(bat);
  let batText = 'v klidu';
  if (bat > T) batText = 'nabíjí +' + batPower.toFixed(0) + ' W';
  else if (bat < -T) batText = 'vybíjí −' + batPower.toFixed(0) + ' W';
  document.getElementById('flow-bat-sub').textContent = batText;
  document.getElementById('flow-home').textContent = fmt(home, ' W', 0);
  document.getElementById('flow-home-sub').textContent = 'spotřeba';
  const gridAbs = Math.abs(grid);
  let gridText = 'neaktivní';
  if (grid > T) gridText = 'nákup ' + gridAbs.toFixed(0) + ' W';
  else if (grid < -T) gridText = 'prodej ' + gridAbs.toFixed(0) + ' W';
  document.getElementById('flow-grid').textContent = fmt(gridAbs, ' W', 0);
  document.getElementById('flow-grid-sub').textContent = gridText;
  document.getElementById('flow-spa').textContent = heaterW > 0 ? '~' + heaterW + ' W' : 'off';
  const flowHpEl = document.getElementById('flow-hp');
  if (flowHpEl) flowHpEl.textContent = hpActive ? Math.round(hpW) + ' W' : 'off';

  document.getElementById('flowUpdateTime').textContent = 'live · ' + new Date().toLocaleTimeString('cs-CZ');
}

function setActive(nodeId, active) {
  const rect = document.querySelector('#' + nodeId + ' rect');
  if (!rect) return;
  if (active) rect.classList.add('active'); else rect.classList.remove('active');
}
function setLineActive(name, active) {
  const line = document.getElementById('line-' + name);
  const dot = document.getElementById('dot-' + name);
  if (line) {
    line.classList.toggle('active', active);
    const src = name.split('-')[0];
    line.classList.remove('solar', 'battery', 'home', 'grid', 'spa');
    if (active) line.classList.add(src);
  }
  if (dot) dot.classList.toggle('active', active);
}

function renderStats(s) {
  const v = s.victron || {};
  const en = s.energy || {};
  const pvToday = v.pv_yield_today_kwh;
  const consToday = v.consumption_today_kwh;
  const batToday = v.battery_in_today_kwh;
  const pvForecast = s.forecast ? s.forecast.pv_today : null;
  const todayGrid = document.getElementById('statsTodayGrid');
  const selfSuff = (pvToday != null && consToday != null && consToday > 0) ? Math.min(100, (pvToday / consToday) * 100) : null;
  const pvForecastPct = (pvToday != null && pvForecast) ? Math.min(100, (pvToday / pvForecast) * 100) : 0;
  const pvDisplay = pvToday != null ? pvToday : en.pv_produced_kwh;
  const consDisplay = consToday != null ? consToday : en.home_consumed_kwh;
  const batDisplay = (batToday != null && batToday > 0) ? batToday : en.battery_charged_kwh;
  const pvBadge = pvToday != null ? 'VRM' : 'session';
  const consBadge = consToday != null ? 'VRM' : 'session';
  const batBadge = (batToday != null && batToday > 0) ? 'VRM' : 'session';
  todayGrid.innerHTML =
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">☀ Výroba FV</span><span class="stat-card-badge">' + pvBadge + '</span></div><div class="stat-value">' + fmt(pvDisplay, '', 1) + '<span class="stat-unit">kWh</span></div><div class="stat-sub">předpověď: ' + fmt(pvForecast, ' kWh', 1) + '</div><div class="stat-bar"><div class="stat-bar-fill solar" style="width: ' + pvForecastPct + '%"></div></div></div>' +
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">🏠 Spotřeba domu</span><span class="stat-card-badge">' + consBadge + '</span></div><div class="stat-value">' + fmt(consDisplay, '', 1) + '<span class="stat-unit">kWh</span></div></div>' +
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">🔋 Do baterie</span><span class="stat-card-badge">' + batBadge + '</span></div><div class="stat-value">' + fmt(batDisplay, '', 1) + '<span class="stat-unit">kWh</span></div></div>' +
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">⚡ Soběstačnost</span><span class="stat-card-badge">' + (selfSuff != null ? 'VRM' : 'n/a') + '</span></div><div class="stat-value">' + fmt(selfSuff, '', 0) + '<span class="stat-unit">%</span></div><div class="stat-bar"><div class="stat-bar-fill success" style="width: ' + (selfSuff || 0) + '%"></div></div></div>';
  document.getElementById('statsYesterdayGrid').innerHTML =
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">☀ Výroba FV</span><span class="stat-card-badge" style="background: #f1f5f9; color: var(--text-muted)">včera</span></div><div class="stat-value" style="color: var(--text-muted)">' + fmt(v.pv_yield_yesterday_kwh, '', 1) + '<span class="stat-unit">kWh</span></div></div>' +
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">🏠 Spotřeba</span><span class="stat-card-badge" style="background: #f1f5f9; color: var(--text-muted)">včera</span></div><div class="stat-value" style="color: var(--text-muted)">' + fmt(v.consumption_yesterday_kwh, '', 1) + '<span class="stat-unit">kWh</span></div></div>';
  const sessH = en.session_hours || 0;
  document.getElementById('statsSessionGrid').innerHTML =
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">⏱ Běží</span><span class="stat-card-badge">session</span></div><div class="stat-value">' + sessH.toFixed(1) + '<span class="stat-unit">h</span></div></div>' +
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">☀ FV</span><span class="stat-card-badge">session</span></div><div class="stat-value">' + fmt(en.pv_produced_kwh, '', 1) + '<span class="stat-unit">kWh</span></div></div>' +
    '<div class="stat-card"><div class="stat-card-header"><span class="stat-card-label">🏠 Dům</span><span class="stat-card-badge">session</span></div><div class="stat-value">' + fmt(en.home_consumed_kwh, '', 1) + '<span class="stat-unit">kWh</span></div></div>';
  renderForecastChart(s);
}

function renderForecastChart(s) {
  const f = s.forecast || {};
  const times = f.hourly_times || [];
  const rad = f.hourly_radiation || [];
  const cloud = f.hourly_cloudcover || [];
  if (!times.length) return;
  const labels = times.slice(0, 24).map(t => t.slice(11, 16));
  const data = { labels, datasets: [
    { label: 'Radiace (W/m²)', data: rad.slice(0, 24), borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.15)', fill: true, tension: 0.3, yAxisID: 'y', pointRadius: 0, borderWidth: 2 },
    { label: 'Oblačnost (%)', data: cloud.slice(0, 24), borderColor: '#64748b', fill: false, tension: 0.3, yAxisID: 'y1', pointRadius: 0, borderWidth: 2, borderDash: [4,3] }
  ]};
  const opts = { responsive: true, maintainAspectRatio: false, scales: {
    x: { ticks: { color: '#64748b', font: { size: 10 }, maxTicksLimit: 12 }, grid: { color: '#f1f5f9' } },
    y: { position: 'left', ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#f1f5f9' }, title: { display: true, text: 'W/m²', color: '#64748b' } },
    y1: { position: 'right', min: 0, max: 100, ticks: { color: '#64748b', font: { size: 10 } }, grid: { display: false }, title: { display: true, text: '%', color: '#64748b' } }
  }, plugins: { legend: { labels: { color: '#0f172a', font: { family: 'Inter', size: 12 } } } } };
  if (forecastChart) { forecastChart.data = data; forecastChart.update('none'); }
  else { forecastChart = new Chart(document.getElementById('forecastChart'), { type: 'line', data, options: opts }); }
}

// ===== v3.7.3 NEW: Aktuální režim hero card (v3.7.4 fix: použít current_scene) =====
function renderCurrentModeCard(s) {
  const card = document.getElementById('currentModeCard');
  const icon = document.getElementById('currentModeIcon');
  const title = document.getElementById('currentModeTitle');
  const desc = document.getElementById('currentModeDesc');
  const meta = document.getElementById('currentModeMeta');
  if (!card) return;

  const ps = s.preshower || {};
  const cleaning = s.cleaning || {};
  const water = s.spa.current_temp;
  const target = s.spa.target_temp;
  const scene = s.current_scene || 'solar_auto';

  let modeKey, modeIcon, modeTitle, modeDesc, modeMeta;

  // Priority: pre-shower > cleaning > scene (kde scene='heat_now' = override)
  if (ps.running) {
    modeKey = 'preshower';
    modeIcon = '🛁';
    modeTitle = 'Příprava vířivky';
    modeDesc = ps.target_time_iso ? `Hotovo v ${ps.target_time_iso}` : 'Připravuji…';
    modeMeta = `voda ${water || '?'}°C → cíl ${ps.target_temp}°C`;
  } else if (cleaning.running) {
    modeKey = 'cleaning';
    modeIcon = '🧼';
    modeTitle = 'Čistící program';
    const remHours = cleaning.remaining_sec ? (cleaning.remaining_sec / 3600).toFixed(1) : '?';
    modeDesc = `${cleaning.duration_hours}h program · zbývá ${remHours}h`;
    modeMeta = `progres ${cleaning.progress_pct ? cleaning.progress_pct.toFixed(0) : 0}%`;
  } else if (scene === 'heat_now' || s.override_active) {
    modeKey = 'heat';
    modeIcon = '♨';
    modeTitle = 'Ohřát hned';
    modeDesc = 'Override aktivní · topí dokud nedosáhne cíle';
    modeMeta = `voda ${water || '?'}°C → cíl ${target || 38}°C`;
  } else if (scene === 'gentle') {
    modeKey = 'gentle';
    modeIcon = '🧒';
    modeTitle = 'Mírný 33°C';
    modeDesc = 'Pro děti během dne';
    modeMeta = `voda ${water || '?'}°C → cíl ${target || 33}°C`;
  } else if (scene === 'off') {
    modeKey = 'off';
    modeIcon = '⏸';
    modeTitle = 'Vypnuto';
    modeDesc = 'Vířivka v klidu, nic se nedělá';
    modeMeta = `voda ${water || '?'}°C`;
  } else {
    // Default solar_auto
    modeKey = 'solar';
    modeIcon = '☀';
    modeTitle = 'Solar auto';
    modeDesc = 'Standard 38°C podle FVE přebytku';
    modeMeta = `voda ${water || '?'}°C → cíl ${target || 38}°C`;
  }

  card.className = 'mode-card mode-' + modeKey;
  icon.textContent = modeIcon;
  title.textContent = modeTitle;
  desc.textContent = modeDesc;
  meta.textContent = modeMeta;

  // Highlight aktivního scene tlačítka (podle skutečné scény, ne podle teploty)
  document.getElementById('sceneAutoBtn').classList.toggle('active', modeKey === 'solar');
  document.getElementById('sceneGentleBtn').classList.toggle('active', modeKey === 'gentle');
  document.getElementById('sceneHeatBtn').classList.toggle('active', modeKey === 'heat');
}

// ===== v3.7.3 NEW: Scheduler status bar (v3.7.4: schovat pokud paused) =====
async function renderSchedulerStatusBar(s) {
  const bar = document.getElementById('schedulerStatusBar');
  const icon = document.getElementById('schedulerStatusIcon');
  const label = document.getElementById('schedulerStatusLabel');
  const inline = document.getElementById('schedulerNextTriggerInline');
  if (!bar) return;

  // Fetch schedule status
  try {
    const sched = await fetch('/api/schedule').then(r => r.json());

    // v3.7.4: schovat pokud planovac neni aktivni (chce uzivatel cisty UI)
    if (!sched.enabled || !sched.global_enabled) {
      bar.style.display = 'none';
      return;
    }

    // Aktivni planovac - ukaz info
    bar.style.display = 'flex';
    bar.className = 'scheduler-status-bar active';
    icon.textContent = '✓';
    label.textContent = 'Plánovač aktivní';
    const next = sched.next_trigger;
    if (next) {
      const inMin = next.in_minutes;
      let when;
      if (inMin < 60) when = `za ${inMin} min`;
      else if (inMin < 1440) when = `za ${Math.round(inMin / 60)} h`;
      else when = `za ${Math.round(inMin / 1440)} dní`;
      inline.textContent = `· další: ${next.name} ${when}`;
    } else {
      inline.textContent = '';
    }
  } catch (e) {
    bar.style.display = 'none';
  }
}

// ===== v3.7.3 NEW: Programové přepnutí tabu =====
function showTab(tabId) {
  const btn = document.querySelector(`.tab[data-tab="${tabId}"], .mobile-nav-item[data-tab="${tabId}"], .more-item[data-tab="${tabId}"]`);
  if (btn) btn.click();
}

function renderControl(s) {
  // v3.7.3 NEW: Aktuální režim hero card
  renderCurrentModeCard(s);
  renderSchedulerStatusBar(s);

  // v3.7 NEW: Pre-shower card
  const ps = s.preshower || {};
  const psIdle = document.getElementById('preshowerIdle');
  const psRunning = document.getElementById('preshowerRunning');
  const psCard = document.getElementById('preshowerCard');
  const psPred = document.getElementById('preshowerHeatingPred');

  if (ps.running) {
    psIdle.style.display = 'none';
    psRunning.style.display = 'block';
    psCard.className = 'plan-card normal';

    const stateLabels = {
      'idle': 'Čekám',
      'warming': '🔥 Topí',
      'bubbles_on': '💨 Bublinky',
      'bubbles_off': '⏱ Klid',
      'ready': '✓ HOTOVO',
      'cancelled': '✗ Zrušeno',
      'failed': '⚠ Chyba',
    };
    document.getElementById('preshowerStateBadge').textContent = stateLabels[ps.state] || ps.state;

    let countdown;
    if (ps.state === 'ready') {
      countdown = '🎉 Vířivka je připravena! Cílová teplota dosažena.';
    } else if (ps.time_remaining_sec != null && ps.time_remaining_sec > 0) {
      const mins = Math.round(ps.time_remaining_sec / 60);
      const targetTime = ps.target_time_iso || '?';
      countdown = `Hotovo za <strong>${mins} min</strong> (${targetTime}) · cíl ${ps.target_temp}°C`;
    } else {
      countdown = '—';
    }
    document.getElementById('preshowerCountdown').innerHTML = countdown;

    document.getElementById('preshowerProgressFill').style.width = (ps.progress_pct || 0) + '%';

    // Stages
    const sw = document.getElementById('stageWarming');
    const sb = document.getElementById('stageBubbles');
    const sr = document.getElementById('stageReady');
    sw.className = 'preshower-stage';
    sb.className = 'preshower-stage';
    sr.className = 'preshower-stage';
    if (ps.state === 'warming') { sw.classList.add('active'); }
    else if (ps.state === 'bubbles_on') { sw.classList.add('done'); sb.classList.add('active'); }
    else if (ps.state === 'bubbles_off') { sw.classList.add('done'); sb.classList.add('done'); }
    else if (ps.state === 'ready') { sw.classList.add('done'); sb.classList.add('done'); sr.classList.add('active'); }
  } else {
    psIdle.style.display = 'block';
    psRunning.style.display = 'none';
    psCard.className = 'plan-card unknown';
    // Predikce ohrevu - ukaze ze 28°C jak dlouho topit
    if (s.spa && s.spa.current_temp != null && s.spa.target_temp != null) {
      // Get from API on next tick - tady jen preset
      psPred.textContent = '';
    }
  }

  const setStatus = (id, val) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (val == null) el.textContent = '—';
    else el.textContent = (typeof val === 'boolean') ? (val ? 'ZAP' : 'vyp') : val;
    el.style.color = val === true ? 'var(--success)' : 'var(--text-muted)';
  };
  setStatus('heaterStatus', s.spa.heater);
  setStatus('filterStatus', s.spa.filter);
  setStatus('bubblesStatus', s.spa.bubbles);
  setStatus('jetsStatus', s.spa.jets);
  const tempStr = s.spa.target_temp != null ? s.spa.target_temp + ' °C' : '—';
  setStatus('tempStatus', tempStr);
  const tv = document.getElementById('tempVal');
  if (tv) tv.textContent = tempStr;

  // --- SANITIZER status line (novy design) ---
  const sanStatusEl = document.getElementById('sanStatus');
  const c = s.cleaning || {};
  if (c.running) {
    const h = Math.round(c.duration_hours);
    sanStatusEl.textContent = `${h}h program · zbývá ${fmtDuration(c.remaining_sec)}`;
    sanStatusEl.style.color = 'var(--teal)';
  } else if (s.spa.sanitizer === true) {
    sanStatusEl.textContent = 'ZAP (ručně, bez timeru)';
    sanStatusEl.style.color = 'var(--success)';
  } else if (s.spa.sanitizer === false) {
    sanStatusEl.textContent = 'vyp';
    sanStatusEl.style.color = 'var(--text-muted)';
  } else {
    sanStatusEl.textContent = '—';
    sanStatusEl.style.color = 'var(--text-muted)';
  }

  // Oznacit aktivni cleaning program (3h / 5h / 8h)
  const setBtn = (id, active, cls) => { const el = document.getElementById(id); if (!el) return; el.classList.toggle(cls, active); };
  setBtn('heaterOnBtn', s.spa.heater === true, 'on'); setBtn('heaterOffBtn', s.spa.heater === false, 'off');
  setBtn('filterOnBtn', s.spa.filter === true, 'on'); setBtn('filterOffBtn', s.spa.filter === false, 'off');
  setBtn('bubblesOnBtn', s.spa.bubbles === true, 'on'); setBtn('bubblesOffBtn', s.spa.bubbles === false, 'off');
  setBtn('jetsOnBtn', s.spa.jets === true, 'on'); setBtn('jetsOffBtn', s.spa.jets === false, 'off');

  // Cleaning tlacitka - vyznacit podle aktivniho programu
  const activeHours = c.running ? Math.round(c.duration_hours) : 0;
  setBtn('clean3Btn', activeHours === 3, 'teal');
  setBtn('clean5Btn', activeHours === 5, 'teal');
  setBtn('clean8Btn', activeHours === 8, 'teal');
  // OFF je cerveny pokud nic nebezi a sanitizer je vyp
  setBtn('sanOffBtn', !c.running && s.spa.sanitizer === false, 'off');

  // Progress bar
  const progRow = document.getElementById('sanProgressRow');
  if (c.running) {
    progRow.style.display = 'block';
    const h = Math.round(c.duration_hours);
    document.getElementById('sanProgLabel').textContent = `🧼 ${h}h čisticí program`;
    document.getElementById('sanProgRemaining').textContent = `zbývá ${fmtDuration(c.remaining_sec)}`;
    document.getElementById('sanProgFill').style.width = (c.progress_pct || 0) + '%';
    if (c.started_at && c.ends_at) {
      const startD = new Date(c.started_at * 1000);
      const endD = new Date(c.ends_at * 1000);
      document.getElementById('sanProgStart').textContent = startD.toLocaleTimeString('cs-CZ', {hour:'2-digit',minute:'2-digit'});
      document.getElementById('sanProgEnd').textContent = endD.toLocaleTimeString('cs-CZ', {hour:'2-digit',minute:'2-digit'});
    }
  } else {
    progRow.style.display = 'none';
  }

  setBtn('sceneAutoBtn', s.current_scene === 'solar_auto' && !s.override_active, 'active');
  setBtn('sceneGentleBtn', s.current_scene === 'gentle', 'active');
  setBtn('sceneHeatBtn', s.current_scene === 'heat_now' || s.override_active, 'active');
}

function renderChart(ticks) {
  if (!ticks.length) return;
  const labels = ticks.map(t => fmtTimeShort(t.ts));
  const data = { labels, datasets: [
    { label: 'FV (W)', data: ticks.map(t => t.pv), borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.12)', fill: true, tension: 0.3, yAxisID: 'y', pointRadius: 0, borderWidth: 2 },
    { label: 'Odběr (W)', data: ticks.map(t => t.load), borderColor: '#64748b', fill: false, tension: 0.3, yAxisID: 'y', pointRadius: 0, borderWidth: 2 },
    { label: 'Přebytek (W)', data: ticks.map(t => t.surplus), borderColor: '#2563eb', fill: false, tension: 0.3, yAxisID: 'y', pointRadius: 0, borderWidth: 2 },
    { label: 'SOC (%)', data: ticks.map(t => t.soc), borderColor: '#16a34a', borderDash: [4,3], fill: false, tension: 0.1, yAxisID: 'y1', pointRadius: 0, borderWidth: 2 }
  ]};
  const opts = { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: {
    x: { ticks: { color: '#64748b', maxTicksLimit: 12, font: { size: 10 } }, grid: { color: '#f1f5f9' } },
    y: { position: 'left', ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#f1f5f9' }, title: { display: true, text: 'W', color: '#64748b' } },
    y1: { position: 'right', min: 0, max: 100, ticks: { color: '#64748b', font: { size: 10 } }, grid: { display: false }, title: { display: true, text: '%', color: '#64748b' } }
  }, plugins: { legend: { labels: { color: '#0f172a', font: { family: 'Inter', size: 12 } } } } };
  if (chart) { chart.data = data; chart.update('none'); }
  else { chart = new Chart(document.getElementById('chart'), { type: 'line', data, options: opts }); }
}

function renderDecisions(ticks) {
  const html = ticks.map(t =>
    '<div class="dec-row"><div class="dec-time">' + fmtTime(t.ts) + '</div><div><span class="dec-state-badge dec-state-' + t.state + '">' + (stateNames[t.state] || t.state) + '</span></div><div class="dec-num">' + fmt(t.surplus, ' W', 0) + '</div><div class="dec-num">' + fmt(t.soc, '%', 0) + '</div><div class="dec-num">' + fmt(t.water_temp, '°C', 0) + '</div><div class="dec-reason">' + (t.reason || '') + '</div></div>'
  ).join('');
  document.getElementById('decisionsList').innerHTML = html || '<div style="padding: 20px; color: var(--text-muted); text-align: center;">Zatím žádná data.</div>';
}

// v4.3.0 NEW: Live status panel pro tab Rozhodnuti
function renderEngineStatus(es) {
  const grid = document.getElementById('engineStatusGrid');
  if (!grid) return;

  // Update time
  const ut = document.getElementById('engineUpdateTime');
  if (ut) ut.textContent = 'aktualizováno: ' + new Date(es.ts * 1000).toLocaleTimeString('cs-CZ');

  // Helper: barva podle hodnoty
  const ok = (v) => v ? '<span style="color: var(--success); font-weight: 700;">✓</span>' : '<span style="color: var(--text-muted);">·</span>';
  const bad = (v) => v ? '<span style="color: var(--danger); font-weight: 700;">⚠</span>' : '<span style="color: var(--text-muted);">·</span>';
  const fmtSec = (s) => s == null ? '—' : (s < 60 ? s + ' s' : Math.floor(s / 60) + ':' + (s % 60).toString().padStart(2, '0'));

  // Card 1: Aktualni stav
  const st = es.state;
  const stateBadgeClass = 'dec-state-' + st.current;
  const stateName = stateNames[st.current] || st.current;
  let timeBadge = '';
  if (st.time_remaining_sec) {
    timeBadge = '<div style="margin-top: 8px; font-family: var(--mono); font-size: 11px; color: var(--warning);">⏱ čeká ' + fmtSec(st.time_remaining_sec) + ' (' + st.min_label + ')</div>';
  } else if (st.min_required_sec) {
    timeBadge = '<div style="margin-top: 8px; font-family: var(--mono); font-size: 11px; color: var(--success);">✓ ' + st.min_label + ' uplynul (' + fmtSec(st.time_in_state_sec) + ')</div>';
  }
  let overrideBadge = '';
  if (st.override_active) {
    overrideBadge = '<div style="margin-top: 8px; padding: 6px 10px; background: var(--purple-soft); border-radius: 6px; font-family: var(--mono); font-size: 11px; color: var(--purple); font-weight: 700;">🔒 OVERRIDE: ' + (st.override_reason || '') + '</div>';
  }
  let sceneBadge = '';
  if (st.current_scene && st.current_scene !== 'solar_auto') {
    const sceneNames = { heat_now: '♨ Ohřát hned', gentle: '🌱 Mírný režim', solar_auto: '☀ Solar auto' };
    sceneBadge = '<div style="margin-top: 6px; font-family: var(--mono); font-size: 10px; color: var(--text-muted);">scéna: ' + (sceneNames[st.current_scene] || st.current_scene) + '</div>';
  }

  // Card 2: Přebytek
  const su = es.surplus;
  const stableClass = su.is_above_on ? 'val-pos' : (su.is_below_off ? 'val-neg' : '');
  const stableBar = su.stable_w != null && su.on_active_w
    ? Math.min(100, Math.max(0, (su.stable_w / es.thresholds.on_active_w) * 100)) : 0;

  // Card 3: Strategie + prahy
  const th = es.thresholds;
  const stratNames = { aggressive: '🚀 AGRESIVNÍ', normal: '⚖ NORMÁL', conservative: '🐢 OPATRNÝ', survive: '🛡 ŠETŘÍM', unknown: '? ČEKÁM' };

  // Card 4: Baterie
  const ba = es.battery;
  const socColor = ba.is_under_min ? 'val-neg' : (ba.is_battery_full ? 'val-pos' : '');

  // Card 5: Vířivka stav
  const sp = es.spa;
  const tempColor = sp.is_at_target ? 'val-pos' : '';

  // Card 6: Spike protection
  const spk = es.spike;
  const spikeClass = spk.active ? 'val-neg' : '';

  // Card 7: Bezpečnost
  const env = es.env;
  const vt = es.victron;
  const safetyItems = [];
  if (vt.stale) safetyItems.push('<div style="color: var(--danger);">⚠ Victron MQTT stale (' + vt.last_update_age_sec + 's)</div>');
  if (!sp.online) safetyItems.push('<div style="color: var(--danger);">⚠ Vířivka offline (' + sp.consecutive_failures + ' chyb)</div>');
  if (sp.error_code) safetyItems.push('<div style="color: var(--danger);">⚠ Chyba vířivky: ' + sp.error_code + '</div>');
  if (env.is_frost) safetyItems.push('<div style="color: var(--danger);">⚠ Mráz: ' + env.air_temp_c + '°C < ' + env.min_air_temp_c + '°C</div>');
  if (ba.is_under_min) safetyItems.push('<div style="color: var(--danger);">⚠ SOC ' + ba.soc_pct + '% pod min ' + ba.min_soc_hard + '%</div>');
  if (env.wind_kmh != null && env.wind_kmh > env.wind_reduce_kmh) safetyItems.push('<div style="color: var(--warning);">⚠ Silný vítr ' + env.wind_kmh + ' km/h</div>');
  if (!safetyItems.length) safetyItems.push('<div style="color: var(--success);">✓ Vše v pořádku</div>');

  // Render
  grid.innerHTML = `
    <div class="stat-card">
      <div class="stat-card-header">
        <span class="stat-card-label">📍 Aktuální stav</span>
        <span class="dec-state-badge ${stateBadgeClass}">${stateName.toUpperCase()}</span>
      </div>
      <div class="stat-value" style="font-size: 22px;">${fmtSec(st.time_in_state_sec)}<span class="stat-unit"> v stavu</span></div>
      ${timeBadge}
      ${overrideBadge}
      ${sceneBadge}
    </div>

    <div class="stat-card">
      <div class="stat-card-header">
        <span class="stat-card-label">⚡ Přebytek (rozhodovací)</span>
        <span class="stat-card-badge">stable ${su.stability_window_sec}s</span>
      </div>
      <div class="stat-value ${stableClass}">${fmt(su.stable_w, '', 0)}<span class="stat-unit"> W</span></div>
      <div class="stat-bar"><div class="stat-bar-fill primary" style="width: ${stableBar}%"></div></div>
      <div class="stat-sub">aktuální ${fmt(su.current_w, ' W', 0)} · max za ${su.off_window_sec}s ${fmt(su.max_recent_w, ' W', 0)}</div>
      <div class="stat-sub">vzorků: ${su.samples_in_window}</div>
    </div>

    <div class="stat-card">
      <div class="stat-card-header">
        <span class="stat-card-label">🎯 Strategie + prahy</span>
        <span class="stat-card-badge">${th.is_dynamic ? 'dynamic' : 'static'}</span>
      </div>
      <div class="stat-value" style="font-size: 18px; color: var(--primary);">${stratNames[th.strategy] || th.strategy}</div>
      <div class="stat-sub" style="margin-top: 8px;">
        zap od: <strong style="color: var(--success)">${fmt(th.on_active_w, ' W', 0)}</strong>
        · vyp pod: <strong style="color: var(--warning)">${fmt(th.off_active_w, ' W', 0)}</strong>
      </div>
      ${th.is_dynamic ? '<div class="stat-sub">statický: ' + th.on_static_w + ' / ' + th.off_static_w + ' W</div>' : ''}
      <div class="stat-sub">${th.strategy_reason || ''}</div>
    </div>

    <div class="stat-card">
      <div class="stat-card-header">
        <span class="stat-card-label">🔋 Baterie</span>
        <span class="stat-card-badge">${ba.is_battery_full ? 'BAT-FULL' : 'normal'}</span>
      </div>
      <div class="stat-value ${socColor}">${fmt(ba.soc_pct, '', 0)}<span class="stat-unit"> %</span></div>
      <div class="stat-bar"><div class="stat-bar-fill success" style="width: ${ba.soc_pct || 0}%"></div></div>
      <div class="stat-sub">hard min: ${ba.min_soc_hard}% · BAT-FULL od: ${ba.battery_full_pct}%</div>
    </div>

    <div class="stat-card">
      <div class="stat-card-header">
        <span class="stat-card-label">♨ Vířivka</span>
        <span class="stat-card-badge">${sp.heater_on ? 'topí' : 'off'}</span>
      </div>
      <div class="stat-value ${tempColor}">${fmt(sp.current_temp_c, '', 1)}<span class="stat-unit"> °C</span></div>
      <div class="stat-sub">cíl: ${fmt(sp.target_temp_c, ' °C', 0)} · max: ${sp.max_temp_c}°C</div>
      ${sp.is_at_target ? '<div class="stat-sub" style="color: var(--success); margin-top: 4px;">✓ teplota dosažena</div>' : ''}
      ${sp.error_code ? '<div class="stat-sub" style="color: var(--danger); margin-top: 4px;">⚠ chyba: ' + sp.error_code + '</div>' : ''}
    </div>

    <div class="stat-card">
      <div class="stat-card-header">
        <span class="stat-card-label">⚠ Spike protection</span>
        <span class="stat-card-badge">fáze ${spk.spa_phase}</span>
      </div>
      ${spk.active ? `
        <div class="stat-value ${spikeClass}">${fmtSec(spk.remaining_sec)}<span class="stat-unit"> cooldown</span></div>
        <div class="stat-sub" style="color: var(--warning); margin-top: 4px;">⚠ ${spk.last_reason || 'spike aktivní'}</div>
      ` : `
        <div class="stat-value" style="font-size: 22px; color: var(--success);">✓ klid</div>
        <div class="stat-sub">ignorovací okno: ${spk.ignore_window_sec}s</div>
        <div class="stat-sub">grid-neutral nad: ${spk.safety_surplus_w}W přebytku</div>
      `}
    </div>

    <div class="stat-card" style="grid-column: 1 / -1;">
      <div class="stat-card-header">
        <span class="stat-card-label">🛡 Bezpečnostní kontroly</span>
      </div>
      <div style="font-family: var(--mono); font-size: 13px; line-height: 1.8;">
        ${safetyItems.join('')}
      </div>
    </div>
  `;

  // Phases panel
  const ph = document.getElementById('phasesPanel');
  if (ph && es.phases) {
    let phasesHtml = '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px;">';
    es.phases.forEach(p => {
      const isSpa = p.is_spa_phase;
      const phaseLabel = p.label + (isSpa ? ' ♨' : '');
      const pct = p.pct || 0;
      const barColor = p.overload ? 'var(--danger)' : (pct > 80 ? 'var(--warning)' : (isSpa ? 'var(--purple)' : 'var(--success)'));
      const valColor = p.overload ? 'val-neg' : '';
      const overloadBadge = p.overload ? ' <span style="background: var(--danger); color: white; padding: 2px 6px; border-radius: 4px; font-size: 9px; font-weight: 700; letter-spacing: 1px;">OVERLOAD</span>' : '';
      phasesHtml += `
        <div style="background: ${isSpa ? 'rgba(147, 51, 234, 0.05)' : 'transparent'}; padding: 12px; border-radius: 8px; border: 1px solid ${isSpa ? 'var(--purple)' : 'var(--border)'};">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--text-muted); letter-spacing: 1.5px;">${phaseLabel}${overloadBadge}</span>
            <span style="font-family: var(--mono); font-size: 16px; font-weight: 700;" class="${valColor}">${fmt(p.load_w, ' W', 0)}</span>
          </div>
          <div class="stat-bar"><div class="stat-bar-fill" style="width: ${Math.min(100, pct)}%; background: ${barColor};"></div></div>
          <div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted); margin-top: 4px;">
            ${pct ? pct.toFixed(0) : '0'}% z limitu ${p.max_w} W (Multiplus II ochrana)
          </div>
        </div>
      `;
    });
    phasesHtml += '</div>';
    phasesHtml += '<div style="margin-top: 14px; padding: 10px 12px; background: var(--surface); border-radius: 8px; font-size: 12px; color: var(--text-muted); line-height: 1.6;">ℹ Vířivka je na fázi <strong>' + es.spike.spa_phase + '</strong>. Spike protection ji vypne pouze když přijde skok na <strong>' + es.spike.spa_phase + '</strong>, nebo když kterákoli fáze přesáhne <strong>' + es.phases[0].max_w + ' W</strong> (Multiplus II shutdown protection).</div>';
    ph.innerHTML = phasesHtml;
  }

  // v4.3.2 NEW: Seplos BMS - napeti vsech clanku z RS485
  // Vse v try/catch aby pripadna chyba nikdy nezablokovala zbytek dashboardu.
  try {
    var cellsDiv = document.getElementById('cellsPanel');
    if (cellsDiv && es.seplos) {
      var sep = es.seplos;
      if (!sep.enabled) {
        cellsDiv.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:14px;font-size:12px;">Seplos RS485 není zapnut v config.yaml (sekce <code>seplos:</code>).</div>';
      } else if (!sep.online || !sep.all_cells || sep.all_cells.length === 0) {
        cellsDiv.innerHTML = '<div style="color:var(--warning);text-align:center;padding:14px;font-size:12px;">⏳ Čekám na data ze Seplos BMS… (port /dev/ttyUSB0, 19200 baud)</div>';
      } else {
        var cells = sep.all_cells;
        var minV = sep.min_cell_voltage || 0;
        var maxV = sep.max_cell_voltage || 0;
        var spreadMv = Math.round((maxV - minV) * 1000);
        var spreadColor = spreadMv > 100 ? 'var(--danger)' : (spreadMv > 50 ? 'var(--warning)' : 'var(--success)');

        // v4.3.2 NEW: prumer napeti pro heatmap (modra pod / cervena nad)
        var avgV = 0;
        for (var ai = 0; ai < cells.length; ai++) avgV += cells[ai].v;
        avgV = avgV / cells.length;
        // Maximalni odchylka od prumeru (pro normalizaci intenzity barvy)
        var maxDeviation = Math.max(maxV - avgV, avgV - minV);
        if (maxDeviation < 0.001) maxDeviation = 0.001; // div by zero guard

        // Heatmap funkce: vraci CSS background podle odchylky napeti od prumeru
        // Pod prumerem -> jemna modra, nad prumerem -> jemna cervena. Intenzita normalizovana.
        function heatBg(v) {
          var dev = v - avgV;
          var intensity = Math.min(1, Math.abs(dev) / maxDeviation);
          var alpha = (intensity * 0.35).toFixed(2); // max 35% saturace, jemne
          if (dev < 0) return 'rgba(37, 99, 235, ' + alpha + ')';  // primary (modra)
          if (dev > 0) return 'rgba(220, 38, 38, ' + alpha + ')';   // danger (cervena)
          return 'var(--surface)';
        }

        var html = '<div style="margin-bottom:12px;font-size:12px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;">'
          + '<span style="font-size:10px;background:rgba(34,197,94,.15);color:var(--success);padding:2px 8px;border-radius:20px;font-weight:700;letter-spacing:1px;">RS485 LIVE</span>'
          + '<span>' + sep.pack_count + ' pack × ' + sep.cells_per_pack + 'S = ' + cells.length + ' článků</span>'
          + '<span>min: <strong style="color:var(--primary);font-family:var(--mono)">' + minV.toFixed(3) + ' V</strong></span>'
          + '<span>max: <strong style="color:var(--success);font-family:var(--mono)">' + maxV.toFixed(3) + ' V</strong></span>'
          + '<span>avg: <strong style="font-family:var(--mono)">' + avgV.toFixed(3) + ' V</strong></span>'
          + '<span>spread: <strong style="color:' + spreadColor + ';font-family:var(--mono)">' + spreadMv + ' mV</strong></span>'
          + '</div>';

        // Seskup po packach
        var packNums = [];
        for (var i = 0; i < cells.length; i++) {
          if (packNums.indexOf(cells[i].pack) < 0) packNums.push(cells[i].pack);
        }
        packNums.sort(function(a, b) { return a - b; });

        for (var pi = 0; pi < packNums.length; pi++) {
          var pn = packNums[pi];
          var idx = pn - 1;
          var packV = (sep.pack_voltages && sep.pack_voltages[idx] != null) ? sep.pack_voltages[idx].toFixed(2) + ' V' : '—';
          var packA = (sep.pack_currents && sep.pack_currents[idx] != null) ? sep.pack_currents[idx].toFixed(1) + ' A' : '—';
          var packSoc = (sep.pack_soc && sep.pack_soc[idx] != null) ? sep.pack_soc[idx].toFixed(0) + ' %' : '—';
          var packTemps = (sep.pack_temperatures && sep.pack_temperatures[idx]) || [];
          var tempStr = packTemps.length ? packTemps.map(function(t) { return t.toFixed(1) + '°'; }).join(' / ') : '—';

          html += '<div style="margin-bottom:14px;">'
            + '<div style="font-size:11px;font-weight:700;color:var(--text-muted);letter-spacing:1.5px;margin-bottom:6px;">'
            + 'PACK ' + pn + ' &nbsp; <span style="font-weight:400;font-family:var(--mono)">' + packV + ' · ' + packA + ' · SOC ' + packSoc + ' · 🌡 ' + tempStr + '</span>'
            + '</div>'
            + '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(70px,1fr));gap:5px;">';

          for (var ci = 0; ci < cells.length; ci++) {
            var c = cells[ci];
            if (c.pack !== pn) continue;
            var isMin = (c.pack === sep.min_cell_pack && c.cell === sep.min_cell_index);
            var isMax = (c.pack === sep.max_cell_pack && c.cell === sep.max_cell_index);
            var border = isMin ? '2px solid var(--primary)' : (isMax ? '2px solid var(--success)' : '1px solid var(--border)');
            // Hard limity (bezpecnost) prebijou heatmap
            var hardColor = c.v < 3.0 ? 'var(--danger)' : (c.v > 3.55 ? 'var(--warning)' : null);
            var textColor = hardColor || 'var(--text)';
            var bg = hardColor ? 'rgba(220,38,38,.15)' : heatBg(c.v);
            var badge = isMin ? '<div style="font-size:9px;color:var(--primary);font-weight:700;">MIN</div>'
                       : (isMax ? '<div style="font-size:9px;color:var(--success);font-weight:700;">MAX</div>'
                       : '<div style="font-size:9px;opacity:0">·</div>');
            html += '<div style="background:' + bg + ';border:' + border + ';border-radius:6px;padding:6px 3px;text-align:center;">'
              + '<div style="font-size:9px;color:var(--text-muted);margin-bottom:2px;">C' + c.cell + '</div>'
              + '<div style="font-size:12px;font-weight:700;color:' + textColor + ';font-family:var(--mono);">' + c.v.toFixed(3) + '</div>'
              + badge
              + '</div>';
          }
          html += '</div></div>';
        }

        // v4.3.2 NEW: Stitek hanby - kdo byl nejcasteji min/max za poslednich 24h
        var weak = sep.weakest_24h;
        var strong = sep.strongest_24h;
        if (weak || strong) {
          html += '<div style="margin-top:6px;padding:10px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:11px;font-family:var(--mono);color:var(--text-muted);line-height:1.6;">';
          if (weak) {
            html += '🔻 <strong style="color:var(--primary)">Nejslabší článek za 24h:</strong> P' + weak.pack + ' C' + weak.cell
                  + ' &nbsp; <span style="color:var(--text-dim)">(' + weak.count + '×, ' + weak.pct + '% vzorků)</span><br>';
          }
          if (strong) {
            html += '🔺 <strong style="color:var(--success)">Nejsilnější článek za 24h:</strong> P' + strong.pack + ' C' + strong.cell
                  + ' &nbsp; <span style="color:var(--text-dim)">(' + strong.count + '×, ' + strong.pct + '% vzorků)</span>';
          }
          html += '</div>';
        }

        cellsDiv.innerHTML = html;
      }
    }
  } catch (e) {
    console.warn('cellsPanel render error:', e);
  }
}

// v4.3.2 NEW: Render porovnani FULL vs LOW snapshotu - odhali clanek s nejvetsim poklesem
async function renderSnapshots() {
  var panel = document.getElementById('snapshotsPanel');
  if (!panel) return;
  try {
    var data = await fetch('/api/seplos/snapshots?limit=20').then(function(r) { return r.json(); });
    var snaps = data.snapshots || [];
    var full = data.last_full;
    var low = data.last_low;

    if (snaps.length === 0) {
      panel.innerHTML = '<div style="color:var(--text-muted);text-align:center;font-size:12px;padding:14px;line-height:1.6;">'
        + 'Zatím žádný snapshot. <br>'
        + 'První FULL se uloží při dosažení SOC ≥99 %, první LOW při poklesu pod 20 %.<br>'
        + '<span style="color:var(--text-dim)">Snapshoty se ukládají do <code>data/cell_snapshots.jsonl</code> a přežijí restart.</span>'
        + '</div>';
      return;
    }

    var html = '<div style="margin-bottom:10px;font-size:11px;color:var(--text-muted);font-family:var(--mono);letter-spacing:0.5px;">'
             + 'celkem snapshotů: <strong>' + data.count + '</strong></div>';

    // Hlavni vychytavka: porovnani posledniho FULL a LOW snapshotu
    if (full && low) {
      var fullCells = full.all_cells || [];
      var lowCells = low.all_cells || [];
      // Map cells by "P{pack}C{cell}" pro lookup
      var lowMap = {};
      for (var li = 0; li < lowCells.length; li++) {
        var lc = lowCells[li];
        lowMap['P' + lc.pack + 'C' + lc.cell] = lc.v;
      }
      // Spocitej delta = full.v - low.v pro kazdou bunku, najdi max delta = nejslabsi
      var deltas = [];
      for (var fi = 0; fi < fullCells.length; fi++) {
        var fc = fullCells[fi];
        var lowV = lowMap['P' + fc.pack + 'C' + fc.cell];
        if (lowV == null) continue;
        var dropMv = Math.round((fc.v - lowV) * 1000);
        deltas.push({ pack: fc.pack, cell: fc.cell, fullV: fc.v, lowV: lowV, dropMv: dropMv });
      }
      deltas.sort(function(a, b) { return b.dropMv - a.dropMv; });

      var fullDate = new Date(full.ts * 1000).toLocaleString('cs-CZ', { dateStyle:'short', timeStyle:'short' });
      var lowDate  = new Date(low.ts  * 1000).toLocaleString('cs-CZ', { dateStyle:'short', timeStyle:'short' });

      html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px;">'
            + '<div style="padding:10px 12px;background:rgba(34,197,94,.08);border:1px solid var(--success);border-radius:8px;">'
            +   '<div style="font-size:10px;letter-spacing:1.5px;color:var(--success);font-weight:700;margin-bottom:4px;">🔋 FULL @ SOC ' + full.soc.toFixed(1) + '%</div>'
            +   '<div style="font-family:var(--mono);font-size:11px;color:var(--text-muted);">' + fullDate + '</div>'
            +   '<div style="font-family:var(--mono);font-size:11px;margin-top:4px;">min: ' + (full.min_cell_voltage||0).toFixed(3) + ' V · max: ' + (full.max_cell_voltage||0).toFixed(3) + ' V · spread ' + (full.spread_mv || '?') + ' mV</div>'
            + '</div>'
            + '<div style="padding:10px 12px;background:rgba(37,99,235,.08);border:1px solid var(--primary);border-radius:8px;">'
            +   '<div style="font-size:10px;letter-spacing:1.5px;color:var(--primary);font-weight:700;margin-bottom:4px;">🪫 LOW @ SOC ' + low.soc.toFixed(1) + '%</div>'
            +   '<div style="font-family:var(--mono);font-size:11px;color:var(--text-muted);">' + lowDate + '</div>'
            +   '<div style="font-family:var(--mono);font-size:11px;margin-top:4px;">min: ' + (low.min_cell_voltage||0).toFixed(3) + ' V · max: ' + (low.max_cell_voltage||0).toFixed(3) + ' V · spread ' + (low.spread_mv || '?') + ' mV</div>'
            + '</div>'
            + '</div>';

      // Tabulka top 5 nejslabsich (nejvetsi drop)
      if (deltas.length) {
        var worst = deltas.slice(0, 5);
        html += '<div style="font-size:11px;font-weight:700;color:var(--text-muted);letter-spacing:1.5px;margin-bottom:6px;">TOP 5 ČLÁNKŮ S NEJVĚTŠÍM POKLESEM (FULL → LOW)</div>'
              + '<div style="font-family:var(--mono);font-size:12px;">';
        for (var wi = 0; wi < worst.length; wi++) {
          var w = worst[wi];
          var medal = wi === 0 ? '🥇' : (wi === 1 ? '🥈' : (wi === 2 ? '🥉' : '  '));
          // Vyssi drop = vetsi vnitrni odpor / nizsi kapacita -> cervena
          var dropColor = w.dropMv > deltas[0].dropMv * 0.9 ? 'var(--danger)' : (wi < 2 ? 'var(--warning)' : 'var(--text)');
          html += '<div style="display:grid;grid-template-columns:30px 70px 1fr 80px;gap:8px;padding:5px 8px;border-bottom:1px solid var(--border);align-items:center;">'
                +   '<div>' + medal + '</div>'
                +   '<div><strong>P' + w.pack + ' C' + w.cell + '</strong></div>'
                +   '<div style="color:var(--text-muted);font-size:11px;">FULL ' + w.fullV.toFixed(3) + ' V → LOW ' + w.lowV.toFixed(3) + ' V</div>'
                +   '<div style="text-align:right;color:' + dropColor + ';font-weight:700;">−' + w.dropMv + ' mV</div>'
                + '</div>';
        }
        html += '</div>';
        var hintCell = worst[0];
        html += '<div style="margin-top:10px;padding:8px 12px;background:var(--surface);border-radius:6px;font-size:11px;color:var(--text-muted);line-height:1.5;">'
              + 'ℹ Článek <strong>P' + hintCell.pack + ' C' + hintCell.cell + '</strong> má největší pokles napětí mezi nabitým a vybitým stavem (' + hintCell.dropMv + ' mV). '
              + 'Pokud je tento článek opakovaně na vrcholu, má pravděpodobně nižší kapacitu nebo vyšší vnitřní odpor než ostatní.'
              + '</div>';
      }
    } else {
      // Mame jenom jeden typ snapshotu
      html += '<div style="padding:10px 12px;background:var(--surface);border-radius:6px;font-size:12px;color:var(--text-muted);line-height:1.6;">'
            + 'Pro porovnání potřebuji <strong>oba</strong> snapshoty: '
            + (full ? '✓ FULL' : '<span style="color:var(--warning)">? FULL (čeká na SOC ≥99%)</span>')
            + ' &nbsp; '
            + (low ? '✓ LOW' : '<span style="color:var(--warning)">? LOW (čeká na SOC ≤20%)</span>')
            + '</div>';
    }

    // Seznam vsech snapshotu (kompaktni)
    html += '<details style="margin-top:14px;"><summary style="cursor:pointer;font-size:11px;color:var(--text-muted);letter-spacing:1px;text-transform:uppercase;font-weight:700;">Historie snapshotů (' + snaps.length + ')</summary>'
          + '<div style="margin-top:8px;font-family:var(--mono);font-size:11px;">';
    for (var si = 0; si < snaps.length; si++) {
      var sn = snaps[si];
      var dt = new Date(sn.ts * 1000).toLocaleString('cs-CZ', { dateStyle:'short', timeStyle:'short' });
      var typeBadge = sn.type === 'FULL'
        ? '<span style="background:rgba(34,197,94,.15);color:var(--success);padding:1px 6px;border-radius:4px;font-weight:700;">FULL</span>'
        : '<span style="background:rgba(37,99,235,.15);color:var(--primary);padding:1px 6px;border-radius:4px;font-weight:700;">LOW</span>';
      html += '<div style="display:grid;grid-template-columns:60px 110px 1fr;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);">'
            +   '<div>' + typeBadge + '</div>'
            +   '<div style="color:var(--text-muted);">' + dt + '</div>'
            +   '<div>SOC ' + sn.soc.toFixed(1) + '% · spread ' + (sn.spread_mv || '?') + ' mV</div>'
            + '</div>';
    }
    html += '</div></details>';

    panel.innerHTML = html;
  } catch (e) {
    console.warn('renderSnapshots error:', e);
    panel.innerHTML = '<div style="color:var(--danger);text-align:center;font-size:12px;">Chyba načítání snapshotů: ' + e.message + '</div>';
  }
}

// v4.3.0 NEW: filter chips state
let _eventFilter = 'all';
let _lastEvents = [];

function setupEventFilters() {
  const wrap = document.getElementById('eventFilters');
  if (!wrap || wrap.dataset.bound) return;
  wrap.dataset.bound = '1';
  wrap.querySelectorAll('.ef-chip').forEach(b => {
    b.addEventListener('click', () => {
      wrap.querySelectorAll('.ef-chip').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      _eventFilter = b.dataset.filter;
      renderEvents(_lastEvents);
    });
  });
}

function renderEvents(events) {
  setupEventFilters();
  _lastEvents = events;
  const list = document.getElementById('eventsList');
  if (!list) return;

  // v4.3.0: filter
  let filtered = events;
  if (_eventFilter && _eventFilter !== 'all') {
    const types = _eventFilter.split(',');
    filtered = events.filter(e => types.includes(e.type));
  }

  if (!filtered.length) {
    const msg = events.length
      ? 'V této kategorii žádné události. Zkus jiný filtr.'
      : 'Zatím žádné události.';
    list.innerHTML = '<div style="padding: 20px; color: var(--text-muted); text-align: center;">' + msg + '</div>';
    return;
  }
  const html = filtered.map(e => {
    let details = '';
    if (e.type === 'state_change') details = (stateNames[e.from_state] || e.from_state) + ' → ' + (stateNames[e.to_state] || e.to_state) + '  ·  ' + (e.reason || '');
    else if (e.type === 'heater_command') details = (e.success ? '✓' : '✗') + ' topení ' + (e.target ? 'ON' : 'OFF') + '  ·  ' + (e.reason || '');
    else if (e.type === 'web_command') details = e.target + ' = ' + e.value + '  (' + (e.success ? 'OK' : 'selhalo') + ')';
    else if (e.type === 'scene') details = 'scéna: ' + e.name + (e.source ? ' (' + e.source + ')' : '');
    else if (e.type === 'override') details = 'override ' + (e.enabled ? 'ZAP' : 'VYP') + '  ·  ' + (e.reason || '');
    else if (e.type === 'cleaning_start') details = 'spuštěn ' + e.hours + 'h program';
    else if (e.type === 'cleaning_stop') details = 'zastaveno po ' + (e.elapsed_hours || '?') + 'h · ' + (e.reason || '');
    else if (e.type === 'schedule_global') details = 'plán ' + (e.enabled ? 'ZAPNUT' : 'VYPNUT');
    else if (e.type === 'schedule_rule') details = 'pravidlo "' + e.name + '" ' + (e.enabled ? 'ZAP' : 'VYP');
    else if (e.type === 'preshower_start') details = '🛁 příprava na ' + (e.target_time_iso || '?') + ' · cíl ' + e.target_temp + '°C · ohřev ' + Math.round(e.predicted_heating_min || 0) + ' min';
    else if (e.type === 'preshower_ready') details = '✓ vířivka připravena · voda ' + (e.water_temp || '?') + '°C';
    else if (e.type === 'preshower_end') details = 'ukončeno · stav: ' + (e.state || '?');
    else details = JSON.stringify(e);
    return '<div class="evt-row"><div class="dec-time">' + fmtTime(e.ts) + '</div><div><span class="evt-type ' + e.type + '">' + e.type + '</span></div><div class="evt-details">' + details + '</div></div>';
  }).join('');
  list.innerHTML = html;
}

// ===== v3.6 NEW: Schedule renderer =====
const sceneLabels = {
  'gentle': 'Mírný',
  'solar_auto': 'Solar auto',
  'heat_now': 'Ohřát hned',
  'off': 'Vypnout',
};
const dayLabelsOrdered = ['Po', 'Út', 'St', 'Čt', 'Pá', 'So', 'Ne'];

function renderSchedule(data) {
  const wrap = document.getElementById('scheduleToggleWrap');
  const status = document.getElementById('scheduleStatus');
  const empty = document.getElementById('scheduleEmpty');
  const rulesEl = document.getElementById('scheduleRules');

  if (!data.enabled) {
    wrap.style.display = 'none';
    status.style.display = 'none';
    empty.style.display = 'block';
    rulesEl.innerHTML = '';
    return;
  }

  wrap.style.display = 'inline-flex';
  empty.style.display = 'none';
  status.style.display = 'block';

  const toggle = document.getElementById('scheduleGlobalToggle');
  toggle.checked = data.global_enabled;
  toggle.onchange = async () => {
    await apiPost('/api/schedule/global', {enabled: toggle.checked});
  };

  // Status card - next trigger
  const next = data.next_trigger;
  let statusHtml;
  if (data.global_enabled) {
    if (next) {
      const inMin = next.in_minutes;
      let when;
      if (inMin < 60) when = 'za ' + inMin + ' min';
      else if (inMin < 1440) when = 'za ' + Math.round(inMin / 60) + ' h';
      else when = 'za ' + Math.round(inMin / 1440) + ' dní';
      statusHtml = '<div style="font-family: var(--mono); font-size: 11px; color: var(--text-muted); letter-spacing: 0.5px;">DALŠÍ TRIGGER ' + when + '</div>' +
        '<div style="font-size: 16px; font-weight: 700; margin-top: 4px;">' + next.name + ' <span class="schedule-rule-scene ' + next.scene + '">' + (sceneLabels[next.scene] || next.scene) + '</span></div>' +
        '<div style="font-family: var(--mono); font-size: 11px; color: var(--text-muted); margin-top: 2px;">' + next.time + '</div>';
    } else {
      statusHtml = '<div style="font-family: var(--mono); font-size: 11px; color: var(--text-muted);">Žádná aktivní pravidla</div>';
    }
  } else {
    statusHtml = '<div style="font-family: var(--mono); font-size: 11px; color: var(--warning); letter-spacing: 0.5px;">⏸ PLÁNOVAČ POZASTAVEN</div>' +
      '<div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Pravidla se neexecutují, dokud master switch není zapnut.</div>';
  }
  status.innerHTML = statusHtml;

  // Rules list
  if (!data.rules.length) {
    rulesEl.innerHTML = '<div style="padding: 20px; color: var(--text-muted); text-align: center;">Žádná pravidla.</div>';
    return;
  }
  rulesEl.innerHTML = data.rules.map((r, idx) => {
    // Days vizualizace - tečka pro každý den
    const dayDots = [0,1,2,3,4,5,6].map(d => {
      const active = r.days.includes(d);
      return '<span style="color: ' + (active ? 'var(--primary)' : 'var(--text-dim)') + '; font-weight: ' + (active ? '700' : '400') + ';">' + dayLabelsOrdered[d] + '</span>';
    }).join(' ');
    const lastTrig = r.last_triggered ? ' · naposledy ' + fmtAge(Date.now()/1000 - r.last_triggered) + ' zpět' : '';
    return '<div class="schedule-rule ' + (r.enabled ? '' : 'disabled') + '">' +
      '<div>' +
        '<div class="schedule-rule-name">' + r.name +
          ' <span class="schedule-rule-scene ' + r.scene + '">' + (sceneLabels[r.scene] || r.scene) + '</span>' +
          (r.target_temp_c ? ' <span style="font-size: 10px; color: var(--text-muted); font-family: var(--mono);">' + r.target_temp_c + '°C</span>' : '') +
        '</div>' +
        '<div class="schedule-rule-detail">' + r.time + ' · ' + dayDots + lastTrig + '</div>' +
      '</div>' +
      '<label class="schedule-toggle">' +
        '<input type="checkbox" ' + (r.enabled ? 'checked' : '') + ' data-rule-idx="' + idx + '">' +
        '<span class="schedule-toggle-slider"></span>' +
      '</label>' +
      '</div>';
  }).join('');

  // Wire up rule toggles
  rulesEl.querySelectorAll('input[data-rule-idx]').forEach(cb => {
    cb.onchange = async () => {
      const idx = parseInt(cb.dataset.ruleIdx);
      await apiPost('/api/schedule/rule', {rule_index: idx, enabled: cb.checked});
    };
  });
}

// ===== v3.6 NEW: Spot price renderer =====
let spotChart = null;

function renderSpot(sp) {
  const card = document.getElementById('spotCurrentCard');
  const priceEl = document.getElementById('spotPriceNow');
  const reasonEl = document.getElementById('spotPriceReason');
  const statsEl = document.getElementById('spotStats');
  const bestEl = document.getElementById('bestHoursList');

  if (!sp.today_prices_kc || !sp.today_prices_kc.length) {
    card.className = 'plan-card unknown';
    priceEl.textContent = '—';
    reasonEl.textContent = sp.stale ? 'OTE-CR data nejsou dostupná' : 'načítám OTE-CR…';
    statsEl.innerHTML = '';
    bestEl.innerHTML = '<div style="padding: 20px; color: var(--text-muted); text-align: center;">Bez dat z OTE-CR</div>';
    if (spotChart) { spotChart.destroy(); spotChart = null; }
    return;
  }

  // v4.1.2 FIX: defaultne zobrazit ciste spot ceny (bez distrib. poplatku, ten se uctuje paušálem mesicne)
  const showFee = localStorage.getItem('spot_show_fee') === 'true';
  const now = showFee ? sp.current_price_kc : (sp.current_price_kc_clean ?? sp.current_price_kc);
  const prices = showFee ? sp.today_prices_kc : (sp.today_prices_kc_clean || sp.today_prices_kc);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const avgP = prices.reduce((a,b) => a+b, 0) / prices.length;

  let strat = 'unknown';
  if (now != null) {
    if (now <= avgP * 0.7) strat = 'aggressive';
    else if (now <= avgP) strat = 'normal';
    else if (now <= avgP * 1.3) strat = 'conservative';
    else strat = 'survive';
  }
  card.className = 'plan-card ' + strat;
  priceEl.textContent = now != null ? now.toFixed(2) + ' Kč' : '—';
  let reason;
  if (now == null) reason = 'mimo dostupné hodiny';
  else if (now <= avgP * 0.7) reason = '✓ levně - ideální čas pro spotřebiče';
  else if (now <= avgP) reason = 'pod průměrem dne';
  else if (now <= avgP * 1.3) reason = 'nad průměrem - pokud možno počkat';
  else reason = '⚠ drahé - šetřit, zapnout jen nutné';
  // v4.1.2 NEW: indikace ze cena je ciste spot vs s distribuci
  const feeNote = showFee
    ? ' · vč. distribuce ' + sp.fee_kc_per_kwh + ' Kč/kWh'
    : ' · čistý spot (bez distribuce)';
  reasonEl.innerHTML = reason +
    '<a href="#" onclick="toggleSpotFee(event)" style="margin-left: 12px; font-size: 11px; color: var(--primary); font-family: var(--mono);">' +
    feeNote + ' [přepnout]</a>';

  statsEl.innerHTML =
    '<div><div class="plan-stat-label">Min dnes</div><div class="plan-stat-value">' + minP.toFixed(2) + '<span class="plan-stat-unit"> Kč</span></div></div>' +
    '<div><div class="plan-stat-label">Max dnes</div><div class="plan-stat-value">' + maxP.toFixed(2) + '<span class="plan-stat-unit"> Kč</span></div></div>' +
    '<div><div class="plan-stat-label">Průměr</div><div class="plan-stat-value">' + avgP.toFixed(2) + '<span class="plan-stat-unit"> Kč</span></div></div>' +
    '<div><div class="plan-stat-label">EUR/CZK</div><div class="plan-stat-value">' + sp.eur_to_kc.toFixed(2) + '</div></div>';

  // Chart
  const labels = prices.map((_, i) => String(i).padStart(2, '0') + ':00');
  const nowHour = new Date().getHours();
  const colors = prices.map((p, i) => {
    if (i === nowHour) return '#2563eb';
    if (sp.best_hours_today.includes(i)) return '#16a34a';
    if (p >= avgP * 1.3) return '#dc2626';
    if (p >= avgP) return '#ea580c';
    return '#94a3b8';
  });
  const data = {
    labels,
    datasets: [{
      label: 'Cena (Kč/kWh)',
      data: prices,
      backgroundColor: colors,
      borderRadius: 4,
    }]
  };
  const opts = {
    responsive: true, maintainAspectRatio: false,
    scales: {
      x: { ticks: { color: '#64748b', font: { size: 9 }, maxTicksLimit: 12 }, grid: { display: false } },
      y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#f1f5f9' }, title: { display: true, text: 'Kč/kWh', color: '#64748b' } },
    },
    plugins: { legend: { display: false } }
  };
  if (spotChart) { spotChart.data = data; spotChart.update('none'); }
  else { spotChart = new Chart(document.getElementById('spotChart'), { type: 'bar', data, options: opts }); }

  // Best hours list
  const best = sp.best_hours_today || [];
  if (!best.length) {
    bestEl.innerHTML = '<div style="padding: 16px; color: var(--text-muted); text-align: center; font-family: var(--mono); font-size: 11px;">Žádné nadcházející levné hodiny dnes</div>';
  } else {
    bestEl.innerHTML = best.map(h => {
      const price = prices[h];
      const widthPct = ((price - minP) / Math.max(0.01, maxP - minP)) * 100;
      const cls = price <= avgP * 0.7 ? 'cheap' : price <= avgP ? 'medium' : 'expensive';
      const isNow = h === nowHour;
      return '<div class="spot-bar-row best' + (isNow ? ' now' : '') + '">' +
        '<span class="spot-hour">' + String(h).padStart(2, '0') + ':00</span>' +
        '<div class="spot-bar-wrap"><div class="spot-bar-fill ' + cls + '" style="width: ' + widthPct + '%"></div></div>' +
        '<span class="spot-price-val">' + price.toFixed(2) + ' Kč</span>' +
        '</div>';
    }).join('');
  }
}

// v4.1.2 FIX: Toggle mezi cistou spot cenou a s distribuci
function toggleSpotFee(ev) {
  if (ev) ev.preventDefault();
  const cur = localStorage.getItem('spot_show_fee') === 'true';
  localStorage.setItem('spot_show_fee', String(!cur));
  refresh();
}

// ===== v4.0 NEW: Insights renderer =====
function renderInsights(data) {
  const container = document.getElementById('insightsContainer');
  if (!container) return;
  if (!data.insights || data.insights.length === 0) {
    container.style.display = 'none';
    return;
  }
  container.style.display = 'block';
  container.innerHTML = data.insights.map(i =>
    '<div class="insight-card severity-' + i.severity + '">' +
    '<div class="insight-icon">' + i.icon + '</div>' +
    '<div>' +
    '<div class="insight-title">' + i.title + '</div>' +
    '<div class="insight-detail">' + i.detail + '</div>' +
    '</div></div>'
  ).join('');
}

// ===== v4.1 NEW: Digest renderer =====
function renderDigest(data) {
  const container = document.getElementById('digestContent');
  if (!container) return;
  if (!data.configured) {
    container.innerHTML = '<div style="padding: 28px; text-align: center; color: var(--text-muted);">Digest generator není nakonfigurovaný.</div>';
    return;
  }
  if (!data.digest) {
    container.innerHTML =
      '<div style="padding: 28px; text-align: center; background: var(--surface); border-radius: 12px; border: 1px dashed var(--border);">' +
      '<div style="font-size: 36px; margin-bottom: 8px;">📊</div>' +
      '<div style="font-family: var(--mono); font-size: 12px; color: var(--text-muted); line-height: 1.6;">' +
      (data.message || 'Žádný digest zatím.') +
      '<br>Zkus tlačítko <strong>"Generovat teď"</strong> jakmile máš pár dní dat.' +
      '</div></div>';
    return;
  }
  const d = data.digest;
  const fmtDelta = (v) => {
    if (v == null) return '';
    const sign = v >= 0 ? '+' : '';
    const cls = v >= 0 ? 'up' : 'down';
    return '<div class="digest-stat-delta ' + cls + '">' + sign + v.toFixed(0) + '% vs týden zpět</div>';
  };
  container.innerHTML =
    '<div class="digest-card">' +
    '<div class="digest-header">📊 Týdenní souhrn</div>' +
    '<div class="digest-period">' + d.week_start + ' – ' + d.week_end + '</div>' +
    '<div class="digest-stats">' +
      '<div class="digest-stat"><div class="digest-stat-label">☀ FV výroba</div>' +
      '<div class="digest-stat-value">' + d.pv_total_kwh.toFixed(0) + '<span style="font-size:13px;color:var(--text-muted)"> kWh</span></div>' +
      '<div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted);">⌀ ' + d.pv_avg_per_day.toFixed(1) + ' kWh/den</div>' +
      fmtDelta(d.delta_pv_pct) +
      '</div>' +
      '<div class="digest-stat"><div class="digest-stat-label">🏠 Spotřeba</div>' +
      '<div class="digest-stat-value">' + d.consumption_total_kwh.toFixed(0) + '<span style="font-size:13px;color:var(--text-muted)"> kWh</span></div>' +
      '<div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted);">⌀ ' + d.consumption_avg_per_day.toFixed(1) + ' kWh/den</div>' +
      fmtDelta(d.delta_cons_pct) +
      '</div>' +
      '<div class="digest-stat"><div class="digest-stat-label">⚡ Soběstačnost</div>' +
      '<div class="digest-stat-value">' + d.self_sufficiency_pct.toFixed(0) + '<span style="font-size:13px;color:var(--text-muted)"> %</span></div>' +
      '</div>' +
      '<div class="digest-stat"><div class="digest-stat-label">🌞 Slunečné dny</div>' +
      '<div class="digest-stat-value">' + d.sunny_days + '<span style="font-size:13px;color:var(--text-muted)"> / 7</span></div>' +
      '</div>' +
      '<div class="digest-stat"><div class="digest-stat-label">🛁 Vířivka</div>' +
      '<div class="digest-stat-value">' + d.heating_sessions + '<span style="font-size:13px;color:var(--text-muted)">× ohřev</span></div>' +
      '<div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted);">' + (d.heating_total_minutes / 60).toFixed(1) + ' h celkem</div>' +
      '</div>' +
      (d.spike_count > 0 ?
        '<div class="digest-stat"><div class="digest-stat-label">⚡ Spike</div>' +
        '<div class="digest-stat-value">' + d.spike_count + '<span style="font-size:13px;color:var(--text-muted)">×</span></div>' +
        '</div>' : '') +
    '</div>' +
    '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 16px;">' +
      '<div style="padding: 10px 12px; background: #ecfdf5; border-radius: 8px; border-left: 3px solid var(--success);">' +
      '<div style="font-family: var(--mono); font-size: 9px; color: var(--success); letter-spacing: 1px; text-transform: uppercase; font-weight: 700; margin-bottom: 3px;">📈 Top den</div>' +
      '<div style="font-family: var(--mono); font-size: 12px; font-weight: 700;">' + d.pv_best_day + '</div>' +
      '<div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted);">' + d.pv_best_kwh.toFixed(1) + ' kWh</div>' +
      '</div>' +
      '<div style="padding: 10px 12px; background: var(--warning-soft); border-radius: 8px; border-left: 3px solid var(--warning);">' +
      '<div style="font-family: var(--mono); font-size: 9px; color: var(--warning); letter-spacing: 1px; text-transform: uppercase; font-weight: 700; margin-bottom: 3px;">📉 Worst den</div>' +
      '<div style="font-family: var(--mono); font-size: 12px; font-weight: 700;">' + d.pv_worst_day + '</div>' +
      '<div style="font-family: var(--mono); font-size: 10px; color: var(--text-muted);">' + d.pv_worst_kwh.toFixed(1) + ' kWh</div>' +
      '</div>' +
    '</div>' +
    (d.insights_text && d.insights_text.length ?
      '<div class="digest-insights">' +
      '<div class="digest-insights-title">💡 Postřehy</div>' +
      '<ul class="digest-insights-list">' + d.insights_text.map(t => '<li>' + t + '</li>').join('') + '</ul>' +
      '</div>' : '') +
    '</div>';
}

async function generateDigestNow() {
  if (!confirm('Vygenerovat týdenní digest teď? (jinak se generuje automaticky každou neděli 18:00)')) return;
  try {
    const r = await fetch('/api/digest/generate', {
      method: 'POST',
      headers: getAuthHeaders(),
    });
    if (!r.ok) {
      const txt = await r.text();
      toast('Chyba: ' + txt, 'error');
      return;
    }
    const data = await r.json();
    renderDigest({digest: data.digest, configured: true, markdown: data.markdown});
  } catch (e) {
    toast('Chyba: ' + e.message, 'error');
  }
}

// ===== v4.1 NEW: User management =====
// ===== v4.1 / v4.1.3 User management =====
async function loadUsers() {
  const container = document.getElementById('usersList');
  if (!container) return;
  try {
    const r = await fetch('/api/users', {headers: getAuthHeaders()});
    if (r.status === 401 || r.status === 403) {
      container.innerHTML = '<div style="padding: 28px; text-align: center; color: var(--text-muted);">Vyžaduje roli <strong>owner</strong>. Aktuálně jsi přihlášen jako <strong>' + ((_currentUser && _currentUser.role) || 'guest') + '</strong>.</div>';
      return;
    }
    const data = await r.json();
    if (!data.users || data.users.length === 0) {
      container.innerHTML = '<div style="padding: 28px; text-align: center; color: var(--text-muted);">Žádní uživatelé. Klepnutím na "+ Přidat" založíš prvního.</div>';
      return;
    }

    const roleColor = {owner: 'var(--danger)', family: 'var(--success)', guest: 'var(--text-muted)'};
    const roleLabel = {owner: 'OWNER', family: 'FAMILY', guest: 'GUEST'};
    const roleDesc = {
      owner: 'plný přístup vč. správy uživatelů',
      family: 'ovládá vířivku, vidí vše',
      guest: 'jen čtení, žádné akce'
    };

    container.innerHTML = data.users.map(u => {
      const lastLogin = u.last_login
        ? new Date(u.last_login * 1000).toLocaleString('cs-CZ', {dateStyle: 'short', timeStyle: 'short'})
        : 'nikdy';
      const created = new Date(u.created_at * 1000).toLocaleDateString('cs-CZ');
      const isMe = data.current_user === u.name;
      const isOwner = u.role === 'owner';
      const ownerCount = data.users.filter(x => x.role === 'owner').length;
      const isLastOwner = isOwner && ownerCount === 1;

      // Role select s aktualnim role a dostupnymi roles
      const roleOptions = (data.valid_roles || ['owner','family','guest']).map(r =>
        '<option value="' + r + '"' + (r === u.role ? ' selected' : '') + '>' + roleLabel[r] + '</option>'
      ).join('');

      return '<div class="user-card">' +
        '<div class="user-card-header">' +
        '<div class="user-card-name">' +
        '<span class="user-name">' + u.name + '</span>' +
        (isMe ? '<span class="user-badge-me">TY</span>' : '') +
        '<span class="user-role-badge" style="background: ' + roleColor[u.role] + '20; color: ' + roleColor[u.role] + ';">' + roleLabel[u.role] + '</span>' +
        '</div>' +
        '<div class="user-card-actions">' +
        '<button class="btn-mini" onclick="regenerateToken(\'' + u.name + '\', ' + isMe + ')" title="Vygenerovat nový token">🔑</button>' +
        (isLastOwner || isMe
          ? '<button class="btn-mini" disabled title="' + (isMe ? 'Nelze smazat sebe' : 'Nelze smazat posledního ownera') + '">🗑</button>'
          : '<button class="btn-mini btn-mini-danger" onclick="deleteUser(\'' + u.name + '\')" title="Smazat uživatele">🗑</button>'
        ) +
        '</div>' +
        '</div>' +
        '<div class="user-card-info">' +
        '<div><span class="info-key">Role:</span> ' +
        '<select class="user-role-select" onchange="changeRole(\'' + u.name + '\', this.value, ' + isLastOwner + ', ' + isMe + ', \'' + u.role + '\')">' +
        roleOptions +
        '</select> <span class="info-note">(' + roleDesc[u.role] + ')</span></div>' +
        '<div><span class="info-key">Vytvořen:</span> ' + created + '</div>' +
        '<div><span class="info-key">Poslední login:</span> ' + lastLogin + '</div>' +
        '</div>' +
        '</div>';
    }).join('');
  } catch (e) {
    container.innerHTML = '<div style="padding: 20px; color: var(--danger);">Chyba: ' + e.message + '</div>';
  }
}

function showCreateUserDialog() {
  document.getElementById('createUserModal').style.display = 'flex';
  document.getElementById('newUserName').value = '';
  document.getElementById('newUserToken').value = '';
  document.getElementById('newUserRole').value = 'family';  // default family
  document.getElementById('createUserError').style.display = 'none';
  // Auto-generuj token aby user nemusel klikat
  generateNewUserToken();
  setTimeout(() => document.getElementById('newUserName').focus(), 100);
}
function hideCreateUserDialog() {
  document.getElementById('createUserModal').style.display = 'none';
}

function generateNewUserToken() {
  const arr = new Uint8Array(32);
  crypto.getRandomValues(arr);
  const b64 = btoa(String.fromCharCode.apply(null, arr))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  document.getElementById('newUserToken').value = b64;
}

// v4.1.3 NEW: krásnější dialog pro zobrazení tokenu (s copy buttonem)
function showTokenDialog(title, name, role, token, extra) {
  const dlg = document.getElementById('tokenShowModal');
  document.getElementById('tokenShowTitle').textContent = title;
  document.getElementById('tokenShowName').textContent = name;
  document.getElementById('tokenShowRole').textContent = role;
  document.getElementById('tokenShowValue').textContent = token;
  document.getElementById('tokenShowExtra').innerHTML = extra || '';
  dlg.style.display = 'flex';
}
function hideTokenDialog() {
  document.getElementById('tokenShowModal').style.display = 'none';
}
async function copyTokenToClipboard() {
  const token = document.getElementById('tokenShowValue').textContent;
  try {
    await navigator.clipboard.writeText(token);
    const btn = document.getElementById('tokenCopyBtn');
    const orig = btn.textContent;
    btn.textContent = '✓ Zkopírováno!';
    btn.style.background = 'var(--success)';
    setTimeout(() => {
      btn.textContent = orig;
      btn.style.background = '';
    }, 2000);
  } catch (e) {
    // Fallback: select text
    const range = document.createRange();
    range.selectNode(document.getElementById('tokenShowValue'));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    toast('Token vybrán - zkopíruj ručně', 'info');
  }
}

async function createUser() {
  const name = document.getElementById('newUserName').value.trim();
  const role = document.getElementById('newUserRole').value;
  const token = document.getElementById('newUserToken').value.trim();
  const err = document.getElementById('createUserError');
  err.style.display = 'none';

  if (!name) {
    err.textContent = 'Vyplň jméno'; err.style.display = 'block'; return;
  }
  if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
    err.textContent = 'Jméno: jen písmena, čísla, _ a - (max 32)'; err.style.display = 'block'; return;
  }
  if (token.length < 8) {
    err.textContent = 'Token musí mít aspoň 8 znaků'; err.style.display = 'block'; return;
  }

  try {
    const r = await fetch('/api/users', {
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({name, role, token})
    });
    if (!r.ok) {
      let errMsg;
      try {
        const errData = await r.json();
        errMsg = typeof errData.detail === 'string' ? errData.detail :
                 (Array.isArray(errData.detail) ? errData.detail.map(d => d.msg).join(', ') :
                  JSON.stringify(errData));
      } catch {
        errMsg = await r.text();
      }
      err.textContent = errMsg;
      err.style.display = 'block';
      return;
    }
    hideCreateUserDialog();
    loadUsers();
    showTokenDialog(
      '✓ Uživatel vytvořen',
      name, role, token,
      '<strong>Předej tento token uživateli</strong> (e-mail, Signal, SMS, ...). ' +
      'Token už nikde nenajdeš - SolarGuard ho ukládá jen jako hash.'
    );
  } catch (e) {
    err.textContent = e.message;
    err.style.display = 'block';
  }
}

async function changeRole(name, newRole, isLastOwner, isMe, oldRole) {
  // Validace na klientu
  if (isLastOwner && newRole !== 'owner') {
    toast('Nelze degradovat posledního ownera', 'warning');
    loadUsers(); return;
  }
  if (isMe && newRole !== 'owner') {
    toast('Nelze degradovat sám sebe', 'warning');
    loadUsers(); return;
  }
  if (newRole === oldRole) return;
  if (!confirm('Změnit roli uživatele ' + name + ' z ' + oldRole.toUpperCase() + ' na ' + newRole.toUpperCase() + '?')) {
    loadUsers(); return;
  }
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(name), {
      method: 'PUT',
      headers: getAuthHeaders(),
      body: JSON.stringify({role: newRole})
    });
    if (!r.ok) {
      const txt = await r.text();
      toast('Chyba: ' + txt, 'error');
      loadUsers();
      return;
    }
    loadUsers();
  } catch (e) {
    toast('Chyba: ' + e.message, 'error');
    loadUsers();
  }
}

async function regenerateToken(name, isMe) {
  let confirmMsg = 'Vygenerovat nový token pro ' + name + '?\n\nStarý token okamžitě přestane fungovat - daný uživatel se odhlásí.';
  if (isMe) {
    confirmMsg += '\n\n⚠ POZOR: Regeneruješ vlastní token! Po této akci se MUSÍŠ znovu přihlásit s novým tokenem.';
  }
  if (!confirm(confirmMsg)) return;
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(name) + '/regenerate-token', {
      method: 'POST', headers: getAuthHeaders()
    });
    if (!r.ok) { toast('Chyba: ' + (await r.text()), 'error'); return; }
    const data = await r.json();

    // Najdi roli uzivatele
    const usersResp = await fetch('/api/users', {headers: getAuthHeaders()});
    const usersData = await usersResp.json();
    const u = usersData.users.find(x => x.name === name);
    const role = u ? u.role : '?';

    let extra = '<strong>Předej tento token uživateli.</strong>';
    if (data.self_regen) {
      extra += '<br><br>⚠ <strong style="color: var(--danger);">Tohle je tvůj vlastní token!</strong> ' +
               'Po zavření dialogu budeš odhlášen. Pak se přihlásíš tímto novým tokenem.';
    }

    showTokenDialog(
      '🔑 Nový token pro ' + name,
      name, role, data.new_token,
      extra
    );

    // Pokud sama sobě - po close dialogu logout
    if (data.self_regen) {
      document.getElementById('tokenShowCloseBtn').onclick = function() {
        hideTokenDialog();
        // Auto-login s novým tokenem
        _authToken = data.new_token;
        localStorage.setItem('solarguard_token', data.new_token);
        location.reload();
      };
    }

    loadUsers();
  } catch (e) { toast('Chyba: ' + e.message, 'error'); }
}

async function deleteUser(name) {
  if (!confirm('Smazat uživatele "' + name + '"?\n\nToken okamžitě přestane fungovat.')) return;
  try {
    const r = await fetch('/api/users/' + encodeURIComponent(name), {
      method: 'DELETE', headers: getAuthHeaders()
    });
    if (!r.ok) {
      const txt = await r.text();
      toast('Chyba: ' + txt, 'error');
      return;
    }
    loadUsers();
  } catch (e) { toast('Chyba: ' + e.message, 'error'); }
}

// v3.9 NEW: Init s auth check
(async function init() {
  // Enter v login inputu
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && document.getElementById('loginOverlay').style.display !== 'none') {
      doLogin();
    }
  });
  const ok = await checkAuth();
  if (ok) {
    // v4.3.0 NEW: PWA shortcut deep link - kdyz user otevre /?tab=control, prepni
    if (_initTab !== 'overview') {
      switchTab(_initTab);
    } else {
      refresh();
    }
    function getRefreshInterval() {
      if (activeTab === 'control' || activeTab === 'flow') return 2000;
      if (activeTab === 'heatpump') return 3000;  // v4.3.0 NEW
      if (activeTab === 'stats' || activeTab === 'spot' || activeTab === 'digest' || activeTab === 'users') return 15000;
      return 5000;
    }
    let _refreshTimer = setInterval(refresh, getRefreshInterval());
    let _lastTab = activeTab;
    setInterval(() => {
      if (activeTab !== _lastTab) {
        clearInterval(_refreshTimer);
        _refreshTimer = setInterval(refresh, getRefreshInterval());
        _lastTab = activeTab;
        refresh();
      }
    }, 500);
  }
})();
</script>
</body>
</html>
"""
