"""
FridgeGuard — Config loader.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class RoommateConfig:
    name:            str
    ble_uuid:        str
    private_webhook: str


@dataclass
class CameraConfig:
    brightness_threshold: int
    black_frame_streak:   int
    after_lookback_sec:   float
    calibration_settle:   float


@dataclass
class BLEConfig:
    rssi_floor: int


@dataclass
class TempConfig:
    alert_threshold_f:   float
    alert_sustained_min: int


@dataclass
class GroqConfig:
    api_key: str
    model:   str


@dataclass
class DiscordConfig:
    status_webhook:  str   # debug dump — all events
    general_webhook: str   # visible to all roommates


@dataclass
class Config:
    roommates:   list[RoommateConfig]
    camera:      CameraConfig
    ble:         BLEConfig
    temperature: TempConfig
    groq:        GroqConfig
    discord:     DiscordConfig

    _uuid_map: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for r in self.roommates:
            self._uuid_map[r.ble_uuid.lower()] = r.name

    def resolve_uuid(self, uuid: str) -> Optional[str]:
        return self._uuid_map.get(uuid.lower())

    @property
    def registered_uuids(self) -> list[str]:
        return list(self._uuid_map.keys())

    def roommate_by_name(self, name: str) -> Optional[RoommateConfig]:
        for r in self.roommates:
            if r.name == name:
                return r
        return None


def load_config(path: str = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())

    roommates = [
        RoommateConfig(
            name=r["name"],
            ble_uuid=r["ble_uuid"].lower(),
            private_webhook=r["private_webhook"],
        )
        for r in raw["roommates"]
    ]

    cam = raw["camera"]
    return Config(
        roommates=roommates,
        camera=CameraConfig(
            brightness_threshold = cam["brightness_threshold"],
            black_frame_streak   = cam["black_frame_streak"],
            after_lookback_sec   = cam["after_lookback_sec"],
            calibration_settle   = cam["calibration_settle"],
        ),
        ble=BLEConfig(
            rssi_floor=raw["ble"]["rssi_floor"],
        ),
        temperature=TempConfig(
            alert_threshold_f   = raw["temperature"]["alert_threshold_f"],
            alert_sustained_min = raw["temperature"]["alert_sustained_min"],
        ),
        groq=GroqConfig(
            api_key = raw["groq"]["api_key"],
            model   = raw["groq"]["model"],
        ),
        discord=DiscordConfig(
            status_webhook  = raw["discord"]["status_webhook"],
            general_webhook = raw["discord"]["general_webhook"],
        ),
    )