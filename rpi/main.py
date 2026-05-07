"""
FridgeGuard — Main orchestrator.

Usage:
    python3 main.py
    python3 main.py --port /dev/ttyACM0
    python3 main.py --mock-ble        # no ESP32
    python3 main.py --mock-analyzer   # no Groq
"""

import argparse
import asyncio
import collections
import os
import sys
import time
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
from notifier      import Notifier
from serial_reader import SerialReader


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port",          type=str, default=None)
    p.add_argument("--baud",          type=int, default=115200)
    p.add_argument("--config",        type=str, default="config.yaml")
    p.add_argument("--mock-ble",      action="store_true")
    p.add_argument("--mock-analyzer", action="store_true")
    p.add_argument("--api-key",       type=str, default=None)
    return p.parse_args()


# ── Mock BLE ──────────────────────────────────────────────────────────────────

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
        self._scans: list      = []
        self._collecting: bool = False
        self._open_time: float = 0.0

    def start(self):
        self._scans      = []
        self._collecting = True
        self._open_time  = time.time()

    def stop(self) -> tuple[list, float]:
        self._collecting = False
        duration = time.time() - self._open_time
        return list(self._scans), duration

    def record(self, scan: BLEScan):
        if self._collecting:
            self._scans.append(scan)

    @property
    def collecting(self) -> bool:
        return self._collecting

    @property
    def open_duration(self) -> float:
        if not self._collecting:
            return 0.0
        return time.time() - self._open_time


# ── Identity resolution ───────────────────────────────────────────────────────

def resolve_best_identity(scans: list, resolver: BLEResolver,
                          config) -> ResolvedIdentity:
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

async def calibration_flasher(cam, display):
    if not display: return
    display.toggle_calibration_mode(True)
    
    while getattr(cam, 'calib_status', "") != "COMPLETE":
        status = getattr(cam, 'calib_status', "WAITING")
        
        if status == "WAITING":
            # This will trigger the flash logic in oled_display
            display.draw_flash_frame("WAITING", "NEEDS CALIBRATION", "OPEN THE DOOR")
            wait_time = 0.4 
        elif status == "CALIBRATING":
            # Solid white background
            display.draw_flash_frame("CALIBRATING", "CALIBRATING...", "STAY OPEN!")
            wait_time = 0.1 # No need to wait long, it's a solid frame
        elif status == "SUCCESS":
            # Thick border style
            display.draw_flash_frame("SUCCESS", "SUCCESS!", "CLOSE THE DOOR")
            wait_time = 0.1
        else:
            wait_time = 0.5

        await asyncio.sleep(wait_time)
        
    display.toggle_calibration_mode(False)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    args   = parse_args()
    config = load_config(args.config)

    if os.path.exists("fridgeguard.db"):
        print("[Main] Wiping old database for a fresh hackathon start...")
        os.remove("fridgeguard.db")

    db     = DB("fridgeguard.db")

    api_key = args.api_key or os.environ.get("GROQ_API_KEY")
    if not api_key and not args.mock_analyzer:
        print("[ERROR] No Groq API key. Pass --api-key or export GROQ_API_KEY.")
        print("        Or run with --mock-analyzer to skip API calls.")
        sys.exit(1)

    notifier     = Notifier(config)
    analyzer     = Analyzer(api_key=api_key or "mock")
    ble_resolver = BLEResolver(config)
    alert_engine = AlertEngine(
        analyzer     = analyzer,
        ble_resolver = ble_resolver,
        db           = db,
        config       = config,
        notifier     = notifier,
        min_api_gap  = 5.0,
    )

    port = args.port
    if not port and not args.mock_ble:
        port = SerialReader.find_esp32_port()
        if port:
            print(f"[Serial] Auto-detected ESP32 on {port}")
        else:
            print("[Serial] No ESP32 found. Use --mock-ble.")
            sys.exit(1)

    serial_reader = SerialReader(port=port or "/dev/ttyUSB0", baud=args.baud)
    ble_window    = BLEWindow()

    # ── Watchdog state ────────────────────────────────────────────────────────
    DOOR_OPEN_TIMEOUT = 120   # seconds before "door left open" alert
    TEMP_WINDOW_SEC   = 300   # 5 minutes of sustained high temp before alert
    temp_history      = collections.deque(maxlen=60)  # (timestamp, temp_f)
    door_alert_sent   = [False]   # mutable flag — reset on close
    temp_alert_sent   = [False]   # mutable flag — reset when temp normalises

    first_cycle_hack  = [True]

    # ── Camera callbacks ──────────────────────────────────────────────────────

    def on_open(before_frame: np.ndarray):
        ble_window.start()
        door_alert_sent[0] = False
        print("\n[Main] Door OPENED — collecting BLE scans...")
        notifier.status("Door OPENED")

    def on_close(before_frame: np.ndarray, after_frame: np.ndarray):
        scans, duration = ble_window.stop()
        identity        = resolve_best_identity(scans, ble_resolver, config)
        actor           = identity.primary or "GUEST"
        door_alert_sent[0] = False  # reset on close

        if first_cycle_hack[0]:
            print("\n[Main] 🪄 First cycle detected! Injecting pitch-black 'Before' frame...")
            # np.zeros_like creates an identical size array, but fills it with 0s (black)
            before_frame = np.zeros_like(after_frame)
            first_cycle_hack[0] = False

        alert_engine._executor.submit(
            notifier.upload_debug_frames, before_frame.copy(), after_frame.copy()
        )

        print(f"[Main] Door CLOSED — actor: {actor}, "
              f"scans: {len(scans)}, duration: {duration:.1f}s")

        if args.mock_analyzer:
            result  = analyzer.analyze_mock()
            changes = result.get("changes", [])
            notifier.groq_result(changes)
            notifier.door_closed(actor, len(scans), duration)
            for change in changes:
                alert_engine._evaluate(identity, change["item"], change["action"])
        else:
            alert_engine.on_door_close(
                before     = before_frame,
                after      = after_frame,
                identity   = identity,
                scan_count = len(scans),
                duration   = duration,
            )

    cam = Camera(
        on_open=on_open,
        on_close=on_close,
    )

    # ── Async tasks ───────────────────────────────────────────────────────────

    async def consume_ble():
        while True:
            scan = await serial_reader.ble_queue.get()
            ble_window.record(scan)
            #if ble_window.collecting:
            ble_resolver.resolve(scan)

    async def consume_temp():
        while True:
            reading = await serial_reader.temp_queue.get()
            try:
                db.log_temp(temp_f=reading.temp_f, humidity=reading.humidity)
                temp_history.append((time.time(), reading.temp_f))
                notifier.status(
                    f"Temp: {reading.temp_f:.1f} °F, {reading.humidity:.0f}% humidity"
                )
            except Exception as e:
                print(f"[Temp] Error: {e} — continuing")

    async def watchdog():
        """
        Runs every 30 seconds.
        Checks for:
          - Door left open too long (> DOOR_OPEN_TIMEOUT seconds)
          - Sustained high temperature (all readings in last TEMP_WINDOW_SEC
            above threshold)
        """
        while True:
            await asyncio.sleep(30)
            now = time.time()

            # ── Door left open ─────────────────────────────────────────────
            if ble_window.collecting:
                duration = ble_window.open_duration
                if duration > DOOR_OPEN_TIMEOUT and not door_alert_sent[0]:
                    door_alert_sent[0] = True
                    mins = duration / 60
                    notifier.status(f"Door open for {mins:.1f} min — alerting")
                    notifier.general(
                        f"🚪 The fridge door has been open for "
                        f"**{mins:.1f} minutes**! Please close it.",
                        color=notifier.COLORS["urgent"],
                    )

            # ── Sustained high temp ────────────────────────────────────────
            threshold = config.temperature.alert_threshold_f
            cutoff    = now - TEMP_WINDOW_SEC
            recent    = [(ts, t) for ts, t in temp_history if ts > cutoff]

            if len(recent) >= 3 and all(t > threshold for _, t in recent):
                if not temp_alert_sent[0]:
                    temp_alert_sent[0] = True
                    notifier.temp_alert(recent[-1][1], threshold)
            else:
                # Reset once temp normalises so future anomalies re-alert
                temp_alert_sent[0] = False

    notifier.status("FridgeGuard starting up")
    print()
    print("  FridgeGuard — Starting up")
    print("  ──────────────────────────────────────────────")
    print(f"  Config    : {args.config}")
    print(f"  BLE       : {'MOCK' if args.mock_ble else port}")
    print(f"  Analyzer  : {'MOCK' if args.mock_analyzer else 'Groq API'}")
    print(f"  Roommates : {[r.name for r in config.roommates]}")
    print(f"  DB        : fridgeguard.db")
    print(f"  Door timeout : {DOOR_OPEN_TIMEOUT}s")
    print(f"  Temp window  : {TEMP_WINDOW_SEC//60} min sustained > "
          f"{config.temperature.alert_threshold_f}°C")
    print()
    print("  Open fridge once to calibrate, then close.")
    print("  System goes live after calibration.")
    print("  Ctrl+C to stop.")
    print()

    tasks = [
        asyncio.create_task(cam.monitor_loop()),
        asyncio.create_task(consume_ble()),
        asyncio.create_task(consume_temp()),
        asyncio.create_task(watchdog()),
        asyncio.create_task(calibration_flasher(cam, serial_reader.display)),
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