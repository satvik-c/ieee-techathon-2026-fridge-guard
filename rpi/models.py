"""
FridgeGuard — Shared data models.

These dataclasses are the common language between every module.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np


@dataclass
class ItemChange:
    """A single item detected as added or removed by Gemini."""
    item_name: str         # e.g. "Monster Energy", "yogurt cup"
    action: str            # "added" or "removed"


@dataclass
class ResolvedIdentity:
    """Who was at the fridge, resolved from BLE service UUIDs."""
    primary: Optional[str] = None
    is_guest: bool = False
    nearby: list[str] = field(default_factory=list)


@dataclass
class FridgeEvent:
    """A complete fridge interaction: who opened it and what changed."""
    timestamp: datetime
    identity: ResolvedIdentity
    changes: list[ItemChange]
    before_frame: Optional[np.ndarray] = None
    after_frame: Optional[np.ndarray] = None


@dataclass
class InventoryItem:
    """An item currently in the fridge, tracked in the database."""
    item_name: str
    owner: str             # roommate name who put it in
    added_at: datetime


@dataclass
class TempReading:
    """A single temperature/humidity reading from the ESP32."""
    timestamp: datetime
    temp_c: float
    humidity: float


@dataclass
class BLEDevice:
    """A single BLE device detected by the ESP32."""
    uuid: str              # 16-bit service UUID hex string, e.g. "ff01"
    rssi: int


@dataclass
class BLEScan:
    """A batch of BLE devices from one scan window."""
    timestamp: datetime
    devices: list[BLEDevice] = field(default_factory=list)