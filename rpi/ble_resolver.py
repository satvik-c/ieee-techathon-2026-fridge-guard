"""
FridgeGuard — BLE identity resolver.

Takes raw BLE scan results from the ESP32 and resolves them into
a ResolvedIdentity: who is at the fridge right now?

Logic:
  - Filter devices by RSSI floor (ignore weak/distant signals).
  - Match service UUIDs against the roommate registry.
  - If one roommate detected → they're the primary.
  - If multiple detected → strongest RSSI is primary, rest are nearby.
  - If none detected → guest/unknown.
  - Maintains a rolling window of recent sightings so the alert engine
    can find a "probable host" for guest scenarios.
"""

from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from models import BLEScan, ResolvedIdentity


class BLEResolver:
    def __init__(self, config):
        """
        Args:
            config: Loaded Config object with roommate registry and BLE settings.
        """
        self.config = config
        self.rssi_floor = config.ble.rssi_floor

        # Rolling window of recent sightings: (timestamp, roommate_name)
        # Used by alert engine to find probable host in guest scenarios
        self.recent_sightings: deque = deque(maxlen=200)

    def resolve(self, scan: BLEScan) -> ResolvedIdentity:
        """
        Resolve a BLE scan into a roommate identity.

        Returns:
            ResolvedIdentity with primary, is_guest, and nearby fields.
        """
        detected = []
        for dev in scan.devices:
            if dev.rssi < self.rssi_floor:
                continue
            name = self.config.resolve_uuid(dev.uuid)
            if name:
                detected.append((name, dev.rssi))

        # Record sightings for the rolling window
        for name, _ in detected:
            self.recent_sightings.append((scan.timestamp, name))

        if len(detected) == 0:
            return ResolvedIdentity(primary=None, is_guest=True, nearby=[])

        # Sort by RSSI — strongest signal = closest = most likely at fridge
        detected.sort(key=lambda x: x[1], reverse=True)

        primary = detected[0][0]
        nearby = [name for name, _ in detected[1:]]

        return ResolvedIdentity(primary=primary, is_guest=False, nearby=nearby)

    def find_recent_nearby(self, window_sec: int = 60) -> Optional[str]:
        """
        Find a roommate seen nearby in the recent past.
        Used to identify a probable host when a guest accesses the fridge.

        Returns the most recently seen roommate name, or None.
        """
        cutoff = datetime.now() - timedelta(seconds=window_sec)

        for ts, name in reversed(self.recent_sightings):
            if ts >= cutoff:
                return name
            if ts < cutoff:
                break

        return None

    def get_sighting_summary(self, window_sec: int = 60) -> dict[str, int]:
        """
        Count how many times each roommate was seen in the last N seconds.
        Useful for debugging and confidence scoring.
        """
        cutoff = datetime.now() - timedelta(seconds=window_sec)
        counts: dict[str, int] = {}

        for ts, name in self.recent_sightings:
            if ts >= cutoff:
                counts[name] = counts.get(name, 0) + 1

        return counts