"""
Phase 3 — Test: Gemini analyzer (mock responses, no API key needed).
    python3 test_phase3.py
"""

import numpy as np
from analyzer import Analyzer
from config_loader import load_config

config = load_config("config.yaml")
analyzer = Analyzer(config)

print("=" * 55)
print("TEST: Gemini Analyzer (mock responses)")
print("=" * 55)

# Dummy frames (not sent to API in mock mode)
before = np.zeros((480, 640, 3), dtype=np.uint8)
after = np.zeros((480, 640, 3), dtype=np.uint8)

# ── Test 1: Normal response — items removed and added ──
print("\n  Test 1: Items removed and added")
response = '[{"item": "Monster Energy can", "action": "removed"}, {"item": "Greek yogurt", "action": "added"}]'
changes = analyzer.analyze_mock(before, after, response)
assert len(changes) == 2
assert changes[0].item_name == "Monster Energy can"
assert changes[0].action == "removed"
assert changes[1].action == "added"
print(f"    {changes[0].item_name} → {changes[0].action} ✓")
print(f"    {changes[1].item_name} → {changes[1].action} ✓")

# ── Test 2: Nothing changed ──
print("\n  Test 2: Nothing changed")
changes = analyzer.analyze_mock(before, after, "[]")
assert len(changes) == 0
print(f"    Empty list → no changes ✓")

# ── Test 3: Response with markdown backticks ──
print("\n  Test 3: Markdown-wrapped response")
response = '```json\n[{"item": "Red Tupperware", "action": "removed"}]\n```'
changes = analyzer.analyze_mock(before, after, response)
assert len(changes) == 1
assert changes[0].item_name == "Red Tupperware"
print(f"    Stripped backticks → {changes[0].item_name} ✓")

# ── Test 4: Malformed JSON ──
print("\n  Test 4: Malformed JSON")
changes = analyzer.analyze_mock(before, after, "this is not json")
assert len(changes) == 0
print(f"    Gracefully returned empty ✓")

# ── Test 5: Partially valid response ──
print("\n  Test 5: Partially valid (missing fields)")
response = '[{"item": "milk", "action": "removed"}, {"bad": "data"}, {"item": "eggs"}]'
changes = analyzer.analyze_mock(before, after, response)
assert len(changes) == 1  # only the first item is valid
assert changes[0].item_name == "milk"
print(f"    Filtered to valid entries only ✓")

# ── Test 6: Invalid action ──
print("\n  Test 6: Invalid action value")
response = '[{"item": "butter", "action": "moved"}]'
changes = analyzer.analyze_mock(before, after, response)
assert len(changes) == 0  # "moved" is not added/removed
print(f"    'moved' action rejected ✓")

# ── Test 7: Multiple items of same type ──
print("\n  Test 7: Multiple items")
response = """[
    {"item": "Coke can", "action": "removed"},
    {"item": "Orange juice bottle", "action": "removed"},
    {"item": "Leftover pasta container", "action": "added"}
]"""
changes = analyzer.analyze_mock(before, after, response)
assert len(changes) == 3
print(f"    3 items parsed correctly ✓")

# ── Test 8: Base64 encoding ──
print("\n  Test 8: Frame to base64 encoding")
test_frame = np.full((100, 100, 3), 128, dtype=np.uint8)
b64 = analyzer._frame_to_base64(test_frame)
assert len(b64) > 100
assert isinstance(b64, str)
print(f"    Encoded {len(b64)} chars ✓")

# ── Test 9: Integration with inventory DB ──
print("\n  Test 9: Analyzer → DB integration flow")
from db import DB
import os

TEST_DB = "test_analyzer.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

db = DB(TEST_DB)

# Simulate: Alex adds items
db.add_item("monster energy can", "Alex")
db.add_item("greek yogurt cup", "Alex")

# Simulate: Gemini detects monster energy was removed
mock_response = '[{"item": "monster energy can", "action": "removed"}]'
changes = analyzer.analyze_mock(before, after, mock_response)

for change in changes:
    if change.action == "removed":
        item = db.find_item(change.item_name)
        if item:
            print(f"    '{change.item_name}' belongs to {item['owner']}")
            assert item["owner"] == "Alex"
            db.remove_item(change.item_name)

assert len(db.get_inventory()) == 1  # yogurt still there
print(f"    Inventory updated correctly ✓")

db.close()
os.remove(TEST_DB)

print("\n  ✓ All analyzer tests passed!\n")