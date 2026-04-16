"""
FridgeGuard — Discord notifier.

Three channel types:
  status  — debug dump, every event (your testing channel)
  general — visible to all roommates (thefts, temp alerts)
  private — per-roommate (their own activity, alerts about their items)

All sends are fire-and-forget via requests. Failures are logged but
never raise — notifications should never crash the main pipeline.
"""

import json
from datetime import datetime
from typing import Optional

import requests


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Notifier:
    def __init__(self, config):
        self.config          = config
        self.status_webhook  = config.discord.status_webhook
        self.general_webhook = config.discord.general_webhook

    # ── Low-level send ────────────────────────────────────────────────────────

    def _send(self, webhook: str, content: str, color: Optional[int] = None):
        """POST a message to a Discord webhook. Never raises."""
        if not webhook or webhook.startswith("https://discord.com/api/webhooks/YOUR"):
            # Webhook not configured — print to terminal only
            print(f"[Discord] (not configured) {content}")
            return

        try:
            if color is not None:
                payload = {
                    "embeds": [{
                        "description": content,
                        "color": color,
                        "footer": {"text": f"FridgeGuard • {_ts()}"},
                    }]
                }
            else:
                payload = {"content": content}

            resp = requests.post(
                webhook,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            if resp.status_code not in (200, 204):
                print(f"[Discord] Webhook error {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            print(f"[Discord] Send failed: {e}")

    # ── Channel helpers ───────────────────────────────────────────────────────

    def status(self, msg: str):
        """Debug dump — goes to status channel only."""
        print(f"[{_ts()}] 🔧  {msg}")
        self._send(self.status_webhook, f"🔧 `{_ts()}` {msg}")

    def general(self, msg: str, color: int = 0x5865F2):
        """Visible to all roommates."""
        print(f"[{_ts()}] 📢  {msg}")
        self._send(self.general_webhook, msg, color=color)

    def private(self, roommate_name: str, msg: str, color: int = 0x57F287):
        """Send to a specific roommate's private channel."""
        print(f"[{_ts()}] 🔒  [{roommate_name}] {msg}")
        roommate = self.config.roommate_by_name(roommate_name)
        if roommate:
            self._send(roommate.private_webhook, msg, color=color)
        else:
            print(f"[Discord] Unknown roommate: {roommate_name}")

    def general_and_private(self, roommate_name: str, general_msg: str,
                             private_msg: str, color: int = 0x5865F2):
        """Send different messages to general + a roommate's private channel."""
        self.general(general_msg, color=color)
        self.private(roommate_name, private_msg, color=color)

    # ── Scenario helpers ──────────────────────────────────────────────────────

    COLORS = {
        "info":    0x57F287,   # green
        "warning": 0xFEE75C,   # yellow
        "urgent":  0xED4245,   # red
        "neutral": 0x5865F2,   # blurple
    }

    def scenario_a_add(self, actor: str, item: str):
        """Owner added their own item — private only."""
        self.status(f"Scenario A — {actor} added '{item}'")
        self.private(actor,
                     f"✅ You added **{item}** to the fridge. It's been logged to your inventory.",
                     color=self.COLORS["info"])

    def scenario_a_remove(self, actor: str, item: str):
        """Owner removed their own item — private only."""
        self.status(f"Scenario A — {actor} removed their own '{item}'")
        self.private(actor,
                     f"✅ You removed your **{item}** from the fridge.",
                     color=self.COLORS["info"])

    def scenario_b(self, actor: str, owner: str, item: str):
        """Roommate took another's item — general + private to owner."""
        self.status(f"Scenario B — {actor} removed '{item}' owned by {owner}")
        self.general(
            f"⚠️ **{actor}** removed **{item}** from the fridge "
            f"(owned by {owner}).",
            color=self.COLORS["warning"],
        )
        self.private(
            owner,
            f"⚠️ **{actor}** removed your **{item}** from the fridge.",
            color=self.COLORS["warning"],
        )

    def scenario_c(self, owner: str, item: str, probable_host: Optional[str]):
        """Guest took an item — general + private to owner + private to host."""
        host_str = f" **{probable_host}** may have let them in." if probable_host else ""
        self.status(f"Scenario C — guest removed '{item}' owned by {owner}, "
                    f"host={probable_host or 'unknown'}")
        self.general(
            f"🚨 An **unregistered person** removed **{item}** "
            f"(owned by {owner}).{host_str}",
            color=self.COLORS["urgent"],
        )
        self.private(
            owner,
            f"🚨 An unregistered person removed your **{item}** from the fridge.{host_str}",
            color=self.COLORS["urgent"],
        )
        if probable_host:
            self.private(
                probable_host,
                f"⚠️ A guest you may have brought removed **{item}** "
                f"(owned by {owner}) from the fridge.",
                color=self.COLORS["warning"],
            )

    def temp_alert(self, temp_c: float, threshold: float):
        """Temperature anomaly — general alert."""
        self.status(f"Temp alert: {temp_c:.1f} °F > {threshold} °F")
        self.general(
            f"🌡️ Fridge temperature is **{temp_c:.1f} °F** "
            f"(threshold: {threshold} °F). Door may be open or seal issue.",
            color=self.COLORS["warning"],
        )

    def door_opened(self, actor: str, brightness: float):
        """Status channel only."""
        self.status(f"Door OPENED — actor: {actor}, brightness: {brightness:.1f}")

    def door_closed(self, actor: str, scan_count: int, duration: float):
        """Status channel only."""
        self.status(f"Door CLOSED — actor: {actor}, "
                    f"scans: {scan_count}, open for {duration:.1f}s")

    def groq_result(self, changes: list):
        """Status channel only — Groq analysis dump."""
        if not changes:
            self.status("Groq: no changes detected")
        else:
            summary = ", ".join(f"{c['action']} '{c['item']}'" for c in changes)
            self.status(f"Groq: {len(changes)} change(s) — {summary}")

    def upload_debug_frames(self, before, after):
        """Uploads the raw frames to the Discord general channel."""
        import io
        from PIL import Image

        if not self.general_webhook or "YOUR" in self.general_webhook:
            return

        print(f"[{_ts()}] 📸  Uploading debug frames to Discord...")
        
        def _to_bytes(frame):
            img = Image.fromarray(frame)
            buf = io.BytesIO()
            # Resize slightly so Discord accepts them instantly
            img.thumbnail((1280, 720)) 
            img.save(buf, format="JPEG", quality=60)
            return buf.getvalue()

        files = {
            "file1": ("before.jpg", _to_bytes(before), "image/jpeg"),
            "file2": ("after.jpg", _to_bytes(after), "image/jpeg")
        }
        
        try:
            # Note: When uploading files, 'content' goes in the data payload, not JSON
            requests.post(
                self.general_webhook, 
                data={"content": "📸 **Debug Frames** (Before vs After)"}, 
                files=files, 
                timeout=10
            )
        except Exception as e:
            print(f"[Discord] Image upload failed: {e}")