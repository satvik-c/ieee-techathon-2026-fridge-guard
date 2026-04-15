"""
Integration test — Serial + BLE resolver (no camera, no Gemini).
    python3 test_integration.py
    python3 test_integration.py --port /dev/ttyACM0
"""

import argparse
import asyncio
import sys
import time

from config_loader import load_config
from serial_reader import SerialReader
from ble_resolver import BLEResolver


async def run(port, baud, duration):
    config = load_config("config.yaml")
    reader = SerialReader(port=port, baud=baud)
    resolver = BLEResolver(config)

    print("=" * 60)
    print("FridgeGuard — Integration Test (Serial + BLE)")
    print(f"  Port: {port}   Duration: {duration}s")
    print(f"  UUIDs: {config.registered_uuids}")
    print(f"  RSSI floor: {config.ble.rssi_floor}")
    print("=" * 60)
    print("  Ctrl+C to stop.\n")

    stream_task = asyncio.create_task(reader.stream())
    ble_count = temp_count = 0
    start = time.time()

    try:
        while time.time() - start < duration:
            while not reader.ble_queue.empty():
                scan = reader.ble_queue.get_nowait()
                ble_count += 1
                identity = resolver.resolve(scan)

                raw = ", ".join(f"{d.uuid}({d.rssi})" for d in scan.devices) or "(empty)"
                if identity.is_guest:
                    id_str = "GUEST"
                    host = resolver.find_recent_nearby(60)
                    if host:
                        id_str += f" (probable host: {host})"
                else:
                    id_str = identity.primary
                    if identity.nearby:
                        id_str += f" (nearby: {', '.join(identity.nearby)})"

                print(f"  [BLE #{ble_count:3d}]  {raw}  →  {id_str}")

            while not reader.temp_queue.empty():
                r = reader.temp_queue.get_nowait()
                temp_count += 1
                print(f"  [Temp #{temp_count:3d}]  {r.temp_c}°C, {r.humidity}%")

            summary = resolver.get_sighting_summary(30)
            s = ", ".join(f"{k}:{v}" for k, v in summary.items()) or "none"
            sys.stdout.write(f"\r  [{int(time.time()-start)}s] BLE:{ble_count} Temp:{temp_count} Last30s:{s}   ")
            sys.stdout.flush()
            await asyncio.sleep(0.3)
    except KeyboardInterrupt:
        print("\n\n  Stopped.")
    finally:
        reader.close()
        stream_task.cancel()
        try: await stream_task
        except asyncio.CancelledError: pass

    print(f"\n  Total: {ble_count} BLE, {temp_count} temp")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--duration", type=int, default=60)
    args = p.parse_args()
    asyncio.run(run(args.port, args.baud, args.duration))