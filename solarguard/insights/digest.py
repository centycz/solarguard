"""
v4.1 NEW: Weekly digest.

Každou neděli v 18:00 spočítá statistiky za týden a:
- Uloží do JSON souboru (digest_history.jsonl)
- Pošle na Slack/Telegram/email pokud config
- Vystavi přes API endpoint /api/digest/latest

Formát digestu (markdown-friendly):

  📊 SolarGuard týdenní souhrn 21.4 - 27.4
  ─────────────────────────────────────
  ☀ FV výroba: 287 kWh (-15% vs minulý týden)
  🏠 Spotřeba: 198 kWh (+5%)
  ⚡ Soběstačnost: 78%
  🛁 Vířivka: 12 ohřevů, 4.2h celkem
  💰 Spot ceny: průměr 2.4 Kč/kWh, peak 8.5 Kč
  🌞 Slunečné dny: 4/7

  Top den: úterý 5.5 kWh
  Worst den: čtvrtek 1.2 kWh (oblačno)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Optional, Dict, Any

import aiohttp

log = logging.getLogger("digest")


@dataclass
class WeeklyDigest:
    week_start: str   # YYYY-MM-DD (pondělí)
    week_end: str     # YYYY-MM-DD (neděle)
    generated_at: float
    pv_total_kwh: float = 0
    pv_avg_per_day: float = 0
    pv_best_day: str = ""
    pv_best_kwh: float = 0
    pv_worst_day: str = ""
    pv_worst_kwh: float = 0
    consumption_total_kwh: float = 0
    consumption_avg_per_day: float = 0
    self_sufficiency_pct: float = 0
    heating_sessions: int = 0
    heating_total_minutes: float = 0
    sunny_days: int = 0  # dny s sunny_hours >= 6
    spike_count: int = 0
    delta_pv_pct: Optional[float] = None  # vs předchozí týden
    delta_cons_pct: Optional[float] = None
    insights_text: List[str] = field(default_factory=list)


def _format_digest_markdown(d: WeeklyDigest) -> str:
    """Hezký Markdown výstup pro Slack/email."""
    lines = []
    lines.append(f"📊 *SolarGuard týdenní souhrn {d.week_start} – {d.week_end}*")
    lines.append("─" * 40)

    # FV
    delta = ""
    if d.delta_pv_pct is not None:
        sign = "+" if d.delta_pv_pct >= 0 else ""
        delta = f" ({sign}{d.delta_pv_pct:.0f}% vs minulý týden)"
    lines.append(f"☀ FV výroba: *{d.pv_total_kwh:.0f} kWh*{delta}")
    lines.append(f"   průměr {d.pv_avg_per_day:.1f} kWh/den")

    # Spotřeba
    delta = ""
    if d.delta_cons_pct is not None:
        sign = "+" if d.delta_cons_pct >= 0 else ""
        delta = f" ({sign}{d.delta_cons_pct:.0f}% vs minulý týden)"
    lines.append(f"🏠 Spotřeba: *{d.consumption_total_kwh:.0f} kWh*{delta}")
    lines.append(f"   průměr {d.consumption_avg_per_day:.1f} kWh/den")

    # Soběstačnost
    if d.self_sufficiency_pct > 0:
        lines.append(f"⚡ Soběstačnost: *{d.self_sufficiency_pct:.0f}%*")

    # Vířivka
    h_h = d.heating_total_minutes / 60
    lines.append(f"🛁 Vířivka: *{d.heating_sessions}× ohřev*, {h_h:.1f} hodin")

    # Slunečné dny
    lines.append(f"🌞 Slunečné dny: *{d.sunny_days}/7*")

    if d.spike_count > 0:
        lines.append(f"⚡ Spike protection: {d.spike_count}× ({d.spike_count // 7} per den průměrně)")

    # Best/worst
    lines.append("")
    if d.pv_best_day:
        lines.append(f"📈 Top den: *{d.pv_best_day}* {d.pv_best_kwh:.1f} kWh")
    if d.pv_worst_day:
        lines.append(f"📉 Worst den: *{d.pv_worst_day}* {d.pv_worst_kwh:.1f} kWh")

    # Insights
    if d.insights_text:
        lines.append("")
        lines.append("💡 *Postřehy:*")
        for ins in d.insights_text:
            lines.append(f"   • {ins}")

    return "\n".join(lines)


class DigestGenerator:
    """Generuje weekly digest. Volá se 1x týdně + manuálně přes API."""

    def __init__(self, log_dir: str, anomaly_detector, config: dict):
        self.detector = anomaly_detector
        self.log_dir = Path(log_dir)
        self.history_path = self.log_dir / "digest_history.jsonl"
        self.config = config
        self._last_digest: Optional[WeeklyDigest] = None
        self._load_last_digest()
        self._shutdown = asyncio.Event()

    def _load_last_digest(self) -> None:
        if not self.history_path.exists():
            return
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                last = json.loads(lines[-1])
                self._last_digest = WeeklyDigest(**last)
        except Exception as e:
            log.warning(f"Failed to load last digest: {e}")

    @property
    def latest(self) -> Optional[WeeklyDigest]:
        return self._last_digest

    def generate_now(self) -> WeeklyDigest:
        """Spočítá digest z anomaly detector history."""
        # Najdi posledních 7 dní (kromě dneška, ten ještě běží)
        all_summaries = self.detector.history
        if len(all_summaries) < 2:
            log.warning("Not enough data for digest (need at least 2 days)")
            return WeeklyDigest(
                week_start="?", week_end="?",
                generated_at=time.time(),
            )

        # Zahrň "dnešek" pokud je neděle (jinak posledních 7 dnů)
        today = date.today()
        # ISO weekday: Monday=1 ... Sunday=7
        if today.isoweekday() == 7:  # neděle
            week_days = all_summaries[-7:] if len(all_summaries) >= 7 else all_summaries[:]
        else:
            # Posledních 7 ukončených dnů (mimo dnešek)
            ended = all_summaries[:-1]
            week_days = ended[-7:] if len(ended) >= 7 else ended[:]

        if not week_days:
            log.warning("No completed days for digest")
            return WeeklyDigest(
                week_start="?", week_end="?",
                generated_at=time.time(),
            )

        # Předchozí týden (pro deltu)
        prev_week = []
        if today.isoweekday() == 7:
            prev_week = all_summaries[-14:-7] if len(all_summaries) >= 14 else []
        else:
            ended = all_summaries[:-1]
            prev_week = ended[-14:-7] if len(ended) >= 14 else []

        # Spočítej
        pv_values = [d.pv_yield_kwh or 0 for d in week_days]
        cons_values = [d.consumption_kwh or 0 for d in week_days]
        sunny_days_count = sum(1 for d in week_days if d.sunny_hours >= 6)
        heating_sess = sum(d.heating_sessions for d in week_days)
        heating_min = sum(d.total_heating_minutes for d in week_days)
        spike_total = sum(d.spike_count for d in week_days)

        pv_total = sum(pv_values)
        cons_total = sum(cons_values)

        # Best/worst day pro PV
        pv_best_idx = max(range(len(week_days)), key=lambda i: pv_values[i]) if pv_values else 0
        pv_worst_idx = min(range(len(week_days)), key=lambda i: pv_values[i]) if pv_values else 0

        # Soběstačnost
        if cons_total > 0:
            ss = min(100, pv_total / cons_total * 100)
        else:
            ss = 0

        # Delta vs minulý týden
        delta_pv = None
        delta_cons = None
        if prev_week:
            prev_pv = sum(d.pv_yield_kwh or 0 for d in prev_week)
            prev_cons = sum(d.consumption_kwh or 0 for d in prev_week)
            if prev_pv > 0:
                delta_pv = (pv_total - prev_pv) / prev_pv * 100
            if prev_cons > 0:
                delta_cons = (cons_total - prev_cons) / prev_cons * 100

        # Insights
        insights = []
        if pv_total > 250:
            insights.append(f"Skvělý týden pro FV - {pv_total:.0f} kWh je hodně i v sezóně.")
        if sunny_days_count >= 5:
            insights.append(f"{sunny_days_count} slunečných dnů - jaro/léto je tady.")
        if delta_pv is not None and delta_pv < -30:
            insights.append(f"Pokles výroby o {abs(delta_pv):.0f}% - pravděpodobně horší počasí.")
        if heating_sess >= 7:
            insights.append(f"Vířivka topila každý den - skvělá využitelnost FVE.")
        elif heating_sess == 0:
            insights.append("Vířivka tento týden netopila - voda je asi na cíli, nebo strategie nedovolila.")
        if spike_total > 10:
            insights.append(f"{spike_total}× spike protection - hodně cizích spotřebičů kolize, zvaž vyšší prahy.")

        digest = WeeklyDigest(
            week_start=week_days[0].date,
            week_end=week_days[-1].date,
            generated_at=time.time(),
            pv_total_kwh=round(pv_total, 1),
            pv_avg_per_day=round(pv_total / len(week_days), 1),
            pv_best_day=week_days[pv_best_idx].date,
            pv_best_kwh=round(pv_values[pv_best_idx], 1),
            pv_worst_day=week_days[pv_worst_idx].date,
            pv_worst_kwh=round(pv_values[pv_worst_idx], 1),
            consumption_total_kwh=round(cons_total, 1),
            consumption_avg_per_day=round(cons_total / len(week_days), 1),
            self_sufficiency_pct=round(ss, 1),
            heating_sessions=heating_sess,
            heating_total_minutes=round(heating_min, 1),
            sunny_days=sunny_days_count,
            spike_count=spike_total,
            delta_pv_pct=delta_pv,
            delta_cons_pct=delta_cons,
            insights_text=insights,
        )

        return digest

    def save_digest(self, digest: WeeklyDigest) -> None:
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(digest), ensure_ascii=False) + "\n")
            self._last_digest = digest
            log.info(f"Digest saved: {digest.week_start} - {digest.week_end}")
        except Exception as e:
            log.error(f"Failed to save digest: {e}")

    async def send_slack(self, digest: WeeklyDigest, webhook_url: str) -> bool:
        text = _format_digest_markdown(digest)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    webhook_url,
                    json={"text": text},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        log.info("Digest sent to Slack")
                        return True
                    log.warning(f"Slack webhook returned {r.status}")
                    return False
        except Exception as e:
            log.warning(f"Slack send failed: {e}")
            return False

    async def send_telegram(self, digest: WeeklyDigest, bot_token: str, chat_id: str) -> bool:
        text = _format_digest_markdown(digest)
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    url,
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        log.info("Digest sent to Telegram")
                        return True
                    body = await r.text()
                    log.warning(f"Telegram returned {r.status}: {body[:200]}")
                    return False
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return False

    async def deliver(self, digest: WeeklyDigest) -> None:
        """Pošle digest přes všechny configured channels."""
        cfg = self.config
        slack_url = cfg.get("slack_webhook")
        if slack_url:
            await self.send_slack(digest, slack_url)

        tg_token = cfg.get("telegram_bot_token")
        tg_chat = cfg.get("telegram_chat_id")
        if tg_token and tg_chat:
            await self.send_telegram(digest, tg_token, tg_chat)

        # Vždycky log do journalctl - to je nejjednodušší "digest channel"
        log.info("=" * 60)
        log.info("WEEKLY DIGEST")
        log.info("=" * 60)
        for line in _format_digest_markdown(digest).split("\n"):
            log.info(line)
        log.info("=" * 60)

    async def _scheduler_loop(self) -> None:
        """Kontroluje každých 60s jestli je čas na týdenní digest."""
        last_run_date: Optional[str] = None
        # Získej datum posledního digestu - aby se po restartu neopakoval
        if self._last_digest:
            last_run_date = self._last_digest.week_end

        while not self._shutdown.is_set():
            try:
                now = datetime.now()
                today_str = date.today().isoformat()
                # Trigger condition: neděle 18:00 a ještě jsme dnes nepublikovali
                if (now.isoweekday() == 7
                        and now.hour >= 18
                        and last_run_date != today_str):
                    log.info(f"Triggering weekly digest at {now}")
                    digest = self.generate_now()
                    self.save_digest(digest)
                    await self.deliver(digest)
                    last_run_date = today_str
            except Exception as e:
                log.exception(f"digest scheduler error: {e}")

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        log.info("Digest generator starting (sunday 18:00 trigger)")
        asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._shutdown.set()


def format_digest(digest: WeeklyDigest) -> str:
    """Public helper pro UI/CLI."""
    return _format_digest_markdown(digest)
