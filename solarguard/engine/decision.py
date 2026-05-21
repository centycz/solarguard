"""Rozhodovaci jadro - state machine s cleaning awareness + SOC-aware off threshold.

V3.1 FIX (24.4.2026):
- Pri plne baterce (SOC >= battery_full_soc_pct) se off_threshold snizi na
  battery_full_off_threshold_w (default -1500W).

V3.1.1 FIX (25.4.2026):
- Anti-glitch ochrana proti vypnuti topeni na zaklade jednoho ticku.
  Vypnuti vyzaduje aby max surplus za 60s byl pod off threshold.

V3.2 NEW (25.4.2026):
- effective_target_temp(ctx): dynamicky cil teploty podle scény / manualniho override.
  - Solar auto: cfg.target_temp_c (typicky 38C)
  - Mirny rezim: 33C (deti behem dne)
  - Heat now: cfg.target_temp_c
  - Manualni +/- v UI: posledni nastavena hodnota
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..state import SystemContext, SystemState, DayStrategy

log = logging.getLogger("engine")


@dataclass
class SpaConfig:
    surplus_on_w: float = 1500
    surplus_off_w: float = 800
    min_soc_pct: float = 20
    target_temp_c: float = 38
    max_temp_c: float = 40
    stability_window_sec: int = 90
    min_on_time_sec: int = 600
    min_off_time_sec: int = 300
    load_spike_threshold_w: float = 800
    spike_cooldown_sec: int = 600
    spike_ignore_window_sec: int = 90
    spike_safety_surplus_w: float = 500
    spike_ignore_if_battery_charging_w: float = 1000
    min_air_temp_c: float = 2
    wind_reduce_kmh: float = 25.0
    wind_target_reduction_c: int = 2
    # NEW v3.1: fix pro plnou baterku
    battery_full_soc_pct: float = 90.0           # od kdy povazovat baterku za plnou
    battery_full_off_threshold_w: float = -1500  # pri plne baterce: tolik muze vybijet
    # NEW v3.1.1: stability okno pro vypinani topeni (anti-glitch)
    off_stability_window_sec: int = 60           # surplus musi byt pod off prahem aspon X sekund
    # NEW v3.4: per-phase spike detection a noční vypnutí
    phase_max_continuous_w: float = 3500   # bezpecny limit per faze pro Multiplus II 5000 (datasheet 4000W @ 25C)
    spa_phase_label: str = "L2"            # na ktere fazi je virivka pripojena
    night_shutdown: bool = True            # vypnout topeni v noci (slunce neziri, beztak by jelo z baterky)
    night_shutdown_offset_min: int = 60    # po zapadu slunce + offset zacne noc
    morning_resume_offset_min: int = 30    # pred vychodem slunce - offset konci noc
    # NEW v3.8.1: late-day prah - kdyz uz toho moc do vecera nezbyva
    late_day_pv_remaining_kwh: float = 1.5    # pod timto thresholdem zvys on_w
    late_day_surplus_on_w: float = 2200       # vyssi prah aby se nezapomelo a delalo nejistotu
    # NEW v4.1.6: BAT-FULL kickstart - resi catch-22 kdy FVE neumi vyrabet bez odberu
    # Pri plne baterce FVE omezi vyrobu na pokryti spotreby domu. Surplus je pak ~0
    # nebo mirne zaporny, takze stable_surplus (=MIN za 90s) ho zachyti jako -100W
    # a nikdy se nezapne. Reseni: pokud vidime PV > X W a spotreba domu > Y W,
    # zapneme i pri zapornem stable_surplus - FVE odpovi zvedanim vyroby.
    bat_full_kickstart_enabled: bool = True
    bat_full_kickstart_min_pv_w: float = 500    # FVE jeste neco vyrabi (tedy svit slunce)
    bat_full_kickstart_min_load_w: float = 500  # dum ma rozumny odber (= FVE umi nahodit)


@dataclass
class Decision:
    target_state: SystemState
    set_heater: Optional[bool] = None
    set_target_temp: Optional[int] = None
    reason: str = ""


class DecisionEngine:
    def __init__(self, config: SpaConfig):
        self.cfg = config

    def _stable_surplus(self, ctx: SystemContext) -> Optional[float]:
        now = time.time()
        cutoff = now - self.cfg.stability_window_sec
        recent = [v for t, v in ctx.surplus_history if t >= cutoff]
        if len(recent) < 3: return None
        return min(recent)

    def _max_surplus_in_window(self, ctx: SystemContext, window_sec: int) -> Optional[float]:
        """Vrati MAXIMUM surplus za posledni window_sec sekund.

        NEW v3.1.1: Pouziva se pro vypinani topeni jako anti-glitch.
        Pokud byl jediny tick s nizkym surplus (jako 4500W propad PV za 30s
        a hned zpatky), tak max() z 60s okna ten glitch ignoruje a topeni nevypne.
        """
        now = time.time()
        cutoff = now - window_sec
        recent = [v for t, v in ctx.surplus_history if t >= cutoff]
        if len(recent) < 2: return None
        return max(recent)

    def _effective_target(self, ctx: SystemContext) -> float:
        """NEW v3.2: target_temp_override prepise default cil teploty.

        Pouzity scenari:
        - Mirny rezim 33C (deti behem dne) - target_temp_override = 33
        - Manualni nastaveni cile pres +/- v UI - target_temp_override = X
        - Default Solar auto / Heat now - target_temp_override = None (= cfg)
        """
        override = getattr(ctx, 'target_temp_override', None)
        if override is not None:
            return float(override)
        return float(self.cfg.target_temp_c)

    def _is_night_time(self, ctx: SystemContext):
        """v3.4 NEW: Vrátí (is_night, reason) podle sunset/sunrise z Open-Meteo.

        Pokud forecast chybí, použije fallback 21:00-07:00 (rozumný režim pro CR).
        Vrati tuple (bool, str) - bool je True pokud je noc, str je vysvetleni.
        """
        if not self.cfg.night_shutdown:
            return (False, "night_shutdown disabled")

        from datetime import datetime, time as dtime
        now = datetime.now()
        f = ctx.forecast

        sunset = None
        sunrise = None
        if f.sunset:
            try:
                sunset = datetime.fromisoformat(f.sunset.replace('T', ' '))
            except Exception:
                pass
        if f.sunrise:
            try:
                sunrise = datetime.fromisoformat(f.sunrise.replace('T', ' '))
            except Exception:
                pass

        if sunset and sunrise:
            from datetime import timedelta
            night_start = sunset + timedelta(minutes=self.cfg.night_shutdown_offset_min)
            morning_end = sunrise - timedelta(minutes=self.cfg.morning_resume_offset_min)
            # Ráno (před morning_end) - pořád noc
            if now < morning_end:
                return (True, f"pred vychodem slunce ({sunrise.strftime('%H:%M')})")
            # Večer (po night_start) - noc začíná
            if now >= night_start:
                return (True, f"po zapadu slunce ({sunset.strftime('%H:%M')} +{self.cfg.night_shutdown_offset_min}min)")
            return (False, f"den (sunset {sunset.strftime('%H:%M')})")

        # Fallback bez forecastu: noc je 21:00-07:00
        hour = now.hour
        if hour < 7 or hour >= 21:
            return (True, f"fallback noc {hour}h (21:00-07:00)")
        return (False, f"fallback den {hour}h")

    def _load_spike_detected(self, ctx: SystemContext) -> bool:
        """v3.4: Per-phase spike detection.

        Vířivka topí na L2 (2200W). Pokud spike přijde na L1 (vápna, trouba)
        a L2 + L3 mají dost rezervy, není důvod vypínat topení.

        Spike vypneme topení JEN pokud:
        - skok je na L2 (fáze vířivky), nebo
        - kterákoli fáze se přiblíží limitu Multiplus II (3500W per phase),
          což by hrozilo overload a shutdown jako 26.4. dopoledne.

        v3.8.1 NEW: respektuje manual_heater_started_at - pokud uzivatel rucne
        zapnul topeni (web/scena), ignorujeme spike na L2 po dobu
        spike_ignore_window_sec - jako kdyby state byl HEATING.

        v4.1.5 NEW: nastavuje ctx._last_spike_reason pro presnejsi log v decide()
        """
        now = time.time()
        if ctx.current_state == SystemState.HEATING \
                and ctx.time_in_state() < self.cfg.spike_ignore_window_sec:
            return False

        # v3.8.1 NEW: stejna ignore-window i kdyz uzivatel rucne zapnul pres web
        # (state mozna jeste neni HEATING protoze tick neprobehl)
        manual_age = now - ctx.manual_heater_started_at
        if ctx.manual_heater_started_at > 0 \
                and manual_age < self.cfg.spike_ignore_window_sec:
            log.info(
                f"Manual heater start {manual_age:.0f}s ago "
                f"-> ignoring potential spike (within {self.cfg.spike_ignore_window_sec}s window)"
            )
            return False

        # 1) Per-phase overload check: pokud kterakoli faze prekrocila phase_max,
        #    je to nebezpecne -> vypnout (Multiplus II shutdown protection)
        v = ctx.victron
        phase_max = self.cfg.phase_max_continuous_w
        for phase_label, val in [
            ("L1", v.load_l1_w), ("L2", v.load_l2_w), ("L3", v.load_l3_w)
        ]:
            if val is not None and val > phase_max:
                log.warning(
                    f"PHASE OVERLOAD: {phase_label}={val:.0f}W > {phase_max}W "
                    f"-> vypnu virivku ABYCHom predesli Multiplus shutdown"
                )
                ctx._last_spike_reason = f"phase overload: {phase_label}={val:.0f}W > {phase_max}W (Multiplus protection)"
                return True

        # 2) Per-phase spike check pro fazi virivky (L2)
        spa_phase = self.cfg.spa_phase_label  # "L2"
        spa_history = {
            "L1": ctx.load_l1_history,
            "L2": ctx.load_l2_history,
            "L3": ctx.load_l3_history,
        }.get(spa_phase, ctx.load_l2_history)

        window_start = now - 60
        recent = [val for t, val in spa_history if t >= window_start]
        if len(recent) < 10:
            return False
        avg_old = sum(recent[:-5]) / max(1, len(recent) - 5)
        current = sum(recent[-3:]) / 3
        delta = current - avg_old

        # Skok na fazi virivky musi byt vyznamny (>load_spike_threshold_w/2 -
        # protoze jedna faze je tretina z totalniho prahu)
        phase_spike_threshold = self.cfg.load_spike_threshold_w / 2
        if delta <= phase_spike_threshold:
            return False

        # 3) Skok na L2 nastal - jeste zkontroluj jestli neni grid-neutral
        surplus = v.surplus_w
        bat = v.battery_power_w
        if surplus is not None and surplus > self.cfg.spike_safety_surplus_w:
            log.info(
                f"L2 SPIKE +{delta:.0f}W ale surplus {surplus:.0f}W "
                f"=> ignoring (grid-neutral)"
            )
            return False
        if bat is not None and bat > self.cfg.spike_ignore_if_battery_charging_w:
            log.info(
                f"L2 SPIKE +{delta:.0f}W ale baterka nabiji {bat:.0f}W "
                f"=> ignoring (battery buffer)"
            )
            return False

        log.warning(
            f"L2 SPIKE detected: +{delta:.0f}W na fazi virivky "
            f"(current={current:.0f}W, was={avg_old:.0f}W)"
        )
        ctx._last_spike_reason = f"L2 skok odberu +{delta:.0f}W na fazi virivky"
        return True

    def _effective_thresholds(self, ctx: SystemContext):
        """Vraci (on_w, off_w).

        NEW v3.1: Pri plne baterce (SOC >= battery_full_soc_pct) se off threshold
        snizuje na battery_full_off_threshold_w. To umoznuje pokracovat topit
        i pri docasne oblacnosti - baterka klidne vybije par stovek wattu misto
        aby se prebytek ztratil do site.

        NEW v3.8.1: Late-day check. Pokud Open-Meteo predikuje ze do konce dne
        zbyva vyrobit < late_day_pv_remaining_kwh kWh, znamena to ze slunce
        zapada a uz toho moc neudela. Zvysime on_w aby se uz nezapinaly nove
        topne cykly ze zoufalstvi - lepsi nechat baterii do veceru.
        """
        plan = ctx.plan
        if plan.dynamic_surplus_on_w is not None:
            on_w = plan.dynamic_surplus_on_w
            off_w = plan.dynamic_surplus_off_w
        else:
            on_w = self.cfg.surplus_on_w
            off_w = self.cfg.surplus_off_w

        # Pokud je baterka plna, nechame topeni bezet i pri malem/zadnem prebytku
        v = ctx.victron
        if v.soc_pct is not None and v.soc_pct >= self.cfg.battery_full_soc_pct:
            # off_w muze byt drasticky nizsi
            off_w = min(off_w, self.cfg.battery_full_off_threshold_w)
            # on_w taky posuneme dolu - aby se klidne zaplo pri jakemkoli prebytku
            # kdyz je baterka plna (misto cekani na 300W pri aggressive)
            on_w = min(on_w, 100)

        # v3.8.1 NEW: late-day check - slunce zapada, malo zbyva na vyrobu
        f = ctx.forecast
        if f.predicted_pv_kwh_remaining is not None \
                and f.predicted_pv_kwh_remaining < self.cfg.late_day_pv_remaining_kwh:
            # Aplikuj vyssi prah - pokud uzivatel chce delat ad-hoc startup
            # ze zapadajícího slunce, musí být přebytek opravdu robustní.
            # Battery-full vyjimka ma prednost (off_w nemenime).
            if not (v.soc_pct is not None and v.soc_pct >= self.cfg.battery_full_soc_pct):
                on_w = max(on_w, self.cfg.late_day_surplus_on_w)

        return (on_w, off_w)

    def decide(self, ctx: SystemContext) -> Decision:
        if ctx.override_active:
            return Decision(ctx.current_state,
                reason=f"override: {ctx.override_reason}")

        v = ctx.victron
        s = ctx.spa
        e = ctx.env
        plan = ctx.plan
        now = time.time()

        # 1) STALE / OFFLINE / ERROR
        if v.is_stale:
            return Decision(SystemState.SAFE_MODE, set_heater=False, reason="victron MQTT stale")
        if not s.online and s.consecutive_failures >= 5:
            return Decision(SystemState.SAFE_MODE, set_heater=False, reason="spa offline")
        if s.has_error:
            return Decision(SystemState.SAFE_MODE, set_heater=False, reason=f"spa error {s.error_code}")

        # 2) MRAZ
        real_air_temp = e.air_temp_c if not e.is_stale else None
        if real_air_temp is not None and real_air_temp < self.cfg.min_air_temp_c:
            return Decision(SystemState.SAFE_MODE, set_heater=False,
                            reason=f"frost protection: {real_air_temp}C < {self.cfg.min_air_temp_c}C")

        # 2.5) NIGHT_OFF v3.4: pres noc topeni nepovolime - virivka by tahala baterku
        # Vyjimka: Heat now scena/override - tam uzivatel vedome chce topit
        if ctx.current_scene != "heat_now" and not ctx.override_active:
            is_night, night_reason = self._is_night_time(ctx)
            if is_night:
                if ctx.current_state == SystemState.HEATING:
                    return Decision(SystemState.NIGHT_OFF, set_heater=False,
                                    reason=f"NIGHT OFF: {night_reason}")
                return Decision(SystemState.NIGHT_OFF, set_heater=False,
                                reason=f"NIGHT OFF: {night_reason}")
            # Den - pokud byl predtim NIGHT_OFF, prejdi na IDLE
            if ctx.current_state == SystemState.NIGHT_OFF:
                return Decision(SystemState.IDLE, reason=f"morning resume: {night_reason}")

        # 3) SURVIVE
        if plan.strategy == DayStrategy.SURVIVE:
            if ctx.current_state == SystemState.HEATING:
                return Decision(SystemState.IDLE, set_heater=False, reason=f"SURVIVE mode: {plan.reason}")
            return Decision(SystemState.IDLE, reason=f"SURVIVE mode: {plan.reason}")

        # 4) HARD STOP
        if s.current_temp_c is not None and s.current_temp_c >= self.cfg.max_temp_c:
            return Decision(SystemState.IDLE, set_heater=False, reason=f"max temp {s.current_temp_c}C")

        # 5) SKOK ODBERU
        if self._load_spike_detected(ctx):
            ctx.cooldown_until = now + self.cfg.spike_cooldown_sec
            spike_reason = getattr(ctx, "_last_spike_reason", "cizi skok odberu v domacnosti")
            return Decision(SystemState.SPIKE_COOLDOWN, set_heater=False, reason=spike_reason)
        if ctx.current_state == SystemState.SPIKE_COOLDOWN:
            if now < ctx.cooldown_until:
                return Decision(SystemState.SPIKE_COOLDOWN, reason=f"spike cooldown {int(ctx.cooldown_until-now)}s")
            # v4.1.2 FIX: po skonceni cooldownu prejdi explicitne do IDLE
            # (predtim padalo na fallback "fallback noc/den" bez akce)
            log.info(f"SPIKE cooldown ukoncen po {self.cfg.spike_cooldown_sec}s -> IDLE")
            return Decision(SystemState.IDLE, reason="spike cooldown skoncil")

        # 6) Tvrde minimum SOC
        if v.soc_pct is not None and v.soc_pct < self.cfg.min_soc_pct:
            decision = Decision(SystemState.IDLE,
                reason=f"SOC {v.soc_pct:.0f}% < hard min {self.cfg.min_soc_pct}%")
            if ctx.current_state == SystemState.HEATING \
                    and ctx.time_in_state() < self.cfg.min_on_time_sec:
                decision.target_state = SystemState.HEATING
                decision.reason = "critical SOC but min_on_time"
            else:
                decision.set_heater = False
            return decision

        # 7) Vitr
        if e.wind_kmh is not None and e.wind_kmh > self.cfg.wind_reduce_kmh:
            eff_target = self._effective_target(ctx)
            reduced_target = eff_target - self.cfg.wind_target_reduction_c
            if s.target_temp_c is not None and s.target_temp_c > reduced_target:
                log.info(f"Wind {e.wind_kmh} km/h > {self.cfg.wind_reduce_kmh} -> recommend target {reduced_target}C")

        # 8) Teplota dosazena
        target = self._effective_target(ctx)
        if s.current_temp_c is not None and s.current_temp_c >= target:
            if ctx.current_state == SystemState.HEATING:
                return Decision(SystemState.IDLE, set_heater=False, reason=f"target reached ({s.current_temp_c}C >= {target:.0f}C)")
            return Decision(SystemState.IDLE, reason=f"at target temp ({target:.0f}C)")

        stable_surplus = self._stable_surplus(ctx)
        on_threshold, off_threshold = self._effective_thresholds(ctx)
        strat = plan.strategy.value if plan.strategy else "unknown"
        cleaning_tag = " +cleaning" if ctx.cleaning.is_running else ""

        # NEW v3.1: pokud je baterka plna, pridame tag do reasonu
        battery_full = v.soc_pct is not None and v.soc_pct >= self.cfg.battery_full_soc_pct
        full_tag = f" BAT-FULL({v.soc_pct:.0f}%)" if battery_full else ""

        # v4.1.6 NEW: BAT-FULL kickstart logika
        # Kdyz je baterka plna, FVE se sama omezi na pokryti spotreby domu.
        # Klasicky surplus je pak ~0 nebo mirne zaporny -> stable_surplus
        # (= MIN za 90s) zachyti propady na -100..-200W -> nikdy se nezapne.
        # ALE: jakmile zacneme topit (2200W), FVE okamzite zvedne vyrobu.
        # Reseni: pokud je BAT-FULL + slunce sviti + dum ma rozumny odber,
        # zapneme i pri nepatrne zapornem stable_surplus.
        # Predpoklad: aspon par hodin po vychodu / pred zapadem (PV > 1500W).
        kickstart_eligible = (
            self.cfg.bat_full_kickstart_enabled
            and battery_full
            and ctx.current_state == SystemState.IDLE
            and v.pv_power_w is not None and v.pv_power_w > self.cfg.bat_full_kickstart_min_pv_w
            and v.load_total_w is not None and v.load_total_w > self.cfg.bat_full_kickstart_min_load_w
        )

        # 9) HLAVNI ROZHODOVANI
        if ctx.current_state == SystemState.IDLE:
            if ctx.time_in_state() < self.cfg.min_off_time_sec:
                return Decision(SystemState.IDLE,
                    reason=f"min_off_time ({int(self.cfg.min_off_time_sec - ctx.time_in_state())}s)")
            if stable_surplus is not None and stable_surplus > on_threshold:
                # NEW v3.2: nastav target podle effective_target (mirny rezim, manualni override, ...)
                eff_target = int(self._effective_target(ctx))
                target_to_set = None
                if s.target_temp_c is None or s.target_temp_c != eff_target:
                    target_to_set = eff_target
                return Decision(SystemState.HEATING, set_heater=True,
                    set_target_temp=target_to_set,
                    reason=f"[{strat}{cleaning_tag}{full_tag}] surplus {stable_surplus:.0f}W > {on_threshold:.0f} (cil {eff_target}C)")

            # v4.1.6 NEW: BAT-FULL kickstart vyjimka
            # Pokud klasicky test selhal kvuli zapornemu stable_surplus, ale jsme
            # v BAT-FULL situaci kdy FVE jen nema kam pustit energii, zkusime to.
            # FVE pri zapnuti virivky zvedne vyrobu o 2200W - to je predpoklad.
            if kickstart_eligible:
                eff_target = int(self._effective_target(ctx))
                target_to_set = None
                if s.target_temp_c is None or s.target_temp_c != eff_target:
                    target_to_set = eff_target
                return Decision(SystemState.HEATING, set_heater=True,
                    set_target_temp=target_to_set,
                    reason=f"[{strat}{cleaning_tag}{full_tag}] KICKSTART: PV={v.pv_power_w:.0f}W omezena (baterka plna), zapinam topeni aby FVE mela kam dodat (cil {eff_target}C)")

            return Decision(SystemState.IDLE,
                reason=f"[{strat}{cleaning_tag}{full_tag}] nedost. prebytek (stable={stable_surplus}, potreba>{on_threshold:.0f})")

        if ctx.current_state == SystemState.HEATING:
            if ctx.time_in_state() < self.cfg.min_on_time_sec:
                return Decision(SystemState.HEATING,
                    reason=f"min_on_time ({int(self.cfg.min_on_time_sec - ctx.time_in_state())}s)")
            current_surplus = v.surplus_w
            # NEW v3.1.1: Anti-glitch - vypneme jen kdyz max za poslednich 60s je pod off
            # Tim ignorujeme jednorazove propady PV (mraky, MQTT glitche)
            max_surplus_recent = self._max_surplus_in_window(ctx, self.cfg.off_stability_window_sec)
            sustained_low = (
                current_surplus is not None and current_surplus < off_threshold and
                (max_surplus_recent is None or max_surplus_recent < off_threshold)
            )
            if sustained_low:
                return Decision(SystemState.COOLDOWN, set_heater=False,
                    reason=f"[{strat}{cleaning_tag}{full_tag}] surplus {current_surplus:.0f}W < {off_threshold:.0f} (sustained {self.cfg.off_stability_window_sec}s)")
            # Pokud aktualne pod prahem ale max za okno > prah, je to glitch -> nevypni
            if current_surplus is not None and current_surplus < off_threshold:
                return Decision(SystemState.HEATING,
                    reason=f"[{strat}{cleaning_tag}{full_tag}] glitch: surplus {current_surplus:.0f}W ale max za {self.cfg.off_stability_window_sec}s={max_surplus_recent:.0f}W -> ignoruji")
            # Pro zlepseni logu: kdyz je baterka plna a surplus maly, zduraznit to
            if battery_full and current_surplus is not None and current_surplus < 200:
                return Decision(SystemState.HEATING,
                    reason=f"[{strat}{cleaning_tag}{full_tag}] topi, baterka plna -> vybiji pro topeni ({current_surplus:.0f}W)")
            return Decision(SystemState.HEATING,
                reason=f"[{strat}{cleaning_tag}{full_tag}] topi, surplus={current_surplus}")

        if ctx.current_state == SystemState.COOLDOWN:
            if ctx.time_in_state() >= self.cfg.min_off_time_sec:
                return Decision(SystemState.IDLE, reason="cooldown dokoncen")
            return Decision(SystemState.COOLDOWN,
                reason=f"cooldown {int(self.cfg.min_off_time_sec - ctx.time_in_state())}s")

        if ctx.current_state == SystemState.SAFE_MODE:
            return Decision(SystemState.IDLE, reason="recovery from safe mode")

        return Decision(SystemState.IDLE, reason="fallback")
