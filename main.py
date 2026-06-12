"""SolarGuard v3.7 - hlavni entry point + EnergyTracker + Cleaning + Appliances."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time

import uvicorn
import yaml

from solarguard.state import SystemContext, SystemState
from solarguard.sources.victron import VictronMQTT
from solarguard.sources.loxone import LoxoneSource
from solarguard.sources.openmeteo import OpenMeteoSource
from solarguard.sources.ote import OteSource
from solarguard.sources.h66_mqtt import H66MqttSource
from solarguard.devices.spa import SpaController
from solarguard.devices.heatpump import HeatPumpController
from solarguard.engine.decision import DecisionEngine, SpaConfig, Decision
from solarguard.engine.heatpump_engine import HeatPumpEngine, HeatPumpConfig, HeatPumpState
from solarguard.engine.planner import Planner, PlannerConfig
from solarguard.engine.cleaning import CleaningManager
from solarguard.engine.appliances import ApplianceEvaluator, profiles_from_config
from solarguard.engine.appliance_learning import ApplianceLearningManager
from solarguard.engine.scheduler import Scheduler, parse_rules_from_config
from solarguard.engine.heating_curve import HeatingCurveTracker
from solarguard.engine.preshower import PreShowerManager
from solarguard.storage.logger import EventLogger, setup_logging
from solarguard.auth import init_auth, AuthConfig
from solarguard.insights.anomaly import AnomalyDetector
from solarguard.insights.digest import DigestGenerator
from solarguard.web import (
    create_app, record_tick, record_event,
    set_spa_controller, set_cleaning_manager, set_appliance_evaluator,
    set_heatpump_controller, set_heatpump_engine,
    set_learning_manager,
)


VERSION = "4.4.0"


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class SolarGuard:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ctx = SystemContext()
        # NEW v3.2: scena a override teploty (mirny rezim, manualni +/- v UI)
        self.ctx.target_temp_override = None  # None = pouzij cfg.target_temp_c
        self.ctx.current_scene = "solar_auto"  # solar_auto / gentle / heat_now
        self._shutdown = asyncio.Event()

        setup_logging(
            cfg["logging"]["log_dir"],
            level=cfg["general"].get("log_level", "INFO"),
            app_file=cfg["logging"]["app_file"],
        )
        self.log = logging.getLogger("main")
        self.events = EventLogger(cfg["logging"]["log_dir"], cfg["logging"]["events_file"])

        # v3.9/v4.1 NEW: API auth (volitelne, multi-user v4.1)
        from pathlib import Path
        api_cfg = cfg.get("api", {})
        users_file = Path(cfg["logging"]["log_dir"]) / "users.json"
        init_auth(AuthConfig(
            enabled=api_cfg.get("auth_enabled", False),
            legacy_token=api_cfg.get("token"),
            users_file=users_file,
            auth_read_too=api_cfg.get("auth_read_too", False),
            allow_localhost=api_cfg.get("allow_localhost", True),
        ))

        vc = cfg["victron"]
        self.victron = VictronMQTT(
            host=vc["host"], port=vc["port"], portal_id=vc["portal_id"],
            context=self.ctx,
            username=vc.get("username"), password=vc.get("password"),
            keepalive_interval=vc.get("keepalive_interval_sec", 30),
        )

        self.loxone = None
        if "loxone" in cfg:
            lx = cfg["loxone"]
            self.loxone = LoxoneSource(
                host=lx["host"], username=lx["username"], password=lx["password"],
                context=self.ctx, poll_interval=lx.get("poll_interval_sec", 60),
            )

        self.openmeteo = None
        if "openmeteo" in cfg:
            om = cfg["openmeteo"]
            self.openmeteo = OpenMeteoSource(
                latitude=om["latitude"], longitude=om["longitude"],
                context=self.ctx,
                installed_kwp=om.get("installed_kwp", 11.8),
                efficiency=om.get("efficiency", 0.75),
                refresh_interval=om.get("refresh_interval_sec", 14400),
            )

        # v3.6 NEW: OTE-CR spotove ceny
        self.ote = None
        if cfg.get("spot_pricing", {}).get("enabled", False):
            sp = cfg["spot_pricing"]
            self.ote = OteSource(
                context=self.ctx,
                eur_to_kc=sp.get("eur_to_kc_fallback", 25.0),
                fee_kc_per_kwh=sp.get("fee_kc_per_kwh", 1.5),
                refresh_interval=sp.get("refresh_interval_sec", 3600),
            )

        self.planner = None
        if "planner" in cfg:
            pl = cfg["planner"]
            self.planner = Planner(self.ctx, PlannerConfig(
                battery_kwh_total=pl.get("battery_kwh_total", 32.0),
                battery_reserve_pct=pl.get("battery_reserve_pct", 20.0),
                battery_round_trip_eff=pl.get("battery_round_trip_eff", 0.9),
                baseline_consumption_kwh=pl.get("baseline_consumption_kwh", 12.0),
                aggressive_threshold_kwh=pl.get("aggressive_threshold_kwh", 15.0),
                normal_threshold_kwh=pl.get("normal_threshold_kwh", 8.0),
                conservative_threshold_kwh=pl.get("conservative_threshold_kwh", 3.0),
                surplus_on_aggressive_w=pl.get("surplus_on_aggressive_w", 300),
                surplus_on_normal_w=pl.get("surplus_on_normal_w", 1500),
                surplus_on_conservative_w=pl.get("surplus_on_conservative_w", 2500),
                surplus_off_ratio=pl.get("surplus_off_ratio", 0.5),
                refresh_interval_sec=pl.get("refresh_interval_sec", 300),
            ))

        sc = cfg["spa"]
        self.spa = SpaController(
            host=sc["host"], context=self.ctx,
            retry_count=sc.get("command_retry_count", 3),
            verify_delay_sec=sc.get("command_verify_delay_sec", 5),
            dry_run=cfg["general"].get("dry_run", True),
        )

        self.engine = DecisionEngine(SpaConfig(
            surplus_on_w=sc["surplus_on_w"], surplus_off_w=sc["surplus_off_w"],
            min_soc_pct=sc["min_soc_pct"], target_temp_c=sc["target_temp_c"],
            max_temp_c=sc["max_temp_c"], stability_window_sec=sc["stability_window_sec"],
            min_on_time_sec=sc["min_on_time_sec"], min_off_time_sec=sc["min_off_time_sec"],
            load_spike_threshold_w=sc["load_spike_threshold_w"],
            spike_cooldown_sec=sc["spike_cooldown_sec"],
            spike_safety_surplus_w=sc.get("spike_safety_surplus_w", 500),
            spike_ignore_if_battery_charging_w=sc.get("spike_ignore_if_battery_charging_w", 1000),
            min_air_temp_c=sc["min_air_temp_c"],
            wind_reduce_kmh=sc.get("wind_reduce_kmh", 25.0),
            wind_target_reduction_c=sc.get("wind_target_reduction_c", 2),
            # NEW v3.1
            battery_full_soc_pct=sc.get("battery_full_soc_pct", 90.0),
            battery_full_off_threshold_w=sc.get("battery_full_off_threshold_w", -1500),
            # NEW v3.1.1
            off_stability_window_sec=sc.get("off_stability_window_sec", 60),
            # NEW v3.4
            phase_max_continuous_w=sc.get("phase_max_continuous_w", 3500),
            spa_phase_label=sc.get("spa_phase_label", "L2"),
            night_shutdown=sc.get("night_shutdown", True),
            night_shutdown_offset_min=sc.get("night_shutdown_offset_min", 60),
            morning_resume_offset_min=sc.get("morning_resume_offset_min", 30),
            # v4.3.0 NEW: BAT-FULL kickstart
            bat_full_kickstart_enabled=sc.get("bat_full_kickstart_enabled", True),
            bat_full_kickstart_min_pv_w=sc.get("bat_full_kickstart_min_pv_w", 1000),
            bat_full_kickstart_min_load_w=sc.get("bat_full_kickstart_min_load_w", 500),
            # v4.4.0 NEW: auto-expirace heat_now
            heat_now_max_hours=sc.get("heat_now_max_hours", 8.0),
            heat_now_linger_min=sc.get("heat_now_linger_min", 120),
        ))

        self.cleaning = CleaningManager(
            self.ctx, self.spa,
            event_logger=self.events, web_record_event=record_event,
        )

        profiles = profiles_from_config(cfg)
        pl_cfg = cfg.get("planner", {})
        self.appliances = ApplianceEvaluator(
            profiles=profiles,
            soc_reserve_pct=pl_cfg.get("battery_reserve_pct", 25.0),
            battery_kwh_total=pl_cfg.get("battery_kwh_total", 32.0),
            # v4.3.0: Sjednoceni s spa configem - pri SOC nad timto je BAT-FULL
            battery_full_soc_pct=sc.get("battery_full_soc_pct", 95.0),
        )

        # v4.3.0 NEW: Appliance learning manager - sleduje aktivni cykly
        # a uci se profily spotrebicu (peak/avg/cycle_min/kwh) automaticky.
        learn_cfg = cfg.get("appliance_learning", {})
        self.learning = ApplianceLearningManager(
            context=self.ctx,
            data_dir=learn_cfg.get("data_dir", "/home/pi/solarguard/data"),
        )
        # Aplikuj naucene profily na ApplianceEvaluator (override defaults z configu)
        for app_id, profile in self.learning.get_all_profiles().items():
            for ap in self.appliances.profiles:
                if ap.id == app_id and profile.sample_count >= 1:
                    if profile.avg_peak_w > 100:
                        ap.peak_w = profile.avg_peak_w
                    if profile.avg_avg_w > 50:
                        ap.avg_w = profile.avg_avg_w
                    if profile.avg_cycle_min > 5:
                        ap.cycle_min = int(profile.avg_cycle_min)
                    if profile.detected_phase:
                        ap.phase = profile.detected_phase
                    self.log.info(f"  Applied learned profile to {app_id}: "
                                  f"peak={ap.peak_w}W, avg={ap.avg_w}W, cycle={ap.cycle_min}min")

        # v3.6 NEW: Scheduler
        self.scheduler = None
        sched_cfg = cfg.get("schedule", {})
        if sched_cfg.get("enabled", False):
            rules_raw = sched_cfg.get("rules", [])
            rules = parse_rules_from_config(rules_raw)
            self.scheduler = Scheduler(
                ctx=self.ctx,
                rules=rules,
                scene_callback=self._scheduler_trigger_scene,
                check_interval_sec=sched_cfg.get("check_interval_sec", 30),
                global_enabled=True,
            )

        self.loop_interval = cfg["general"]["loop_interval_sec"]
        self.dry_run = cfg["general"].get("dry_run", True)

        # v3.7 NEW: Heating curve tracker (uci se rychlost ohrevu)
        # v3.7.1: parametry virivky z config (Intex 28462 = 1098L, 2200W)
        sc_v3 = cfg.get("spa", {})
        self.heating_curve = HeatingCurveTracker(
            log_dir=cfg["logging"]["log_dir"],
            history_file="heating_history.jsonl",
            volume_l=sc_v3.get("tub_volume_l", 1098.0),
            heater_power_w=sc_v3.get("heater_power_w", 2200.0),
            base_efficiency=sc_v3.get("base_efficiency", 0.6),
        )

        # v3.7 NEW: Pre-shower manager (priprava virivky pred koupanim)
        self.preshower = PreShowerManager(
            context=self.ctx,
            spa_controller=self.spa,
            heating_curve_tracker=self.heating_curve,
            event_logger=self.events,
            web_record_event=record_event,
        )

        # v4.4.0: InfluxDB/Grafana podpora odstranena (nepouzivala se).
        # Obnova: git revert commitu "chore: odstraneni InfluxDB/Grafana"

        # v4.0 NEW: Anomaly detector (vede 14-day rolling window denních souhrnů)
        self.anomaly = AnomalyDetector(
            log_dir=cfg["logging"]["log_dir"],
            context=self.ctx,
            window_days=cfg.get("insights", {}).get("window_days", 14),
            min_baseline_days=cfg.get("insights", {}).get("min_baseline_days", 3),
        )

        # v4.1 NEW: Weekly digest generator
        self.digest = DigestGenerator(
            log_dir=cfg["logging"]["log_dir"],
            anomaly_detector=self.anomaly,
            config=cfg.get("digest", {}),
        )

        # Pro detekci spousteni/koncovani topeni - sledujeme zmeny heater_on
        self._last_heater_state = None

        # v4.3.0 NEW: Heat Pump (IVT AIR X 70 + Airmodul E9 pres Husdata H66)
        self.h66 = None
        self.heatpump = None
        self.heatpump_engine = None
        hp_cfg = cfg.get("heatpump", {})
        if hp_cfg.get("enabled", False):
            registers = hp_cfg.get("registers", {})
            writable = hp_cfg.get("writable", {})
            mqtt_cfg = hp_cfg.get("mqtt", {})
            self.h66 = H66MqttSource(
                mqtt_host=mqtt_cfg.get("host", "127.0.0.1"),
                mqtt_port=mqtt_cfg.get("port", 1883),
                mac_address=hp_cfg.get("mac_address", ""),
                context=self.ctx,
                registers=registers,
                writable=writable,
                username=mqtt_cfg.get("username") or None,
                password=mqtt_cfg.get("password") or None,
                keepalive_minutes=mqtt_cfg.get("keepalive_minutes", 5),
            )
            self.heatpump = HeatPumpController(
                h66_source=self.h66,
                context=self.ctx,
                default_hot_water_target_c=hp_cfg.get("default_hot_water_c", 50.0),
                boost_hot_water_target_c=hp_cfg.get("boost_hot_water_c", 60.0),
                default_room_target_c=hp_cfg.get("default_room_c", 21.0),
                boost_room_target_c=hp_cfg.get("boost_room_c", 22.5),
                cooling_room_target_c=hp_cfg.get("cooling_room_c", 24.0),
                dry_run=cfg["general"].get("dry_run", True),
            )
            self.heatpump_engine = HeatPumpEngine(HeatPumpConfig(
                enabled=True,
                boost_min_surplus_w=hp_cfg.get("boost_min_surplus_w", 2000),
                boost_min_soc_pct=hp_cfg.get("boost_min_soc_pct", 80),
                boost_min_lux=hp_cfg.get("boost_min_lux", 30000),
                boost_outdoor_max_c=hp_cfg.get("boost_outdoor_max_c", 18),
                cooling_enabled=hp_cfg.get("cooling_enabled", False),
                cooling_min_outdoor_c=hp_cfg.get("cooling_min_outdoor_c", 24),
                cooling_min_soc_pct=hp_cfg.get("cooling_min_soc_pct", 70),
                night_saving_enabled=hp_cfg.get("night_saving_enabled", True),
                night_block_aux_heater=hp_cfg.get("night_block_aux_heater", True),
                max_hot_water_target_c=hp_cfg.get("max_hot_water_c", 60),
                max_room_target_c=hp_cfg.get("max_room_c", 23),
                min_room_target_c=hp_cfg.get("min_room_c", 18),
            ))

        # v4.3.2 NEW: Seplos BMS V3.0 RS485 Modbus RTU driver
        self.seplos = None
        sep_cfg = cfg.get("seplos", {})
        if sep_cfg.get("enabled", False):
            from solarguard.sources.seplos import SeplosRS485
            self.seplos = SeplosRS485(
                port=sep_cfg["port"],
                pack_count=sep_cfg.get("pack_count", 2),
                cells_per_pack=sep_cfg.get("cells_per_pack", 16),
                context=self.ctx,
                baudrate=sep_cfg.get("baudrate", 9600),
                poll_interval_sec=sep_cfg.get("poll_interval_sec", 30),
                reg_cell_start=int(sep_cfg.get("reg_cell_start", "0x1100"), 16)
                    if isinstance(sep_cfg.get("reg_cell_start", 0x1100), str)
                    else sep_cfg.get("reg_cell_start", 0x1100),
            )

        self.app = create_app(self.ctx, cfg)
        set_spa_controller(self.spa)
        set_cleaning_manager(self.cleaning)
        set_appliance_evaluator(self.appliances)
        set_learning_manager(self.learning)
        # v4.3.0 NEW: heat pump
        if self.heatpump:
            set_heatpump_controller(self.heatpump)
            set_heatpump_engine(self.heatpump_engine)
        # v3.6 NEW
        from solarguard.web import set_scheduler
        set_scheduler(self.scheduler)
        # v3.7 NEW
        from solarguard.web import set_heating_curve, set_preshower
        set_heating_curve(self.heating_curve)
        set_preshower(self.preshower)
        # v4.0 NEW
        from solarguard.web import set_anomaly, set_digest
        set_anomaly(self.anomaly)
        set_digest(self.digest)

    # v3.6 NEW: callback pro Scheduler aby spustil scenu
    async def _scheduler_trigger_scene(self, scene_id: str, target_temp_c=None) -> bool:
        """Naplanovany trigger - spousti scenu jako kdyby uzivatel klikl v UI."""
        try:
            self.log.info(f"Scheduler -> scene '{scene_id}' (target_temp_c={target_temp_c})")
            # v4.4.0: naplanovana scena je explicitni prikaz - rusi manual-off hold
            self.ctx.manual_heater_off_until = 0.0
            self.ctx.override_started_at = 0.0
            self.ctx.heat_now_target_reached_at = 0.0
            if scene_id == "heat_now":
                self.ctx.override_active = True
                self.ctx.override_reason = "schedule: heat_now"
                self.ctx.current_scene = "heat_now"
                self.ctx.override_started_at = time.time()
                temp = target_temp_c or self.cfg.get("spa", {}).get("target_temp_c", 38)
                self.ctx.target_temp_override = temp
                await self.spa.set_filter(True, force=True)
                await self.spa.set_heater(True, force=True)
                await self.spa.set_target_temp(temp)
                record_event("scene", name="heat_now", source="schedule", target_temp=temp)
                return True
            elif scene_id == "gentle":
                self.ctx.override_active = False
                self.ctx.override_reason = ""
                self.ctx.current_scene = "gentle"
                temp = target_temp_c or 33
                self.ctx.target_temp_override = temp
                await self.spa.set_target_temp(temp)
                record_event("scene", name="gentle", source="schedule", target_temp=temp)
                return True
            elif scene_id == "solar_auto":
                self.ctx.override_active = False
                self.ctx.override_reason = ""
                self.ctx.current_scene = "solar_auto"
                self.ctx.target_temp_override = None  # zpet na config
                await self.spa.set_heater(False, force=True)
                record_event("scene", name="solar_auto", source="schedule")
                return True
            elif scene_id == "off":
                # Specialni 'off' scena - vsechno vypnout
                # (od v4.4.0 ji DecisionEngine respektuje - drive se topeni
                # pri dalsim prebytku zase zapnulo)
                self.ctx.override_active = False
                self.ctx.override_reason = ""
                self.ctx.current_scene = "off"
                await self.spa.set_heater(False, force=True)
                record_event("scene", name="off", source="schedule")
                return True
            else:
                self.log.warning(f"Unknown scene: {scene_id}")
                return False
        except Exception as e:
            self.log.error(f"Scheduler scene callback error: {e}")
            return False

    async def _spa_refresh_loop(self):
        while not self._shutdown.is_set():
            try:
                await self.spa.refresh_status()
            except Exception as e:
                self.log.error(f"spa refresh error: {e}")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def _decision_loop(self):
        while not self._shutdown.is_set():
            try:
                await self._tick()
            except Exception as e:
                self.log.exception(f"tick error: {e}")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.loop_interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        self.ctx.energy.update(self.ctx.victron)

        # v4.0 NEW: anomaly detector running sums
        try:
            self.anomaly.update_running_sums(self.ctx)
        except Exception as e:
            self.log.error(f"anomaly update error: {e}")

        try:
            await self.cleaning.tick()
        except Exception as ex:
            self.log.error(f"cleaning tick error: {ex}")

        # v4.3.0 NEW: Heat pump auto-rizeni
        if self.heatpump_engine and self.heatpump:
            try:
                hp_decision = self.heatpump_engine.decide(self.ctx)
                if hp_decision.target_state != self.heatpump_engine.current_state:
                    self.log.info(
                        f"HP transition: {self.heatpump_engine.current_state.value} -> "
                        f"{hp_decision.target_state.value} | {hp_decision.reason}"
                    )
                    record_event("heatpump_state",
                                 from_state=self.heatpump_engine.current_state.value,
                                 to_state=hp_decision.target_state.value,
                                 reason=hp_decision.reason)
                    self.heatpump_engine.transition(hp_decision.target_state)
                    # Vykonej akce z rozhodnuti
                    for action_name, args in hp_decision.actions:
                        method = getattr(self.heatpump, action_name, None)
                        if method:
                            try:
                                await method(*args)
                            except Exception as ex:
                                self.log.error(f"HP action {action_name} error: {ex}")
            except Exception as ex:
                self.log.error(f"heatpump engine error: {ex}")

        # FIX v3.1: Nech planner prepocitat PV remaining kazdy tick (je to levne)
        # aby strategie dne drzela krok s aktualnim casem
        if self.planner:
            try:
                self.planner.compute_plan()
            except Exception as ex:
                self.log.exception(f"planner tick compute error: {ex}")

        decision: Decision = self.engine.decide(self.ctx)
        v = self.ctx.victron
        s = self.ctx.spa
        e = self.ctx.env
        p = self.ctx.plan
        c = self.ctx.cleaning

        cleaning_suffix = ""
        if c.is_running:
            cleaning_suffix = f" cleaning={c.duration_hours}h rem={c.remaining_sec/60:.0f}min"

        self.log.info(
            f"[{self.ctx.current_state.value}->{decision.target_state.value}] "
            f"[{p.strategy.value}] "
            f"SOC={self._fmt(v.soc_pct,'%')} PV={self._fmt(v.pv_power_w,'W')} "
            f"surplus={self._fmt(v.surplus_w,'W')} water={self._fmt(s.current_temp_c,'C')} "
            f"air={self._fmt(e.air_temp_c,'C')}{cleaning_suffix} | {decision.reason}"
        )

        self.events.log(
            "tick",
            state_from=self.ctx.current_state.value, state_to=decision.target_state.value,
            soc=v.soc_pct, pv=v.pv_power_w, surplus=v.surplus_w, load=v.load_total_w,
            water_temp=s.current_temp_c, heater_on=s.heater_on,
            action=decision.set_heater, reason=decision.reason,
            override=self.ctx.override_active,
            air_temp=e.air_temp_c, light_lux=e.light_lux, is_raining=e.is_raining,
            strategy=p.strategy.value, discretionary_kwh=p.discretionary_kwh,
            cleaning_running=c.is_running,
            cleaning_remaining_min=round(c.remaining_sec / 60.0, 1) if c.is_running else None,
        )

        record_tick(self.ctx, decision.reason)

        # v3.7 NEW: heating curve tracking
        # Detekujeme prechod heater False->True (start ohrevu) a True->False (konec)
        current_heater = self.ctx.spa.heater_on
        if self._last_heater_state is False and current_heater is True:
            # Start topeni
            if self.ctx.spa.current_temp_c is not None and self.ctx.spa.target_temp_c is not None:
                self.heating_curve.on_heating_start(
                    current_temp=self.ctx.spa.current_temp_c,
                    target_temp=self.ctx.spa.target_temp_c,
                    air_temp=self.ctx.env.air_temp_c,
                    wind_kmh=self.ctx.env.wind_kmh,
                )
        elif self._last_heater_state is True and current_heater is False:
            # Konec topeni
            if self.ctx.spa.current_temp_c is not None:
                self.heating_curve.on_heating_stop(
                    current_temp=self.ctx.spa.current_temp_c,
                    reason=decision.reason or "auto",
                )
        self._last_heater_state = current_heater

        if decision.set_target_temp is not None \
                and s.target_temp_c != decision.set_target_temp:
            self.log.info(f"-> setting target temp to {decision.set_target_temp}C")
            await self.spa.set_target_temp(decision.set_target_temp)

        # v4.1.2 FIX: detekce state drift - pokud state=HEATING ale heater_on=False
        # (typicky po spike co vypnul topeni a state se pak vratil do heating)
        # je nutne aktivne zapnout topeni, ne jen logovat ze "topi"
        if decision.target_state == SystemState.HEATING \
                and s.heater_on is False \
                and decision.set_heater is None \
                and not self.ctx.override_active:
            self.log.warning(
                f"STATE DRIFT: state=HEATING ale heater_on=False -> opravim "
                f"(reason: {decision.reason})"
            )
            decision.set_heater = True

        if decision.set_heater is not None and s.heater_on != decision.set_heater:
            self.log.info(f"-> setting heater to {decision.set_heater}")
            ok = await self.spa.set_heater(decision.set_heater)
            self.events.log("heater_command", target=decision.set_heater, success=ok, reason=decision.reason)
            record_event("heater_command", target=decision.set_heater, success=ok, reason=decision.reason)

        if decision.target_state != self.ctx.current_state:
            self.events.log("state_change",
                from_state=self.ctx.current_state.value, to_state=decision.target_state.value,
                reason=decision.reason)
            record_event("state_change",
                from_state=self.ctx.current_state.value, to_state=decision.target_state.value,
                reason=decision.reason)
            # v4.0 NEW: pocitej kolikrat se aktivoval spike_cool
            if decision.target_state == SystemState.SPIKE_COOLDOWN:
                self.anomaly.record_spike()
            self.ctx.transition(decision.target_state, decision.reason)

    @staticmethod
    def _fmt(val, unit: str) -> str:
        if val is None: return "-"
        return f"{val:.0f}{unit}"

    async def _web_loop(self):
        config = uvicorn.Config(self.app, host="0.0.0.0", port=8000, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def _watchdog_loop(self):
        """v3.9 NEW: posila keepalive do systemd kazdou polovinu intervalu.
        Pokud SolarGuard zamrzne (decision loop neproběhl), systemd ho restartne.
        """
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        if not watchdog_usec:
            self.log.info("systemd watchdog disabled (no WATCHDOG_USEC env)")
            return
        try:
            interval_sec = int(watchdog_usec) / 1_000_000 / 2  # poloviny intervalu
        except (ValueError, TypeError):
            self.log.warning(f"invalid WATCHDOG_USEC: {watchdog_usec}")
            return

        self.log.info(f"systemd watchdog active (notify every {interval_sec:.0f}s)")
        last_decision_tick = time.time()
        while not self._shutdown.is_set():
            try:
                # Detekuj zda decision loop bezi
                from solarguard.web import tick_history
                if tick_history:
                    last_tick_age = time.time() - tick_history[-1].get("ts", 0)
                    if last_tick_age > 120:
                        self.log.error(
                            f"WATCHDOG: main loop stuck (last tick {last_tick_age:.0f}s ago) "
                            f"- NOT notifying systemd, expect restart"
                        )
                        # Skip notify - let systemd restart us
                        try:
                            await asyncio.wait_for(self._shutdown.wait(), timeout=interval_sec)
                        except asyncio.TimeoutError:
                            pass
                        continue
                # Vse OK, notify systemd
                self._sd_notify("WATCHDOG=1")
            except Exception as e:
                self.log.error(f"watchdog check error: {e}")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval_sec)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _sd_notify(state: str) -> None:
        """sd_notify implementation bez systemd python knihovny."""
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        if not notify_socket:
            return
        try:
            import socket
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            # Pokud zacina @, je to abstract socket (Linux)
            addr = notify_socket
            if addr.startswith("@"):
                addr = "\0" + addr[1:]
            s.connect(addr)
            s.sendall(state.encode("utf-8"))
            s.close()
        except Exception:
            pass

    async def run(self):
        self.log.info("=" * 50)
        self.log.info(f"SolarGuard v{VERSION} starting (dry_run={self.dry_run})")
        if not self.dry_run:
            self.log.warning(">>> OSTRY PROVOZ - virivka bude REALNE ovladana <<<")
        self.log.info("=" * 50)
        self.events.log("startup", dry_run=self.dry_run, version=f"v{VERSION}")

        await self.victron.start()
        await self.spa.connect()
        if self.loxone: await self.loxone.start()
        if self.openmeteo: await self.openmeteo.start()
        if self.ote: await self.ote.start()
        # v4.3.0 NEW
        if self.h66: await self.h66.start()
        # v4.3.2 NEW: Seplos BMS RS485
        if self.seplos: await self.seplos.start()
        # v4.3.0 NEW: Appliance learning - sample loop
        await self.learning.start()

        self.log.info("Waiting 30s for initial data (MQTT + forecast)...")
        await asyncio.sleep(30)

        if self.planner:
            await self.planner.start()
        if self.scheduler:
            await self.scheduler.start()
        # v4.1 NEW: digest scheduler
        await self.digest.start()

        # v3.9 NEW: notify systemd we're ready
        self._sd_notify("READY=1\nSTATUS=Running")

        tasks = [
            asyncio.create_task(self._decision_loop()),
            asyncio.create_task(self._spa_refresh_loop()),
            asyncio.create_task(self._web_loop()),
            asyncio.create_task(self._watchdog_loop()),  # v3.9 NEW
        ]
        await self._shutdown.wait()
        # v3.9 NEW: notify systemd we're stopping
        self._sd_notify("STOPPING=1")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        if self.ctx.cleaning.is_running:
            self.log.info("Shutdown: stopping cleaning program")
            try:
                await self.cleaning.stop_program(reason="app_shutdown")
            except Exception as ex:
                self.log.error(f"cleaning stop error at shutdown: {ex}")

        if self.ctx.spa.heater_on and not self.dry_run and not self.ctx.override_active:
            self.log.info("Shutdown: turning off heater")
            await self.spa.set_heater(False)

        await self.victron.stop()
        if self.loxone: await self.loxone.stop()
        if self.openmeteo: await self.openmeteo.stop()
        if self.ote: await self.ote.stop()
        if self.planner: await self.planner.stop()
        if self.scheduler: await self.scheduler.stop()
        if self.h66: await self.h66.stop()  # v4.3.0 NEW
        await self.learning.stop()  # v4.3.0 NEW
        # v4.1 NEW
        await self.digest.stop()
        # v4.0 NEW: persist daily summaries
        try:
            self.anomaly.save()
        except Exception as e:
            self.log.error(f"anomaly save error: {e}")
        self.events.log("shutdown")
        self.log.info("SolarGuard stopped.")

    def request_shutdown(self):
        self.log.info("Shutdown requested")
        self._shutdown.set()


async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)
    app = SolarGuard(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.request_shutdown)

    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
