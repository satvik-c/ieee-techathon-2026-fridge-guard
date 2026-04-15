"""
FridgeGuard — Camera module (Pi Camera Module 3 Wide).

Calibration cycle:
  - On boot: camera starts in free-running mode for brightness detection only.
  - First door open: system enters calibration mode.
    AEC/AWB are allowed to converge on the fully-lit fridge interior.
    After converging, settings are locked permanently.
    This first open does NOT fire on_open or on_close — it's calibration only.
  - All subsequent opens: before/after frames captured with locked lit-state
    settings. No settle delay needed. Consistent exposure and white balance.

State machine (post-calibration):
  CLOSED → (brightness > threshold)    → OPEN   → fires on_open(before_frame)
  OPEN   → (N consecutive dark frames) → CLOSED → fires on_close(before_frame, after_frame)
"""

import asyncio
import collections
import time
from enum import Enum
from typing import Callable, Optional

import numpy as np
from picamera2 import Picamera2


class FridgeState(Enum):
    CLOSED = "closed"
    OPEN   = "open"


_Entry = collections.namedtuple("_Entry", ["ts", "frame"])


class Camera:
    def __init__(
        self,
        brightness_threshold: int = 40,
        black_frame_streak: int = 5,
        after_lookback_sec: float = 0.2,
        calibration_settle: float = 3.0,
        on_open:  Optional[Callable] = None,
        on_close: Optional[Callable] = None,
    ):
        """
        Args:
            brightness_threshold: Mean pixel value above this = fridge open.
            black_frame_streak:   Consecutive dark frames to confirm closed.
            after_lookback_sec:   Seconds before last bright frame to pick after_frame.
            calibration_settle:   Seconds to let AEC/AWB converge during calibration open.
            on_open:  Called with (before_frame,) on door open (post-calibration only).
            on_close: Called with (before_frame, after_frame) on confirmed close.
        """
        self.brightness_threshold = brightness_threshold
        self.black_frame_streak   = black_frame_streak
        self.after_lookback_sec   = after_lookback_sec
        self.calibration_settle   = calibration_settle
        self._on_open             = on_open
        self._on_close            = on_close

        self.state: FridgeState                 = FridgeState.CLOSED
        self.before_frame: Optional[np.ndarray] = None
        self._dark_count: int                   = 0
        self._open_time: Optional[float]        = None
        self._last_bright_ts: Optional[float]   = None
        self._cam: Optional[Picamera2]          = None
        self._calibrated: bool                  = False

        # Rolling buffer of bright frames — ~15s at 5fps
        self._bright_buf: collections.deque = collections.deque(maxlen=75)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _init_camera(self):
        self._cam = Picamera2()
        cfg = self._cam.create_preview_configuration(
            main={"size": (1920, 1080), "format": "RGB888"},
        )
        self._cam.configure(cfg)
        self._cam.start()

        # Let AEC/AWB do an initial settle in the dark.
        # We don't lock here — calibration happens on first open.
        time.sleep(2)

        print(f"[Camera] Pi Camera Module 3 Wide ready — awaiting calibration "
              f"(threshold={self.brightness_threshold}, "
              f"streak={self.black_frame_streak}, "
              f"lookback={self.after_lookback_sec}s)")
        print(f"[Camera] Open the fridge door once to calibrate.")

    def _calibrate(self):
        """
        Called during the first door open.
        Unlocks AEC/AWB, waits for them to converge on the lit fridge interior,
        then locks the settings permanently.
        """
        print(f"[Camera] Calibrating — letting AEC/AWB converge on lit interior "
              f"({self.calibration_settle}s)...")

        # Unlock so the sensor can adapt to the bright lit interior
        self._cam.set_controls({"AeEnable": True, "AwbEnable": True})
        time.sleep(self.calibration_settle)

        # Read the converged values and lock them permanently
        with self._cam.captured_request() as req:
            meta = req.get_metadata()

        gain      = meta.get("AnalogueGain", None)
        exposure  = meta.get("ExposureTime", None)
        awb_gains = meta.get("ColourGains", None)

        controls = {"AeEnable": False, "AwbEnable": False}
        if gain      is not None: controls["AnalogueGain"] = gain
        if exposure  is not None: controls["ExposureTime"] = exposure
        if awb_gains is not None: controls["ColourGains"]  = awb_gains

        self._cam.set_controls(controls)
        time.sleep(0.3)  # let lock take effect

        self._calibrated = True
        print(f"[Camera] Calibration complete — AEC/AWB locked "
              f"(gain={gain:.2f}, exposure={exposure}us, awb={awb_gains})")
        print(f"[Camera] Close the fridge. System is ready.")

    def release(self):
        if self._cam:
            self._cam.stop()
            self._cam = None

    # ── Frame helpers ─────────────────────────────────────────────────────────

    def capture(self) -> Optional[np.ndarray]:
        if self._cam is None:
            return None
        return self._cam.capture_array()

    def get_brightness(self, frame: np.ndarray) -> float:
        return float(np.mean(frame))

    def is_bright(self, frame: np.ndarray) -> bool:
        return self.get_brightness(frame) > self.brightness_threshold

    def _pick_after_frame(self) -> Optional[np.ndarray]:
        """
        Return the buffered frame ~after_lookback_sec before the last bright frame.
        Anchored to _last_bright_ts, not time.monotonic(), because by the time
        close is confirmed several dark frames have already elapsed.
        """
        if not self._bright_buf or self._last_bright_ts is None:
            return None

        target = self._last_bright_ts - self.after_lookback_sec
        best   = self._bright_buf[0]

        for entry in self._bright_buf:
            if entry.ts <= target:
                best = entry
            else:
                break

        age = self._last_bright_ts - best.ts
        print(f"[Camera] After frame: {age:.2f}s before last bright frame")
        return best.frame

    # ── State machine ─────────────────────────────────────────────────────────

    def process_frame(self, frame: np.ndarray) -> Optional[str]:
        """Returns 'opened', 'closed', or None."""
        bright = self.is_bright(frame)

        if self.state == FridgeState.CLOSED:
            self._dark_count = 0
            if bright:
                self.state           = FridgeState.OPEN
                self._open_time      = time.monotonic()
                self._last_bright_ts = None
                self._bright_buf.clear()
                print(f"[Camera] Door OPENED "
                      f"(brightness={self.get_brightness(frame):.1f})")
                return "opened"

        elif self.state == FridgeState.OPEN:
            if bright:
                now = time.monotonic()
                self._last_bright_ts = now
                self._bright_buf.append(_Entry(ts=now, frame=frame.copy()))
                self._dark_count = 0
            else:
                self._dark_count += 1
                if self._dark_count >= self.black_frame_streak:
                    self.state       = FridgeState.CLOSED
                    self._dark_count = 0
                    duration = time.monotonic() - self._open_time if self._open_time else 0
                    print(f"[Camera] Door CLOSED (open for {duration:.1f}s)")
                    return "closed"

        return None

    # ── Main async loop ───────────────────────────────────────────────────────

    async def monitor_loop(self, poll_interval: float = 0.2):
        self._init_camera()
        try:
            while True:
                frame = self.capture()
                if frame is not None:
                    event = self.process_frame(frame)

                    # ── First open: calibration cycle ─────────────────────────
                    if event == "opened" and not self._calibrated:
                        # Calibrate on this open — do not fire on_open/on_close
                        self._calibrate()
                        # Wait for door to close before resuming normal operation
                        print("[Camera] Waiting for door to close after calibration...")
                        while True:
                            await asyncio.sleep(poll_interval)
                            f = self.capture()
                            if f is not None and not self.is_bright(f):
                                # Confirm closed with full streak
                                streak = 0
                                while streak < self.black_frame_streak:
                                    await asyncio.sleep(poll_interval)
                                    f2 = self.capture()
                                    if f2 is not None and not self.is_bright(f2):
                                        streak += 1
                                    else:
                                        streak = 0
                                self.state = FridgeState.CLOSED
                                self._dark_count = 0
                                print("[Camera] Door closed — calibration complete. "
                                      "System is live.")
                                break

                    # ── Normal open (post-calibration) ────────────────────────
                    elif event == "opened" and self._calibrated:
                        before = self.capture()
                        if before is not None:
                            self.before_frame = before.copy()
                            print(f"[Camera] Before frame captured "
                                  f"(brightness={self.get_brightness(before):.1f})")
                        else:
                            print("[Camera] WARNING: failed to capture before frame.")

                        if self._on_open:
                            self._on_open(self.before_frame)

                    # ── Door closed ───────────────────────────────────────────
                    elif event == "closed" and self._calibrated:
                        after = self._pick_after_frame()
                        if after is None:
                            print("[Camera] WARNING: buffer empty — skipping event.")
                        else:
                            print(f"[Camera] After frame brightness="
                                  f"{self.get_brightness(after):.1f}")
                            if self._on_close:
                                self._on_close(self.before_frame, after)

                await asyncio.sleep(poll_interval)
        finally:
            self.release()