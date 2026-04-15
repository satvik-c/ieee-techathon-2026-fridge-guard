"""
FridgeGuard — Main orchestrator.

Runs all subsystems concurrently:
  - Camera (open/close detection + before/after frames)
  - Serial reader (ESP32 BLE + temp data)
  - BLE window collector (tracks who's at fridge during open)
  - Alert engine (Groq analysis + scenario A/B/C + DB logging)

Usage:
    python3 main.py
    python3 main.py --port /dev/ttyACM0
    python3 main.py --mock-ble        # no ESP32, fake BLE data
    python3 main.py --mock-analyzer   # no Groq, fake analysis
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from alert_engine  import AlertEngine
from analyzer      import Analyzer
from ble_resolver  import BLEResolver
from camera        import Camera
from config_loader import load_config
from db            import DB
from models        import BLEDevice, BLEScan, ResolvedIdentity
from serial_reader import SerialReader


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port",          type=str, default=None,
                   help="Serial port for ESP32 (auto-detect if not specified)")
    p.add_argument("--baud",          type=int, default=115200)
    p.add_argument("--config",        type=str, default="config.yaml")
    p.add_argument("--mock-ble",      action="store_true",
                   help="Simulate BLE data, no ESP32 needed")
    p.add_argument("--mock-analyzer", action="store_true",
                   help="Use fake Groq responses, no API calls")
    p.add_argument("--api-key",       type=str, default=None,
                   help="Groq API key (or set GROQ_API_KEY env var)")
    return p.parse_args()


# ── Mock BLE stream ───────────────────────────────────────────────────────────

async def mock_ble_stream(ble_queue: asyncio.Queue, config):
    uuids = config.registered_uuids
    print(f"[MockBLE] Cycling through: {uuids}")
    i = 0
    while True:
        uuid = uuids[i % len(uuids)]
        await ble_queue.put(BLEScan(
            timestamp=datetime.now(),
            devices=[BLEDevice(uuid=uuid, rssi=-55)],
        ))
        await asyncio.sleep(2)
        i += 1


# ── BLE window ────────────────────────────────────────────────────────────────

class BLEWindow:
    def __init__(self):
        self._scans: list = []
        self._collecting  = False

    def start(self):
        self._scans      = []
        self._collecting = True

    def stop(self) -> list:
        self._collecting = False
        return list(self._scans)

    def record(self, scan: BLEScan):
        if self._collecting:
            self._scans.append(scan)

    @property
    def collecting(self) -> bool:
        return self._collecting


# ── Identity resolution ───────────────────────────────────────────────────────

def resolve_best_identity(scans: list, resolver: BLEResolver,
                          config) -> ResolvedIdentity:
    """Pick the strongest-signal identity from all scans in the open window."""
    best_identity = ResolvedIdentity(primary=None, is_guest=True, nearby=[])
    best_rssi     = -999

    for scan in scans:
        identity = resolver.resolve(scan)
        if not identity.is_guest:
            max_rssi = max(
                (d.rssi for d in scan.devices if config.resolve_uuid(d.uuid)),
                default=-999,
            )
            if max_rssi > best_rssi:
                best_rssi     = max_rssi
                best_identity = identity

    return best_identity


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    args   = parse_args()
    config = load_config(args.config)
    db     = DB("fridgeguard.db")

    api_key = args.api_key or os.environ.get("GROQ_API_KEY")
    if not api_key and not args.mock_analyzer:
        print("[ERROR] No Groq API key. Pass --api-key or export GROQ_API_KEY.")
        print("        Or run with --mock-analyzer to skip API calls.")
        sys.exit(1)

    analyzer     = Analyzer(api_key=api_key or "mock")
    ble_resolver = BLEResolver(config)
    alert_engine = AlertEngine(
        analyzer     = analyzer,
        ble_resolver = ble_resolver,
        db           = db,
        config       = config,
        min_api_gap  = 5.0,
    )

    # Resolve serial port
    port = args.port
    if not port and not args.mock_ble:
        port = SerialReader.find_esp32_port()
        if port:
            print(f"[Serial] Auto-detected ESP32 on {port}")
        else:
            print("[Serial] No ESP32 found. Use --mock-ble to run without hardware.")
            sys.exit(1)

    serial_reader = SerialReader(port=port or "/dev/ttyUSB0", baud=args.baud)
    ble_window    = BLEWindow()

    def on_open(before_frame: np.ndarray):
        print("\n[Main] Door OPENED — collecting BLE scans...")
        ble_window.start()

    def on_close(before_frame: np.ndarray, after_frame: np.ndarray):
        scans    = ble_window.stop()
        identity = resolve_best_identity(scans, ble_resolver, config)

        actor = identity.primary or "GUEST"
        print(f"[Main] Door CLOSED — actor: {actor}, "
              f"BLE scans: {len(scans)} — handing off to alert engine...")

        if args.mock_analyzer:
            result = analyzer.analyze_mock()
            changes = result.get("changes", [])
            print(f"[Main] Mock analysis: {len(changes)} change(s)")
            for change in changes:
                alert_engine._evaluate(identity, change["item"], change["action"])
        else:
            alert_engine.on_door_close(before_frame, after_frame, identity)

    cam = Camera(
        on_open=on_open,
        on_close=on_close,
    )

    async def consume_ble():
        while True:
            scan = await serial_reader.ble_queue.get()
            ble_window.record(scan)
            if ble_window.collecting:
                ble_resolver.resolve(scan)

    async def consume_temp():
        while True:
            reading = await serial_reader.temp_queue.get()
            db.log_temp(temp_c=reading.temp_c, humidity=reading.humidity)
            print(f"[Temp] {reading.temp_c:.1f}°C, {reading.humidity:.0f}% humidity")

    print()
    print("  FridgeGuard — Starting up")
    print("  ──────────────────────────────────────────────")
    print(f"  Config    : {args.config}")
    print(f"  BLE       : {'MOCK' if args.mock_ble else port}")
    print(f"  Analyzer  : {'MOCK' if args.mock_analyzer else 'Groq API'}")
    print(f"  Roommates : {[r.name for r in config.roommates]}")
    print(f"  DB        : fridgeguard.db")
    print()
    print("  Open fridge once to calibrate, then close.")
    print("  System goes live after calibration.")
    print("  Ctrl+C to stop.")
    print()

    tasks = [
        asyncio.create_task(cam.monitor_loop()),
        asyncio.create_task(consume_ble()),
        asyncio.create_task(consume_temp()),
    ]

    if args.mock_ble:
        tasks.append(asyncio.create_task(
            mock_ble_stream(serial_reader.ble_queue, config)
        ))
    else:
        tasks.append(asyncio.create_task(serial_reader.stream()))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Main] Stopped.")