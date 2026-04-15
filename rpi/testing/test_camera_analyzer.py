"""
FridgeGuard — test_camera_analyzer.py

End-to-end integration test for camera.py + analyzer.py (Groq vision).
  1. Camera monitors brightness (Pi Camera Module 3 Wide)
  2. On OPEN  → captures before_frame (after settle + AWB lock)
  3. On CLOSE → picks after_frame from buffer (lookback before last bright frame)
  4. Analyzer.analyze() called in background thread via Groq
  5. Prints what changed

Usage:
    python3 test_camera_analyzer.py [--threshold 85] [--api-key KEY] [--mock] [--save-frames] [--once]

Dependencies:
    pip install groq pillow --break-system-packages
    (picamera2 pre-installed on Raspberry Pi OS)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
from camera   import Camera
from analyzer import Analyzer


# ── Args ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--threshold",   type=int,   default=40,
                   help="Brightness threshold for open/close (default: 85)")
    p.add_argument("--lookback",    type=float, default=0.2,
                   help="Seconds before last bright frame to pick after_frame (default: 1.0)")
    p.add_argument("--cal-settle",  type=float, default=3.0,
                   help="Seconds for AEC/AWB to converge during calibration open (default: 3.0)")
    p.add_argument("--api-key",     type=str,   default=None,
                   help="Groq API key (or set GROQ_API_KEY env var)")
    p.add_argument("--mock",        action="store_true",
                   help="Use mock response, no API call")
    p.add_argument("--save-frames", action="store_true",
                   help="Save before/after JPEGs to disk")
    p.add_argument("--once",        action="store_true",
                   help="Exit after first complete cycle")
    p.add_argument("--min-gap",     type=float, default=5.0,
                   help="Min seconds between API calls (default: 5)")
    return p.parse_args()


# ── Output ────────────────────────────────────────────────────────────────────

ICONS = {"added": "➕", "removed": "➖", "moved": "↔️ "}

def print_results(result: dict):
    print()
    print("=" * 58)
    print("  BEFORE — Contents")
    print("=" * 58)
    for item in result.get("before_contents") or ["(nothing detected)"]:
        print(f"    • {item}")
    print()
    print("=" * 58)
    print("  AFTER — Contents")
    print("=" * 58)
    for item in result.get("after_contents") or ["(nothing detected)"]:
        print(f"    • {item}")
    print()
    print("=" * 58)
    print("  CHANGES")
    print("=" * 58)
    changes = result.get("changes", [])
    if changes:
        for c in changes:
            icon   = ICONS.get(c["action"], "?")
            action = c["action"].upper().ljust(8)
            print(f"    {icon}  {action}  {c['item']}")
    else:
        print("    No changes detected.")
    print("=" * 58)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    api_key = args.api_key or os.environ.get("GROQ_API_KEY")
    if not api_key and not args.mock:
        print("[ERROR] No Groq API key. Pass --api-key or export GROQ_API_KEY.")
        print("        Or use --mock to test without an API call.")
        sys.exit(1)

    analyzer       = Analyzer(api_key=api_key or "mock")
    cycle_count    = 0
    last_call_time = [0.0]
    stop_event     = asyncio.Event()
    executor       = ThreadPoolExecutor(max_workers=1)

    def on_open(before_frame: np.ndarray):
        print("\n[EVENT] Fridge OPENED — before frame ready.")

    def on_close(before_frame: np.ndarray, after_frame: np.ndarray):
        nonlocal cycle_count
        cycle_count += 1
        n = cycle_count
        print(f"\n[EVENT] Fridge CLOSED — cycle #{n}")

        if args.save_frames:
            Image.fromarray(before_frame).save(f"before_{n}.jpg")
            Image.fromarray(after_frame).save(f"after_{n}.jpg")
            print(f"[SAVED] before_{n}.jpg / after_{n}.jpg")

        if args.mock:
            result = analyzer.analyze_mock()
            print_results(result)
            with open(f"result_{n}.json", "w") as f:
                json.dump(result, f, indent=2)
            if args.once:
                stop_event.set()
            return

        b, a = before_frame.copy(), after_frame.copy()

        def run():
            # Cooldown inside thread — doesn't block camera loop
            elapsed = time.time() - last_call_time[0]
            if elapsed < args.min_gap:
                wait = args.min_gap - elapsed
                print(f"[Analyzer] Cooldown: waiting {wait:.1f}s...")
                time.sleep(wait)
            last_call_time[0] = time.time()

            try:
                result = analyzer.analyze(b, a)
                print_results(result)
                with open(f"result_{n}.json", "w") as f:
                    json.dump(result, f, indent=2)
                print(f"[SAVED] result_{n}.json")
            except Exception as e:
                print(f"[ERROR] Analysis failed: {e}")
            finally:
                if args.once:
                    stop_event.set()

        executor.submit(run)

    cam = Camera(
        brightness_threshold=args.threshold,
        black_frame_streak=5,
        after_lookback_sec=args.lookback,
        calibration_settle=args.cal_settle,
        on_open=on_open,
        on_close=on_close,
    )

    print()
    print("  FridgeGuard — Camera + Analyzer Integration Test")
    print("  ─────────────────────────────────────────────────")
    print(f"  Threshold  : {args.threshold}")
    print(f"  Cal settle : {args.cal_settle}s")
    print(f"  Lookback   : {args.lookback}s")
    print(f"  Min gap    : {args.min_gap}s between API calls")
    print(f"  Mode       : {'MOCK' if args.mock else 'LIVE (Groq API)'}")
    print()
    print("  Expose camera to light → fridge open")
    print("  Cover camera           → fridge close")
    print("  Ctrl+C to quit.")
    print()

    async def run():
        monitor = asyncio.create_task(cam.monitor_loop())
        if args.once:
            await stop_event.wait()
            monitor.cancel()
        else:
            await monitor

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[EXIT] Stopped.")
    finally:
        cam.release()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()