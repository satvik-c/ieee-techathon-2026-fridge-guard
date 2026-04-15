"""
Phase 1 — Test: models, config, database (inventory-based).
    python3 test_phase1.py
"""

import os
from datetime import datetime

print("=" * 50)
print("TEST 1: Models")
print("=" * 50)

from models import ItemChange, ResolvedIdentity, FridgeEvent, InventoryItem, BLEDevice, BLEScan

change = ItemChange(item_name="Monster Energy", action="removed")
print(f"  ItemChange: {change}")
assert change.action == "removed"

identity = ResolvedIdentity(primary="Alex", is_guest=False, nearby=["Blake"])
assert not identity.is_guest

guest = ResolvedIdentity(is_guest=True)
assert guest.is_guest

inv = InventoryItem(item_name="yogurt", owner="Alex", added_at=datetime.now())
print(f"  InventoryItem: {inv}")

ble_dev = BLEDevice(uuid="ff01", rssi=-42)
scan = BLEScan(timestamp=datetime.now(), devices=[ble_dev])
assert scan.devices[0].uuid == "ff01"

print("  ✓ All model tests passed\n")

print("=" * 50)
print("TEST 2: Config loader")
print("=" * 50)

from config_loader import load_config
config = load_config("config.yaml")

print(f"  Roommates: {[r.name for r in config.roommates]}")
assert len(config.roommates) == 3

assert config.resolve_uuid("ff01") == "Alex"
assert config.resolve_uuid("FF02") == "Blake"
assert config.resolve_uuid("ff99") is None
print(f"  UUID resolution ✓")

assert config.camera.brightness_threshold == 85
assert config.ble.rssi_floor == -85
assert config.gemini.model == "gemini-2.0-flash-lite"
assert config.gemini.max_retries == 3
print(f"  Gemini config: model={config.gemini.model} ✓")

alex = config.roommate_by_name("Alex")
assert alex.discord_webhook.startswith("https://")
print(f"  Discord webhook configured ✓")

assert config.discord.general_webhook.startswith("https://")
print(f"  General webhook configured ✓")

print("  ✓ All config tests passed\n")

print("=" * 50)
print("TEST 3: Database — inventory + events")
print("=" * 50)

TEST_DB = "test_fridgeguard.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from db import DB
db = DB(TEST_DB)

# ── Inventory CRUD ──
db.add_item("monster energy", "Alex")
db.add_item("greek yogurt", "Blake")
db.add_item("leftover pizza", "Alex")

inv = db.get_inventory()
print(f"  Added 3 items, inventory has {len(inv)}")
assert len(inv) == 3

# Find item
item = db.find_item("Monster Energy")  # case insensitive
assert item is not None
assert item["owner"] == "Alex"
print(f"  find_item('Monster Energy') → owner={item['owner']} ✓")

item = db.find_item("nonexistent")
assert item is None
print(f"  find_item('nonexistent') → None ✓")

# Remove item
removed = db.remove_item("greek yogurt")
assert removed is not None
assert removed["owner"] == "Blake"
print(f"  remove_item('greek yogurt') → owner={removed['owner']} ✓")

inv = db.get_inventory()
assert len(inv) == 2
print(f"  Inventory now has {len(inv)} items ✓")

# By owner
alex_items = db.get_inventory_by_owner("Alex")
assert len(alex_items) == 2
print(f"  Alex has {len(alex_items)} items ✓")

# ── Event logging ──
db.log_event(actor="Alex", action="added", item_name="monster energy",
             item_owner="Alex", scenario="owner_add")
db.log_event(actor="Blake", action="removed", item_name="monster energy",
             item_owner="Alex", scenario="theft")
db.log_event(actor=None, action="removed", item_name="pizza",
             item_owner="Casey", scenario="guest_theft")

events = db.get_recent_events(limit=10)
assert len(events) == 3
assert events[0]["actor"] == "guest"
assert events[1]["scenario"] == "theft"
print(f"  Logged 3 events ✓")

# ── Temp ──
db.log_temp(temp_c=6.0, humidity=45.0)
assert len(db.get_recent_temps(minutes=5)) == 1
print(f"  Temp logging ✓")

# ── Clear inventory ──
db.clear_inventory()
assert len(db.get_inventory()) == 0
print(f"  clear_inventory() ✓")

db.close()
os.remove(TEST_DB)
print("  ✓ All database tests passed\n")

print("=" * 50)
print("Phase 1 COMPLETE — all tests passed!")
print("=" * 50)