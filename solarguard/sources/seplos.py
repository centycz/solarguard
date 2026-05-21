"""
Seplos BMS V3.0 - RS485 Modbus RTU driver.

Cte napeti vsech clanku, teploty a souhrnna data z kazdeho pack pres USB-RS485.
Seplos V3.0 pouziva standardni Modbus RTU (9600 8N1).

Zapojeni: USB-RS485 adaptér -> RS485 port na Seplos BMS (spodni konektor).
Vice pack: vsechny jsou na jedne sbernici, adresovane 0x01, 0x02, ...

Register mapa (FC=0x04, Input Registers):
  0x1100 + n  : napeti clanku n (n=0..15), jednotka: 1 mV
  0x1110..13  : teploty 4 cidel, jednotka: 0.1 K  (prepocet: (raw-2731)/10 = °C)
  0x1120      : celkove napeti packu, 0.01 V
  0x1121      : proud signed int16, 0.01 A (kladne = nabijeni)
  0x1122      : SOC, 0.1 %
  0x1123      : SOH, 0.1 %
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import List, Optional

from ..state import SystemContext

log = logging.getLogger("seplos")

# Vychozi register adresy (Seplos V3.0 dokumentace + komunita)
REG_CELL_V_START = 0x1100
REG_TEMPS_START  = 0x1110
REG_PACK_SUMMARY = 0x1120   # voltage, current, SOC, SOH
TEMPS_COUNT      = 4
SUMMARY_COUNT    = 4


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
        baudrate: int = 9600,
        poll_interval_sec: int = 30,
        reg_cell_start: int = REG_CELL_V_START,
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

    # ── Modbus RTU low-level ─────────────────────────────────────────────────

    def _open(self):
        import serial
        self._ser = serial.Serial(
            self.port, self.baudrate,
            bytesize=8, parity='N', stopbits=1,
            timeout=1.0,
        )
        log.info(f"Seplos RS485 otevren: {self.port} @ {self.baudrate} baud")

    def _read_input_regs(self, addr: int, start: int, count: int) -> Optional[List[int]]:
        """Modbus RTU FC=0x04 read input registers."""
        req = struct.pack('>BBHH', addr, 0x04, start, count)
        req += _crc16(req)
        try:
            self._ser.reset_input_buffer()
            self._ser.write(req)
            expected = 3 + count * 2 + 2
            resp = self._ser.read(expected)
        except Exception as e:
            log.error(f"serial chyba: {e}")
            return None

        if len(resp) < 3 + count * 2 + 2:
            log.warning(f"kratka odpoved: {len(resp)}B od addr {addr:#04x}, start={start:#06x}")
            return None
        if resp[0] != addr or resp[1] != 0x04:
            log.warning(f"neocekavana odpoved: {resp[:3].hex()} (addr={addr:#04x})")
            return None
        if _crc16(resp[:-2]) != resp[-2:]:
            log.warning("CRC chyba v odpovedi")
            return None

        return [struct.unpack_from('>H', resp, 3 + i * 2)[0] for i in range(count)]

    # ── Pack reading ─────────────────────────────────────────────────────────

    def _read_pack(self, pack_addr: int) -> Optional[dict]:
        # 1. Napeti clanku
        cell_regs = self._read_input_regs(pack_addr, self.reg_cell_start, self.cells_per_pack)
        if cell_regs is None:
            return None
        cell_voltages = [v / 1000.0 for v in cell_regs]   # mV -> V

        # 2. Teploty (4 cidla, 0.1 K -> °C)
        temp_regs = self._read_input_regs(pack_addr, REG_TEMPS_START, TEMPS_COUNT)
        temperatures = []
        if temp_regs:
            for raw in temp_regs:
                if raw > 0:
                    temperatures.append(round((raw - 2731) / 10.0, 1))

        # 3. Souhrnne hodnoty packu
        summary = self._read_input_regs(pack_addr, REG_PACK_SUMMARY, SUMMARY_COUNT)
        pack_voltage = summary[0] / 100.0 if summary else None

        current = None
        if summary and len(summary) > 1:
            raw = summary[1]
            if raw > 32767:
                raw -= 65536          # signed int16
            current = raw / 100.0

        soc = summary[2] / 10.0 if summary and len(summary) > 2 else None
        soh = summary[3] / 10.0 if summary and len(summary) > 3 else None

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
            addr = i + 1    # pack 1 = 0x01, pack 2 = 0x02, ...
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
                log.warning(f"Pack {i+1} (addr {addr:#04x}) neodpovedel")
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

        # Agregat min/max
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
            s.min_cell_pack    = mn[0] + 1
            s.min_cell_index   = mn[1] + 1
            s.max_cell_pack    = mx[0] + 1
            s.max_cell_index   = mx[1] + 1
            spread_mv = (mx[2] - mn[2]) * 1000
            log.info(
                f"Seplos {ok}/{self.pack_count} pack | "
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
            f"Seplos RS485 starting: port={self.port} "
            f"{self.pack_count} pack x {self.cells_per_pack}S "
            f"poll={self.poll_interval}s"
        )
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._ser and self._ser.is_open:
            self._ser.close()
