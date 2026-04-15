"""
Phase 2 — Camera test (Arducam IMX708 via Picamera2).

    python3 test_phase2.py --unit          # no camera needed
    python3 test_phase2.py --headless      # SSH friendly, prints to terminal
    python3 test_phase2.py --headless --threshold 85

The unit test feeds synthetic frames through the state machine.
The headless test uses Picamera2 to capture real frames.
"""

import argparse
import sys
import time

import cv2
import numpy as np

from camera import Camera, FridgeState


def test_unit():
    """Synthetic test — no camera needed."""
    print("=" * 55)
    print("TEST: Unit test (synthetic frames)")
    print("=" * 55)

    transition_log = []

    def on_open(before):
        transition_log.append(("opened", before.mean()))

    def on_close(before, after):
        transition_log.append(("closed", before.mean(), after.mean()))

    cam = Camera(
        brightness_threshold=20,
        black_frame_streak=3,
        on_open=on_open,
        on_close=on_close,
    )

    def bright_frame(val=150):
        return np.full((480, 640, 3), val, dtype=np.uint8)

    def dark_frame(val=5):
        return np.full((480, 640, 3), val, dtype=np.uint8)

    assert cam.state == FridgeState.CLOSED
    print("  Initial state: CLOSED ✓")

    for _ in range(5):
        result = cam.process_frame(dark_frame())
        assert result is None
    print("  Dark frames while closed → stays CLOSED ✓")

    result = cam.process_frame(bright_frame(150))
    assert result == "opened"
    assert cam.state == FridgeState.OPEN
    print("  Bright frame → OPENED ✓")
    print(f"    before_frame brightness: {cam.get_brightness(cam.before_frame):.1f}")

    cam.process_frame(bright_frame(160))
    cam.process_frame(bright_frame(170))
    cam.process_frame(bright_frame(180))
    assert cam.state == FridgeState.OPEN
    print("  Bright frames while open → stays OPEN, buffering ✓")

    cam.process_frame(dark_frame())
    cam.process_frame(dark_frame())
    assert cam.state == FridgeState.OPEN
    print("  2 dark frames (streak=3) → still OPEN ✓")

    cam.process_frame(bright_frame(200))
    assert cam.state == FridgeState.OPEN
    print("  Bright frame resets dark count ✓")

    cam.process_frame(dark_frame())
    cam.process_frame(dark_frame())
    result = cam.process_frame(dark_frame())
    assert result == "closed"
    assert cam.state == FridgeState.CLOSED
    print("  3 consecutive dark frames → CLOSED ✓")

    after_brightness = cam.get_brightness(cam.after_frame)
    assert after_brightness > 100
    print(f"    after_frame is last bright frame (brightness={after_brightness:.0f}) ✓")

    assert len(transition_log) == 2
    print("  Callbacks fired correctly ✓")

    cam.process_frame(bright_frame(130))
    assert cam.state == FridgeState.OPEN
    for _ in range(3):
        cam.process_frame(dark_frame())
    assert cam.state == FridgeState.CLOSED
    assert len(transition_log) == 4
    print("  Second open/close cycle works ✓")

    print("\n  ✓ All unit tests passed!\n")


def test_headless(threshold: int):
    """
    Headless test with Picamera2 — prints brightness to terminal.
    Cover the camera → CLOSED. Uncover → OPEN.
    """
    print("=" * 55)
    print("TEST: Headless camera test (Picamera2 / IMX708)")
    print(f"  Threshold: {threshold}")
    print("  Cover lens → CLOSED. Uncover → OPEN.")
    print("  Ctrl+C to stop.")
    print("=" * 55)

    event_count = {"opened": 0, "closed": 0}

    def on_open(before):
        event_count["opened"] += 1
        brightness = Camera.get_brightness(None, before)
        print(f"\n  >>> FRIDGE OPENED (#{event_count['opened']})  "
              f"before brightness: {brightness:.1f}")

    def on_close(before, after):
        event_count["closed"] += 1
        b = Camera.get_brightness(None, before)
        a = Camera.get_brightness(None, after)
        print(f"\n  >>> FRIDGE CLOSED (#{event_count['closed']})  "
              f"before: {b:.1f}  after: {a:.1f}")

    cam = Camera(
        brightness_threshold=threshold,
        black_frame_streak=5,
        on_open=on_open,
        on_close=on_close,
    )
    cam._init_camera()

    try:
        while True:
            frame = cam.capture()
            if frame is None:
                print("  [!] Failed to capture frame")
                time.sleep(1)
                continue

            brightness = cam.get_brightness(frame)
            state_str = cam.state.value.upper()

            sys.stdout.write(
                f"\r  Brightness: {brightness:6.1f}  |  "
                f"State: {state_str:6s}  |  "
                f"Opens: {event_count['opened']}  Closes: {event_count['closed']}  "
            )
            sys.stdout.flush()

            cam.process_frame(frame)
            time.sleep(0.2)

    except KeyboardInterrupt:
        print(f"\n\n  Stopped. Opens: {event_count['opened']}  "
              f"Closes: {event_count['closed']}")
    finally:
        cam.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FridgeGuard Phase 2 — Camera Test")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--unit", action="store_true", help="Synthetic test")
    group.add_argument("--headless", action="store_true", help="Live with Picamera2")

    parser.add_argument("--threshold", type=int, default=30,
                        help="Brightness threshold (default: 30)")

    args = parser.parse_args()

    if args.unit:
        test_unit()
    elif args.headless:
        test_headless(args.threshold)