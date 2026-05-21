"""
Husdata H66 WiFi Gateway - MQTT klient.

H66 publikuje vsechna data tepelneho cerpadla na MQTT topiky:
    {mac_addr}/HP/{registerId}    -> aktualni hodnoty (publish)
    {mac_addr}/HP/SET/{registerId} -> setovani (subscribe by H66, my publishujeme)
    {mac_addr}/HP/CMD             -> prikazy (STATUS, GETALL)

Konkretni register IDs zavisi na typu pumpy (Rego1000/2000/3000/etc).
Po instalaci H66 zjistis seznam dostupnych registru v jeho webovem interfacu.

KOMPATIBILITA:
- IVT AIR X 70 + Airmodul E9 pravdepodobne pouziva Rego1000 (CAN bus)
- Konkretni mapping doplnis v config.yaml -> heatpump.registers
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any

from paho.mqtt import client as mqtt_client

from ..state import SystemContext

log = logging.getLogger("h66")


class H66MqttSource:
    """MQTT klient pro Husdata H66 gateway.

    Konfigurace v config.yaml:

    heatpump:
      enabled: true
      h66_host: "10.0.0.150"        # IP H66 (jen pro reference)
      mqtt_host: "10.0.0.120"        # MQTT broker (Mosquitto na RPi)
      mqtt_port: 1883
      mqtt_user: ""                  # pokud broker pouziva auth
      mqtt_pass: ""
      mac_address: "841d2e82daf0"    # MAC adresa H66 (z jeho web UI)
      registers:                     # mapping ze stateu na register IDs
        outdoor_temp:    "0001"
        supply_temp:     "0002"
        return_temp:     "0003"
        hot_water_temp:  "0006"
        room_temp:       "0008"
        compressor:      "0100"
        add_heater:      "0101"
        operating_mode:  "0200"
        # ... atd. podle dokumentace pro konkretni Rego
      writable:                       # ktere registry SolarGuard smi menit
        target_hot_water:  "0006"     # napr. boost TUV
        room_setpoint:     "0008"
        block_add_heater:  "0101"     # blokace elektrickeho dohrevu
    """

    def __init__(
        self,
        mqtt_host: str,
        mqtt_port: int,
        mac_address: str,
        context: SystemContext,
        registers: Dict[str, str],
        writable: Optional[Dict[str, str]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        keepalive_minutes: int = 5,
    ):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mac = mac_address.lower().replace(":", "")
        self.ctx = context
        self.registers = registers          # name -> regId mapping
        self.writable = writable or {}
        self.keepalive_minutes = keepalive_minutes

        # Reverse mapping pro rychly lookup pri MQTT message
        self._reg_to_field = {v: k for k, v in registers.items()}

        self.client = mqtt_client.Client(
            mqtt_client.CallbackAPIVersion.VERSION2,
            client_id=f"solarguard-h66-{int(time.time())}",
            clean_session=True,
        )
        if username:
            self.client.username_pw_set(username, password or "")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self._keepalive_task: Optional[asyncio.Task] = None

    # ───── MQTT callbacks ─────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error(f"H66 MQTT connect failed, rc={rc}")
            return
        log.info(f"H66 MQTT connected to {self.mqtt_host}:{self.mqtt_port} (mac={self.mac})")

        # Subscribe vsechny registry
        for name, reg_id in self.registers.items():
            topic = f"{self.mac}/HP/{reg_id}"
            client.subscribe(topic)
            log.debug(f"  subscribed: {topic} ({name})")

        # Vyzadejte plny dump dat
        client.publish(f"{self.mac}/HP/CMD", "GETALL")

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        log.warning(f"H66 MQTT disconnected, rc={rc} (paho zajisti reconnect)")
        self.ctx.heatpump.online = False

    def _on_message(self, client, userdata, msg):
        try:
            # H66 muze publishovat raw cislo nebo JSON - zkusime obe
            payload_str = msg.payload.decode().strip()
            try:
                value = float(payload_str)
            except ValueError:
                # Mozna je to JSON {"value": x}
                try:
                    data = json.loads(payload_str)
                    value = data.get("value", payload_str)
                except Exception:
                    value = payload_str  # string jako "Heat", "Cool", "Off"

            # Topic format: {mac}/HP/{regId}
            parts = msg.topic.split("/")
            if len(parts) < 3:
                return
            reg_id = parts[-1]
            field_name = self._reg_to_field.get(reg_id)
            if not field_name:
                return

            self._apply_register(field_name, value)
            self.ctx.heatpump.last_update = time.time()
            self.ctx.heatpump.online = True
            self.ctx.heatpump.consecutive_failures = 0

        except Exception as e:
            log.error(f"H66 message parse error on {msg.topic}: {e}")

    def _apply_register(self, field_name: str, value: Any) -> None:
        """Mapuj nazev registru ze configu na konkretni pole HeatPumpData.

        Tady drzime semantiku - register "outdoor_temp" jde do `outdoor_temp_c`.
        """
        hp = self.ctx.heatpump
        # Pokud value je text reprezentace (boolean)
        if isinstance(value, str):
            v_low = value.lower().strip()
            if v_low in ("on", "1", "true", "yes"): value = True
            elif v_low in ("off", "0", "false", "no"): value = False

        try:
            if field_name == "outdoor_temp":     hp.outdoor_temp_c = float(value)
            elif field_name == "indoor_temp":    hp.indoor_temp_c = float(value)
            elif field_name == "supply_temp":    hp.supply_temp_c = float(value)
            elif field_name == "return_temp":    hp.return_temp_c = float(value)
            elif field_name == "hot_water_temp": hp.hot_water_temp_c = float(value)
            elif field_name == "room_temp":      hp.indoor_temp_c = float(value)
            elif field_name == "target_room_temp":      hp.target_room_temp_c = float(value)
            elif field_name == "target_supply_temp":    hp.target_supply_temp_c = float(value)
            elif field_name == "target_hot_water":      hp.target_hot_water_temp_c = float(value)
            elif field_name == "compressor":     hp.compressor_running = bool(value)
            elif field_name == "add_heater":     hp.additional_heater_active = bool(value)
            elif field_name == "block_add_heater": hp.additional_heater_blocked = bool(value)
            elif field_name == "fan_speed":      hp.fan_speed = float(value)
            elif field_name == "power":          hp.power_consumption_w = float(value)
            elif field_name == "energy_today":   hp.energy_today_kwh = float(value)
            elif field_name == "cop":            hp.cop_estimated = float(value)
            elif field_name == "operating_mode":
                # Muze byt ciselny enum nebo text
                if isinstance(value, (int, float)):
                    mode_map = {0: "off", 1: "heat", 2: "cool", 3: "hot_water"}
                    hp.operating_mode = mode_map.get(int(value), str(value))
                else:
                    hp.operating_mode = str(value).lower()
            elif field_name == "alarm":          hp.alarm_active = bool(value)
            elif field_name == "alarm_code":     hp.alarm_code = str(value) if value else None
            elif field_name == "heating_curve":  hp.heating_curve = float(value)
            else:
                log.debug(f"H66 unknown field: {field_name} = {value}")
        except (ValueError, TypeError) as e:
            log.warning(f"H66 type error for {field_name}={value}: {e}")

    # ───── Public API pro ovladani ─────

    async def set_register(self, name: str, value: Any) -> bool:
        """Nastavi hodnotu na heat pump pres MQTT SET topic.

        Pouziva self.writable mapping (nazev -> reg_id). Pokud register neni
        povoleny v configu, vraci False (bezpecnost - nelze omylem zmenit
        vsechny registry, jen ty co jsou explicitne v 'writable').
        """
        reg_id = self.writable.get(name)
        if not reg_id:
            log.warning(f"H66 set_register({name}): not in writable list")
            return False

        topic = f"{self.mac}/HP/SET/{reg_id}"
        try:
            self.client.publish(topic, str(value))
            log.info(f"H66 SET {name}({reg_id}) = {value}")
            return True
        except Exception as e:
            log.error(f"H66 SET {name} failed: {e}")
            return False

    async def request_full_dump(self) -> None:
        """Vyzadej plny re-publish vsech hodnot."""
        try:
            self.client.publish(f"{self.mac}/HP/CMD", "GETALL")
            log.debug("H66 GETALL requested")
        except Exception as e:
            log.warning(f"H66 GETALL failed: {e}")

    # ───── Lifecycle ─────

    async def _keepalive_loop(self):
        """Pravidelne vyzaduj GETALL aby data nebyla stale."""
        while True:
            await asyncio.sleep(self.keepalive_minutes * 60)
            try:
                await self.request_full_dump()
            except Exception as e:
                log.warning(f"H66 keepalive failed: {e}")

    async def start(self):
        log.info(f"H66 connecting to MQTT broker {self.mqtt_host}:{self.mqtt_port}")
        try:
            self.client.connect_async(self.mqtt_host, self.mqtt_port, keepalive=60)
            self.client.loop_start()
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        except Exception as e:
            log.error(f"H66 MQTT connect error: {e}")

    async def stop(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
