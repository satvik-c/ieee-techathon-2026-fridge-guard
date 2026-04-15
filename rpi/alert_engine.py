"""
FridgeGuard — Alert Engine.

Receives fridge close events (before/after frames + BLE identity),
runs Groq analysis (with inventory context for consistent descriptions),
evaluates scenario matrix, prints terminal alerts, and logs to SQLite.

Scenarios:
  A — Owner accesses own item        → log only, no alert
  B — Roommate takes another's item  → info alert
  C — Guest / unknown takes item     → urgent alert + probable host
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

from analyzer     import Analyzer
from ble_resolver import BLEResolver
from db           import DB
from models       import ResolvedIdentity


# ── Terminal alerts ───────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def alert_log(msg: str):
    print(f"[{_ts()}] 📋  {msg}")

def alert_info(msg: str):
    print(f"\n[{_ts()}] ℹ️   {msg}")

def alert_urgent(msg: str):
    print(f"\n[{_ts()}] 🚨  {msg}")


# ── Alert Engine ──────────────────────────────────────────────────────────────

class AlertEngine:
    def __init__(
        self,
        analyzer:     Analyzer,
        ble_resolver: BLEResolver,
        db:           DB,
        config,
        min_api_gap:  float = 5.0,
    ):
        self.analyzer     = analyzer
        self.ble_resolver = ble_resolver
        self.db           = db
        self.config       = config
        self.min_api_gap  = min_api_gap

        self._executor      = ThreadPoolExecutor(max_workers=1)
        self._last_api_call = 0.0

    def on_door_close(
        self,
        before:   np.ndarray,
        after:    np.ndarray,
        identity: ResolvedIdentity,
    ):
        """
        Entry point called when fridge closes.
        Runs in background thread so camera loop stays unblocked.
        """
        # Snapshot inventory NOW (before thread runs) so it reflects
        # the state at the moment the door closed
        inventory = self.db.get_inventory()
        b, a      = before.copy(), after.copy()
        self._executor.submit(self._run, b, a, identity, inventory)

    def _run(self, before: np.ndarray, after: np.ndarray,
             identity: ResolvedIdentity, inventory: list[dict]):
        # Cooldown between API calls
        elapsed = time.time() - self._last_api_call
        if elapsed < self.min_api_gap:
            wait = self.min_api_gap - elapsed
            print(f"[AlertEngine] Cooldown: {wait:.1f}s...")
            time.sleep(wait)
        self._last_api_call = time.time()

        print(f"[AlertEngine] Analyzing — inventory has {len(inventory)} item(s)...")
        try:
            # Pass current inventory so Groq uses exact stored descriptions
            result = self.analyzer.analyze(before, after, inventory=inventory)
        except Exception as e:
            print(f"[AlertEngine] Groq failed: {e}")
            return

        changes = result.get("changes", [])
        actor   = identity.primary

        if not changes:
            alert_log(f"No item changes detected "
                      f"(actor: {actor or 'unknown'}).")
            return

        print(f"[AlertEngine] {len(changes)} change(s) — "
              f"actor: {actor or 'GUEST'}")

        for change in changes:
            self._evaluate(identity, change["item"], change["action"])

    def _evaluate(self, identity: ResolvedIdentity, item: str, action: str):
        actor = identity.primary

        if action == "added":
            if actor:
                self.db.add_item(item_name=item, owner=actor)
                self.db.log_event(actor=actor, action="added",
                                  item_name=item, item_owner=actor, scenario="A")
                alert_log(f"Scenario A — {actor} added '{item}' → inventory.")
            else:
                alert_log(f"Guest added '{item}' — ownership unassigned.")
                self.db.log_event(actor=None, action="added",
                                  item_name=item, scenario="C")

        elif action == "removed":
            record = self.db.find_item(item)
            owner  = record["owner"] if record else None

            if owner is None:
                alert_log(f"'{item}' removed but not in inventory "
                          f"(actor: {actor or 'unknown'}).")
                self.db.log_event(actor=actor, action="removed",
                                  item_name=item, scenario=None)
                return

            if identity.is_guest:
                # Scenario C
                host     = self.ble_resolver.find_recent_nearby(window_sec=20)
                host_str = f" Probable host: {host}." if host else ""
                alert_urgent(
                    f"Scenario C — GUEST removed '{item}' "
                    f"(owned by {owner}).{host_str}"
                )
                self.db.remove_item(item)
                self.db.log_event(actor="guest", action="removed",
                                  item_name=item, item_owner=owner, scenario="C")

            elif actor == owner:
                # Scenario A
                alert_log(f"Scenario A — {actor} removed their own '{item}'.")
                self.db.remove_item(item)
                self.db.log_event(actor=actor, action="removed",
                                  item_name=item, item_owner=owner, scenario="A")

            else:
                # Scenario B
                alert_info(
                    f"Scenario B — {actor} removed '{item}' "
                    f"(owned by {owner})."
                )
                self.db.log_event(actor=actor, action="removed",
                                  item_name=item, item_owner=owner, scenario="B")

    def shutdown(self):
        self._executor.shutdown(wait=False)