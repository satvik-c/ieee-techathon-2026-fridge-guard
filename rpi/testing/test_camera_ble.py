"""
FridgeGuard — test_camera_ble.py

Integration test: camera open/close detection + BLE identity resolution.
No Gemini/Groq calls — confirms that when the fridge opens and closes,
the system correctly identifies who was there from ESP32 BLE data.

Usage:
    python3 test_camera_ble.py
    python3 test_camera_ble.py --port /dev/ttyACM0
    python3 test_camera_ble.py --mock-ble   # fake BLE, no ESP32 needed

Flow:
    1. Open fridge once to calibrate camera
    2. Close it — system goes live
    3. Every subsequent open/close prints who was at the fridge
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from camera        import Camera
from serial_reader import SerialReader
from ble_resolver  import BLEResolver
from config_loader import load_config
from models        import BLEDevice, BLEScan, ResolvedIdentity


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port",     type=str, default=None,
                   help="Serial port for ESP32 (auto-detect if not specified)")
    p.add_argument("--baud",     type=int, default=115200)
    p.add_argument("--config",   type=str, default="config.yaml")
    p.add_argument("--mock-ble", action="store_true",
                   help="Simulate BLE data without ESP32")
    return p.parse_args()


# ── Mock BLE stream ───────────────────────────────────────────────────────────

async def mock_ble_stream(ble_queue: asyncio.Queue, config):
    """Cycles through registered UUIDs to simulate ESP32 output."""
    uuids = config.registered_uuids
    print(f"[MockBLE] Cycling through UUIDs: {uuids}")
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
    """Accumulates BLE scans while the door is open."""

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


# ── Output ────────────────────────────────────────────────────────────────────

def print_identity(identity: ResolvedIdentity, scans: list, resolver: BLEResolver):
    print()
    print("=" * 55)
    print("  IDENTITY RESOLUTION")
    print("=" * 55)
    if identity.is_guest:
        host = resolver.find_recent_nearby(window_sec=20)
        print(f"  Person    : GUEST / UNKNOWN")
        print(f"  Prob host : {host or 'none detected recently'}")
    else:
        print(f"  Person    : {identity.primary}")
        if identity.nearby:
            print(f"  Also near : {', '.join(identity.nearby)}")
    print(f"  BLE scans : {len(scans)} during open window")
    summary = resolver.get_sighting_summary(window_sec=20)
    if summary:
        print(f"  Last 20s  : {summary}")
    print("=" * 55)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args   = parse_args()
    config = load_config(args.config)

    resolver   = BLEResolver(config)
    ble_window = BLEWindow()
    cycle      = 0

    # Resolve serial port
    port = args.port
    if not port and not args.mock_ble:
        port = SerialReader.find_esp32_port()
        if port:
            print(f"[Serial] Auto-detected ESP32 on {port}")
        else:
            print("[Serial] No ESP32 found. Use --mock-ble to test without hardware.")
            sys.exit(1)

    serial_reader = SerialReader(port=port or "/dev/ttyUSB0", baud=args.baud)

    def on_open(before_frame: np.ndarray):
        nonlocal cycle
        cycle += 1
        print(f"\n[EVENT] Door OPENED — cycle #{cycle} — collecting BLE scans...")
        ble_window.start()

    def on_close(before_frame: np.ndarray, after_frame: np.ndarray):
        scans = ble_window.stop()
        print(f"[EVENT] Door CLOSED — {len(scans)} BLE scans in window")

        # Pick the best identity from all scans collected during this open
        best_identity = ResolvedIdentity(primary=None, is_guest=True, nearby=[])
        best_rssi     = -999

        for scan in scans:
            identity = resolver.resolve(scan)
            if not identity.is_guest:
                max_rssi = max(
                    (d.rssi for d in scan.devices
                     if config.resolve_uuid(d.uuid)),
                    default=-999,
                )
                if max_rssi > best_rssi:
                    best_rssi     = max_rssi
                    best_identity = identity

        print_identity(best_identity, scans, resolver)

    cam = Camera(
        on_open=on_open,
        on_close=on_close,
    )

    async def consume_ble():
        """
        Drain BLE queue.
        Always records to window (window ignores if door is closed).
        Only updates sighting history while door is open so closed-period
        scans don't contaminate guest/host attribution.
        """
        while True:
            scan = await serial_reader.ble_queue.get()
            ble_window.record(scan)
            if ble_window.collecting:
                resolver.resolve(scan)

    print()
    print("  FridgeGuard — Camera + BLE Integration Test")
    print("  ─────────────────────────────────────────────")
    print(f"  Config    : {args.config}")
    print(f"  BLE mode  : {'MOCK' if args.mock_ble else port}")
    print(f"  Roommates : {[r.name for r in config.roommates]}")
    print(f"  UUIDs     : {config.registered_uuids}")
    print()
    print("  Step 1: Open fridge once to calibrate camera.")
    print("  Step 2: Close it — system goes live.")
    print("  Step 3: Open/close normally — identity prints on each close.")
    print("  Ctrl+C to quit.")
    print()

    async def run():
        tasks = [
            asyncio.create_task(cam.monitor_loop()),
            asyncio.create_task(consume_ble()),
        ]
        if args.mock_ble:
            tasks.append(asyncio.create_task(
                mock_ble_stream(serial_reader.ble_queue, config)
            ))
        else:
            tasks.append(asyncio.create_task(serial_reader.stream()))

        await asyncio.gather(*tasks)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[EXIT] Stopped.")
    finally:
        cam.release()
        serial_reader.close()


if __name__ == "__main__":
    main()