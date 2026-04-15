"""
Phase 4 — Serial reader test.
    python3 test_phase4.py --unit
    python3 test_phase4.py --live
    python3 test_phase4.py --detect
"""

import argparse
import asyncio
import json
import sys
import time


def test_unit():
    print("=" * 55)
    print("TEST: Serial reader (parsing logic)")
    print("=" * 55)

    from serial_reader import SerialReader
    reader = SerialReader(port="/dev/null", baud=115200)

    print("\n  Test 1: BLE scan with UUID")
    reader._parse_line(json.dumps({"type": "ble", "ts": 100,
                                    "devices": [{"uuid": "ff01", "rssi": -42}]}))
    scan = reader.ble_queue.get_nowait()
    assert scan.devices[0].uuid == "ff01"
    print(f"    ff01, rssi=-42 ✓")

    print("\n  Test 2: Two devices")
    reader._parse_line(json.dumps({"type": "ble", "ts": 105, "devices": [
        {"uuid": "ff01", "rssi": -45}, {"uuid": "ff02", "rssi": -60}
    ]}))
    scan = reader.ble_queue.get_nowait()
    assert len(scan.devices) == 2
    print(f"    {[d.uuid for d in scan.devices]} ✓")

    print("\n  Test 3: Empty scan (guest)")
    reader._parse_line(json.dumps({"type": "ble", "ts": 110, "devices": []}))
    scan = reader.ble_queue.get_nowait()
    assert len(scan.devices) == 0
    print(f"    guest ✓")

    print("\n  Test 4: Temperature")
    reader._parse_line(json.dumps({"type": "temp", "temp_c": 5.5, "humidity": 72, "ts": 115}))
    reading = reader.temp_queue.get_nowait()
    assert reading.temp_c == 5.5
    print(f"    {reading.temp_c}°C ✓")

    print("\n  Test 5: Garbage ignored")
    before = reader.ble_queue.qsize()
    reader._parse_line("ESP32 boot garbage")
    reader._parse_line("")
    assert reader.ble_queue.qsize() == before
    print(f"    ✓")

    print("\n  Test 6: UUID → roommate")
    from config_loader import load_config
    config = load_config("config.yaml")
    assert config.resolve_uuid("ff01") == "Alex"
    assert config.resolve_uuid("ff99") is None
    print(f"    ff01→Alex, ff99→None ✓")

    print("\n  ✓ All unit tests passed!\n")


def test_detect():
    from serial_reader import SerialReader
    print("=" * 55)
    print("Auto-detect ESP32 port")
    print("=" * 55)
    port = SerialReader.find_esp32_port()
    if port:
        print(f"  Found: {port}")
    else:
        print("  Not found. Available:")
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            print(f"    {p.device} — {p.description}")


async def test_live_async(port, baud, duration):
    from serial_reader import SerialReader
    from config_loader import load_config

    config = load_config("config.yaml")
    reader = SerialReader(port=port, baud=baud)
    stream_task = asyncio.create_task(reader.stream())

    ble_count = temp_count = 0
    start = time.time()

    try:
        while time.time() - start < duration:
            while not reader.ble_queue.empty():
                scan = reader.ble_queue.get_nowait()
                ble_count += 1
                if scan.devices:
                    parts = [f"{config.resolve_uuid(d.uuid) or '???'}[{d.uuid}]({d.rssi})"
                             for d in scan.devices]
                    print(f"\n  [BLE #{ble_count}] {', '.join(parts)}")
                else:
                    print(f"\n  [BLE #{ble_count}] (guest)")
            while not reader.temp_queue.empty():
                r = reader.temp_queue.get_nowait()
                temp_count += 1
                print(f"\n  [Temp #{temp_count}] {r.temp_c}°C")
            sys.stdout.write(f"\r  {int(time.time()-start)}s/{duration}s  BLE:{ble_count} Temp:{temp_count}  ")
            sys.stdout.flush()
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
        stream_task.cancel()
        try: await stream_task
        except asyncio.CancelledError: pass
    print(f"\n  Done. {ble_count} BLE, {temp_count} temp.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--unit", action="store_true")
    group.add_argument("--live", action="store_true")
    group.add_argument("--detect", action="store_true")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--duration", type=int, default=30)
    args = parser.parse_args()

    if args.unit: test_unit()
    elif args.detect: test_detect()
    elif args.live: asyncio.run(test_live_async(args.port, args.baud, args.duration))