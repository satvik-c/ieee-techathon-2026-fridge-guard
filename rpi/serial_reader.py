"""
FridgeGuard — Serial reader.

Reads JSON lines from the ESP32 over USB serial and routes them
into async queues for downstream consumers.

ESP32 serial protocol (one JSON object per line):
  BLE scan:   {"type":"ble","ts":12345,"devices":[{"uuid":"ff01","rssi":-42}]}
  Temp read:  {"type":"temp","temp_c":6.0,"humidity":45,"ts":12345}

The "uuid" field is a 16-bit service UUID (hex string, lowercase)
that each roommate broadcasts via nRF Connect.
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

import serial
import serial.tools.list_ports

from models import BLEDevice, BLEScan, TempReading


class SerialReader:
    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200):
        self.port = port
        self.baud = baud
        self.ble_queue: asyncio.Queue[BLEScan] = asyncio.Queue()
        self.temp_queue: asyncio.Queue[TempReading] = asyncio.Queue()
        self._serial: Optional[serial.Serial] = None
        self._running = False

    def _open(self):
        self._serial = serial.Serial(port=self.port, baudrate=self.baud, timeout=1)
        self._serial.reset_input_buffer()
        print(f"[Serial] Opened {self.port} at {self.baud} baud")

    def _parse_line(self, line: str) -> Optional[str]:
        line = line.strip()
        if not line:
            return None

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None

        msg_type = msg.get("type")
        ts = datetime.now()

        if msg_type == "ble":
            devices = [
                BLEDevice(uuid=d["uuid"].lower(), rssi=d["rssi"])
                for d in msg.get("devices", [])
            ]
            self.ble_queue.put_nowait(BLEScan(timestamp=ts, devices=devices))
            return "ble"

        elif msg_type == "temp":
            self.temp_queue.put_nowait(TempReading(
                timestamp=ts,
                temp_c=msg.get("temp_c", 0.0),
                humidity=msg.get("humidity", 0.0),
            ))
            return "temp"

        return None

    async def stream(self):
        self._running = True
        while self._running:
            try:
                self._open()
                while self._running:
                    line = await asyncio.get_event_loop().run_in_executor(
                        None, self._serial.readline
                    )
                    if line:
                        try:
                            decoded = line.decode("utf-8", errors="replace")
                        except Exception:
                            continue
                        self._parse_line(decoded)
            except serial.SerialException as e:
                print(f"[Serial] Connection error: {e}")
                print(f"[Serial] Retrying in 3 seconds...")
                self.close()
                await asyncio.sleep(3)
            except Exception as e:
                print(f"[Serial] Unexpected error: {e}")
                self.close()
                await asyncio.sleep(3)

    def close(self):
        self._running = False
        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None

    @staticmethod
    def find_esp32_port() -> Optional[str]:
        ports = serial.tools.list_ports.comports()
        for p in ports:
            desc = (p.description or "").lower()
            mfr = (p.manufacturer or "").lower()
            if any(kw in desc for kw in ["cp210", "ch340", "ftdi", "usb serial", "usb-serial"]):
                return p.device
            if any(kw in mfr for kw in ["silicon labs", "wch", "ftdi", "espressif"]):
                return p.device
        return None