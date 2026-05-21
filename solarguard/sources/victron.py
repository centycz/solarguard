"""
Victron Cerbo GX - MQTT klient s keepalive + daily history.
Scita Yield z obou MPPT controlleru (Ulice + Zahrada).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from paho.mqtt import client as mqtt_client

from ..state import SystemContext

log = logging.getLogger("victron")


LIVE_TOPICS = {
    "system/0/Dc/Battery/Soc": "soc_pct",
    "system/0/Dc/Battery/Power": "battery_power_w",
    "system/0/Dc/Pv/Power": "pv_power_w",
    "system/0/Ac/Grid/L1/Power": "grid_l1_w",
    "system/0/Ac/Grid/L2/Power": "grid_l2_w",
    "system/0/Ac/Grid/L3/Power": "grid_l3_w",
    "system/0/Ac/Consumption/L1/Power": "load_l1_w",
    "system/0/Ac/Consumption/L2/Power": "load_l2_w",
    "system/0/Ac/Consumption/L3/Power": "load_l3_w",
}

MPPT_YIELD_TODAY = [
    "solarcharger/0/History/Daily/0/Yield",
    "solarcharger/1/History/Daily/0/Yield",
]
MPPT_YIELD_YESTERDAY = [
    "solarcharger/0/History/Daily/1/Yield",
    "solarcharger/1/History/Daily/1/Yield",
]
BATTERY_CHARGED_TOPIC = "battery/512/History/ChargedEnergy"
BATTERY_DISCHARGED_TOPIC = "battery/512/History/DischargedEnergy"

# v4.3.1 NEW: napeti clanku — Cerbo GX exponuje pres BMS/VE.Can integraci
BATTERY_INSTANCE = 512
BATTERY_CELL_V_PREFIX = f"battery/{BATTERY_INSTANCE}/Voltages/"
BATTERY_MIN_CELL_V = f"battery/{BATTERY_INSTANCE}/System/MinCellVoltage"
BATTERY_MAX_CELL_V = f"battery/{BATTERY_INSTANCE}/System/MaxCellVoltage"
BATTERY_MIN_CELL_ID = f"battery/{BATTERY_INSTANCE}/System/MinCellVoltageId"
BATTERY_MAX_CELL_ID = f"battery/{BATTERY_INSTANCE}/System/MaxCellVoltageId"


class VictronMQTT:
    def __init__(
        self,
        host: str, port: int, portal_id: str,
        context: SystemContext,
        username: Optional[str] = None,
        password: Optional[str] = None,
        keepalive_interval: int = 30,
    ):
        self.host = host
        self.port = port
        self.portal_id = portal_id
        self.ctx = context
        self.keepalive_interval = keepalive_interval

        self._mppt_today = {}
        self._mppt_yesterday = {}

        self.client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=f"solarguard-{int(time.time())}",
            clean_session=True,
        )
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self._keepalive_task: Optional[asyncio.Task] = None

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error(f"MQTT connect failed, rc={rc}")
            return
        log.info(f"MQTT connected to {self.host}:{self.port}")
        topics_to_sub = list(LIVE_TOPICS.keys())
        topics_to_sub += MPPT_YIELD_TODAY + MPPT_YIELD_YESTERDAY
        topics_to_sub += [BATTERY_CHARGED_TOPIC, BATTERY_DISCHARGED_TOPIC]
        for suffix in topics_to_sub:
            topic = f"N/{self.portal_id}/{suffix}"
            client.subscribe(topic)
            log.debug(f"subscribed: {topic}")
        # v4.3.1 NEW: cell voltages — wildcard + min/max meta
        for suffix in [
            f"battery/{BATTERY_INSTANCE}/Voltages/#",
            BATTERY_MIN_CELL_V, BATTERY_MAX_CELL_V,
            BATTERY_MIN_CELL_ID, BATTERY_MAX_CELL_ID,
        ]:
            topic = f"N/{self.portal_id}/{suffix}"
            client.subscribe(topic)
            log.debug(f"subscribed: {topic}")

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        log.warning(f"MQTT disconnected, rc={rc} - paho si zajisti reconnect")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            value = payload.get("value")
            if value is None: return

            prefix = f"N/{self.portal_id}/"
            if not msg.topic.startswith(prefix): return
            suffix = msg.topic[len(prefix):]

            attr = LIVE_TOPICS.get(suffix)
            if attr is not None:
                setattr(self.ctx.victron, attr, float(value))
                self.ctx.victron.last_update = time.time()
                now = time.time()
                surplus = self.ctx.victron.surplus_w
                load = self.ctx.victron.load_total_w
                if surplus is not None:
                    self.ctx.surplus_history.append((now, surplus))
                if load is not None:
                    self.ctx.load_history.append((now, load))
                # v3.4 NEW: per-phase histories
                if self.ctx.victron.load_l1_w is not None:
                    self.ctx.load_l1_history.append((now, self.ctx.victron.load_l1_w))
                if self.ctx.victron.load_l2_w is not None:
                    self.ctx.load_l2_history.append((now, self.ctx.victron.load_l2_w))
                if self.ctx.victron.load_l3_w is not None:
                    self.ctx.load_l3_history.append((now, self.ctx.victron.load_l3_w))
                return

            if suffix in MPPT_YIELD_TODAY:
                self._mppt_today[suffix] = float(value)
                total = sum(self._mppt_today.values())
                self.ctx.victron.pv_yield_today_kwh = total
                return

            if suffix in MPPT_YIELD_YESTERDAY:
                self._mppt_yesterday[suffix] = float(value)
                total = sum(self._mppt_yesterday.values())
                self.ctx.victron.pv_yield_yesterday_kwh = total
                return

            if suffix == BATTERY_CHARGED_TOPIC:
                self.ctx.victron.battery_in_today_kwh = float(value)
                return
            if suffix == BATTERY_DISCHARGED_TOPIC:
                return

            # v4.3.1 NEW: cell voltages
            if suffix.startswith(BATTERY_CELL_V_PREFIX):
                cell_part = suffix[len(BATTERY_CELL_V_PREFIX):]
                if cell_part.startswith("Cell"):
                    try:
                        cell_num = int(cell_part[4:])
                        self.ctx.victron.cell_voltages[cell_num] = float(value)
                    except (ValueError, TypeError):
                        pass
                return

            if suffix == BATTERY_MIN_CELL_V:
                self.ctx.victron.min_cell_voltage_v = float(value)
                return
            if suffix == BATTERY_MAX_CELL_V:
                self.ctx.victron.max_cell_voltage_v = float(value)
                return
            if suffix == BATTERY_MIN_CELL_ID:
                self.ctx.victron.min_cell_id = int(value)
                return
            if suffix == BATTERY_MAX_CELL_ID:
                self.ctx.victron.max_cell_id = int(value)
                return

        except Exception as e:
            log.error(f"message parse error on {msg.topic}: {e}")

    async def _keepalive_loop(self):
        while True:
            try:
                self.client.publish(f"R/{self.portal_id}/keepalive", "")
                log.debug("keepalive sent")
            except Exception as e:
                log.warning(f"keepalive publish failed: {e}")
            await asyncio.sleep(self.keepalive_interval)

    async def start(self):
        log.info(f"Connecting to Cerbo MQTT at {self.host}:{self.port}")
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def stop(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        self.client.loop_stop()
        self.client.disconnect()
