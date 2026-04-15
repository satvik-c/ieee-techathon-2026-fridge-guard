"""
Phase 5 — BLE resolver test.
    python3 test_phase5.py
"""

from datetime import datetime, timedelta
from models import BLEDevice, BLEScan
from config_loader import load_config
from ble_resolver import BLEResolver

config = load_config("config.yaml")
resolver = BLEResolver(config)

print("=" * 55)
print("TEST: BLE Resolver")
print("=" * 55)

print("\n  Test 1: Single roommate")
scan = BLEScan(timestamp=datetime.now(), devices=[BLEDevice(uuid="ff01", rssi=-45)])
identity = resolver.resolve(scan)
assert identity.primary == "Alex" and not identity.is_guest
print(f"    → {identity.primary} ✓")

print("\n  Test 2: Two roommates — strongest wins")
scan = BLEScan(timestamp=datetime.now(), devices=[
    BLEDevice(uuid="ff01", rssi=-60), BLEDevice(uuid="ff02", rssi=-35)
])
identity = resolver.resolve(scan)
assert identity.primary == "Blake" and "Alex" in identity.nearby
print(f"    → {identity.primary}, nearby={identity.nearby} ✓")

print("\n  Test 3: No devices → guest")
scan = BLEScan(timestamp=datetime.now(), devices=[])
identity = resolver.resolve(scan)
assert identity.is_guest
print(f"    → guest ✓")

print("\n  Test 4: Unknown UUID → guest")
scan = BLEScan(timestamp=datetime.now(), devices=[BLEDevice(uuid="ff99", rssi=-40)])
identity = resolver.resolve(scan)
assert identity.is_guest
print(f"    → guest ✓")

print("\n  Test 5: RSSI below floor → filtered")
scan = BLEScan(timestamp=datetime.now(), devices=[BLEDevice(uuid="ff01", rssi=-90)])
identity = resolver.resolve(scan)
assert identity.is_guest  # -90 < -85 floor
print(f"    → filtered out ✓")

print("\n  Test 6: RSSI at floor → included")
scan = BLEScan(timestamp=datetime.now(), devices=[BLEDevice(uuid="ff01", rssi=-85)])
identity = resolver.resolve(scan)
assert identity.primary == "Alex"
print(f"    → {identity.primary} ✓")

print("\n  Test 7: All three roommates")
scan = BLEScan(timestamp=datetime.now(), devices=[
    BLEDevice(uuid="ff01", rssi=-55),
    BLEDevice(uuid="ff02", rssi=-40),
    BLEDevice(uuid="ff03", rssi=-50),
])
identity = resolver.resolve(scan)
assert identity.primary == "Blake"
assert set(identity.nearby) == {"Alex", "Casey"}
print(f"    → {identity.primary}, nearby={identity.nearby} ✓")

print("\n  Test 8: Recent sightings")
summary = resolver.get_sighting_summary(window_sec=60)
assert "Alex" in summary and "Blake" in summary
print(f"    Sightings: {summary} ✓")

print("\n  Test 9: Probable host")
host = resolver.find_recent_nearby(window_sec=60)
assert host is not None
print(f"    → {host} ✓")

print("\n  Test 10: Expired window")
resolver.recent_sightings.clear()
resolver.recent_sightings.append((datetime.now() - timedelta(seconds=120), "Alex"))
host = resolver.find_recent_nearby(window_sec=60)
assert host is None
print(f"    → None (expired) ✓")

print("\n  ✓ All BLE resolver tests passed!\n")