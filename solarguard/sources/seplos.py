"""
Seplos BMS V3.0 - RS485 Modbus RTU driver.

Seplos BMS broadcastuje Modbus RTU rámce kontinuálně na 19200 baud.
Rámec pro pack N: {N} 04 {byte_count} [data] [CRC2]
Kde data = 16 napětí článků (mV) + 4 teploty (0.1K) + souhrn packu.

Zapojení: USB-RS485 adaptér -> RS485 port na Seplos BMS.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import List, Optional

from ..state import SystemContext

log = logging.getLogger("seplos")

# Počet extra registrů za články: 4 teploty + voltage + current + SOC + SOH + 2 rezerva
EXTRA_REGS = 10

def _crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack('<H', crc)


class SeplosRS485:
    def __init__(
        self,
        port: str,
        pack_count: int,
        cells_per_pack: int,
        context: SystemContext,
        baudrate: int = 19200,
        poll_interval_sec: int = 30,
        reg_cell_start: int = 0x1100,
    ):
        self.port = port
        self.pack_count = pack_count
        self.cells_per_pack = cells_per_pack
        self.ctx = context
        self.baudrate = baudrate
        self.poll_interval = poll_interval_sec
        self.reg_cell_start = reg_cell_start
        self._ser = None
        self._task: Optional[asyncio.Task] = None

        # Počet registrů na pack a velikost Modbus rámce
        self._reg_count = cells_per_pack + EXTRA_REGS
        self._data_bytes = self._reg_count * 2
        self._frame_size = 3 + self._data_bytes + 2  # header + data + CRC

    # ── Sériový port ────────────────────────────────────────────────────────

    def _open(self):
        import serial
        self._ser = serial.Serial(
            self.port, self.baudrate,
            bytesize=8, parity='N', stopbits=1,
            timeout=0.1,
        )
        log.info(f"Seplos RS485 otevren: {self.port} @ {self.baudrate} baud")

    # ── Čtení jednoho packu ─────────────────────────────────────────────────

    def _read_pack(self, pack_addr: int) -> Optional[dict]:
        """
        Pošle Modbus dotaz a v příchozím streamu najde validní rámec pro daný pack.
        BMS broadcastuje rámce kontinuálně — hledáme vzor: {addr} 04 {byte_count}.
        """
        req = struct.pack('>BBHH', pack_addr, 0x04, self.reg_cell_start, self._reg_count)
        req += _crc16(req)

        # Trigger dotazem (BMS někdy odpovídá rychleji po dotazu)
        try:
            self._ser.reset_input_buffer()
            self._ser.write(req)
        except Exception as e:
            log.error(f"serial write error: {e}")
            return None

        # Hledáme rámec v streamu po dobu 1.5 sekundy
        target = bytes([pack_addr, 0x04, self._data_bytes])
        buf = b''
        deadline = time.time() + 1.5

        while time.time() < deadline:
            try:
                chunk = self._ser.read(self._frame_size + 64)
            except Exception as e:
                log.error(f"serial read error: {e}")
                return None

            if chunk:
                buf += chunk

            # Hledej platný Modbus rámec
            pos = 0
            while pos <= len(buf) - self._frame_size:
                idx = buf.find(target, pos)
                if idx < 0:
                    break
                frame = buf[idx:idx + self._frame_size]
                if len(frame) == self._frame_size:
                    if _crc16(frame[:-2]) == frame[-2:]:
                        log.debug(f"Pack {pack_addr}: platný rámec nalezen na offset {idx}")
                        return self._parse_frame_data(frame[3:-2])
                pos = idx + 1

            # Udržuj buffer rozumné velikosti
            if len(buf) > 512:
                buf = buf[-256:]

        log.warning(f"Pack {pack_addr:#04x}: rámec nenalezen do timeoutu")
        return None

    def _parse_frame_data(self, data: bytes) -> dict:
        """Parsuje data za hlavickou Modbus ramce.

        Struktura ramce neni dokumentovana stejne pro vsechny verze Seplos V3.
        Puvodni parser s fixnim offsetem cetl teplotni offset misto pack_voltage
        (vystup: pack 27.31V misto skutecnych 55V, SOC 273% misto realnyh procent).

        Tato verze:
        - cell_voltages: prvnich cells_per_pack * 2B (overeno - vzdy spravne)
        - temperatures: scanujeme dokud nenarazime na hodnotu mimo rozsah
          2700-3500 (=  -3 az +77 C). Bezne 4 nebo 6 NTC.
        - pack_voltage: SUMA clanku (vzdy spravne pro seriove zapojeni);
          alternativne validni raw v rozsahu 40-60V
        - current/SOC/SOH: validace proti rozumnym rozsahum, jinak None
        - DEBUG: kazdy 60. ramec hex dump zbylych bytu pro analyzu protokolu
        """
        # Napětí článků (overeno spravne)
        cell_voltages = [
            struct.unpack_from('>H', data, i * 2)[0] / 1000.0
            for i in range(self.cells_per_pack)
        ]

        # Teploty: scanujeme od konce cell napeti, hledame raw 2700-3500
        t_off = self.cells_per_pack * 2
        temperatures = []
        temp_end_off = t_off
        max_temp_sensors = 8
        for i in range(max_temp_sensors):
            pos = t_off + i * 2
            if pos + 2 > len(data):
                break
            raw = struct.unpack_from('>H', data, pos)[0]
            if 2700 <= raw <= 3500:  # validni teplota v 0.1K (= -3 az 77 C)
                temperatures.append(round((raw - 2731) / 10.0, 1))
                temp_end_off = pos + 2
            else:
                break

        # Pack voltage = SUMA clanku (vzdy spravne pri serioveho zapojeni)
        computed_pack_voltage = round(sum(cell_voltages), 3)
        pack_voltage = computed_pack_voltage  # default = computed

        # Zkusime najit summary fields v bytech za teplotami
        current = None
        soc = None
        soh = None
        summary_bytes = data[temp_end_off:]

        # DEBUG: kazdy 5. ramec dump zbyvajicich bytu pro analyzu protokolu
        # (po vyladeni offsetu lze snizit na 60+)
        if not hasattr(self, '_dbg_counter'):
            self._dbg_counter = 0
        self._dbg_counter += 1
        if self._dbg_counter % 5 == 0 and summary_bytes:
            full_hex = ' '.join(f'{b:02x}' for b in summary_bytes)
            log.info(f"Seplos {len(temperatures)}temps end_off={temp_end_off} sumBytes({len(summary_bytes)}): {full_hex}")
            # Vystaveni do attributu - muze cist API endpoint
            self._last_summary_hex = full_hex
            self._last_summary_bytes = bytes(summary_bytes)
            self._last_temp_count = len(temperatures)

        # Hledame validni pack voltage (40-60V pro 16S LiFePO4) v summary
        for off in range(0, min(len(summary_bytes), 10), 2):
            if off + 2 > len(summary_bytes):
                break
            raw_v = struct.unpack_from('>H', summary_bytes, off)[0]
            v_test = raw_v / 100.0
            if 40.0 <= v_test <= 60.0:
                # Validni - prepiseme computed pack_voltage z BMS hodnoty
                # (kontrola jestli neni vetsi nez 5% odchylka od soucty clanku)
                if abs(v_test - computed_pack_voltage) <= computed_pack_voltage * 0.05:
                    pack_voltage = v_test
                # Hned za pack_voltage byva current (signed 16b, /100)
                if off + 4 <= len(summary_bytes):
                    cur_raw = struct.unpack_from('>H', summary_bytes, off + 2)[0]
                    if cur_raw > 32767:
                        cur_raw -= 65536
                    cur_test = cur_raw / 100.0
                    if -300.0 <= cur_test <= 300.0:
                        current = cur_test
                # SOC za current (0-100%, zkusime /10 i /100)
                if off + 6 <= len(summary_bytes):
                    soc_raw = struct.unpack_from('>H', summary_bytes, off + 4)[0]
                    if 0 <= soc_raw <= 1000:    # /10
                        soc = soc_raw / 10.0
                    elif 0 <= soc_raw <= 10000:  # /100
                        soc = soc_raw / 100.0
                # SOH za SOC
                if off + 8 <= len(summary_bytes):
                    soh_raw = struct.unpack_from('>H', summary_bytes, off + 6)[0]
                    if 0 <= soh_raw <= 1100:
                        soh = soh_raw / 10.0
                break

        return {
            'cell_voltages': cell_voltages,
            'temperatures': temperatures,
            'pack_voltage': pack_voltage,
            'current': current,
            'soc': soc,
            'soh': soh,
        }

    # ── Polling loop ─────────────────────────────────────────────────────────

    def _poll(self):
        if self._ser is None or not self._ser.is_open:
            try:
                self._open()
            except Exception as e:
                log.error(f"nelze otevrit {self.port}: {e}")
                self.ctx.seplos.online = False
                return

        pack_cells, pack_temps, pack_v, pack_cur, pack_soc, pack_soh = [], [], [], [], [], []
        ok = 0

        for i in range(self.pack_count):
            addr = i + 1
            result = self._read_pack(addr)
            if result:
                pack_cells.append(result['cell_voltages'])
                pack_temps.append(result.get('temperatures', []))
                pack_v.append(result.get('pack_voltage'))
                pack_cur.append(result.get('current'))
                pack_soc.append(result.get('soc'))
                pack_soh.append(result.get('soh'))
                ok += 1
            else:
                pack_cells.append([])
                pack_temps.append([])
                pack_v.append(None); pack_cur.append(None)
                pack_soc.append(None); pack_soh.append(None)

        s = self.ctx.seplos
        s.pack_cell_voltages = pack_cells
        s.pack_temperatures = pack_temps
        s.pack_voltages = pack_v
        s.pack_currents = pack_cur
        s.pack_soc = pack_soc
        s.pack_soh = pack_soh
        s.online = ok > 0
        s.last_update = time.time()

        # Agregovaný min/max přes všechny packy
        flat = [
            (pi, ci, v)
            for pi, cells in enumerate(pack_cells)
            for ci, v in enumerate(cells)
            if v and v > 0
        ]
        if flat:
            mn = min(flat, key=lambda x: x[2])
            mx = max(flat, key=lambda x: x[2])
            s.min_cell_voltage = mn[2]
            s.max_cell_voltage = mx[2]
            s.min_cell_pack  = mn[0] + 1
            s.min_cell_index = mn[1] + 1
            s.max_cell_pack  = mx[0] + 1
            s.max_cell_index = mx[1] + 1
            spread_mv = (mx[2] - mn[2]) * 1000
            log.info(
                f"Seplos {ok}/{self.pack_count} pack OK | "
                f"min={mn[2]:.3f}V (P{mn[0]+1}C{mn[1]+1}) "
                f"max={mx[2]:.3f}V (P{mx[0]+1}C{mx[1]+1}) "
                f"spread={spread_mv:.0f}mV"
            )
        else:
            log.warning("Seplos: zadna napeti clanku")

    async def _loop(self):
        loop = asyncio.get_event_loop()
        while True:
            try:
                await loop.run_in_executor(None, self._poll)
            except Exception as e:
                log.error(f"Seplos poll vyjimka: {e}")
                self.ctx.seplos.online = False
            await asyncio.sleep(self.poll_interval)

    async def start(self):
        log.info(
            f"Seplos RS485 starting: port={self.port} baud={self.baudrate} "
            f"{self.pack_count} pack × {self.cells_per_pack}S "
            f"frame={self._frame_size}B poll={self.poll_interval}s"
        )
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._ser and self._ser.is_open:
            self._ser.close()
