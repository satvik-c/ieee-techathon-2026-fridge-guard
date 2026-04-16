"""
FridgeGuard — Alert Engine.

Scenarios:
  A — Owner accesses own item        → private notification only
  B — Roommate takes another's item  → general + private to owner
  C — Guest / unknown takes item     → general + private to owner + host
"""

import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from analyzer     import Analyzer
from ble_resolver import BLEResolver
from db           import DB
from models       import ResolvedIdentity
from notifier     import Notifier


class AlertEngine:
    def __init__(
        self,
        analyzer:     Analyzer,
        ble_resolver: BLEResolver,
        db:           DB,
        config,
        notifier:     Notifier,
        min_api_gap:  float = 5.0,
    ):
        self.analyzer     = analyzer
        self.ble_resolver = ble_resolver
        self.db           = db
        self.config       = config
        self.notifier     = notifier
        self.min_api_gap  = min_api_gap

        self._executor      = ThreadPoolExecutor(max_workers=1)
        self._last_api_call = 0.0

    def on_door_close(
        self,
        before:     np.ndarray,
        after:      np.ndarray,
        identity:   ResolvedIdentity,
        scan_count: int = 0,
        duration:   float = 0.0,
    ):
        """Entry point — runs analysis in background thread."""
        inventory = self.db.get_inventory()
        b, a      = before.copy(), after.copy()
        self._executor.submit(
            self._run, b, a, identity, inventory, scan_count, duration
        )

    def _run(self, before: np.ndarray, after: np.ndarray,
             identity: ResolvedIdentity, inventory: list[dict],
             scan_count: int, duration: float):

        actor = identity.primary or "GUEST"
        self.notifier.door_closed(actor, scan_count, duration)

        # Cooldown
        elapsed = time.time() - self._last_api_call
        if elapsed < self.min_api_gap:
            wait = self.min_api_gap - elapsed
            self.notifier.status(f"API cooldown: {wait:.1f}s...")
            time.sleep(wait)
        self._last_api_call = time.time()

        self.notifier.status(
            f"Analyzing — actor: {actor}, inventory: {len(inventory)} item(s)"
        )
        try:
            result = self.analyzer.analyze(before, after, inventory=inventory)
        except Exception as e:
            self.notifier.status(f"Groq failed: {e}")
            return

        changes = result.get("changes", [])
        self.notifier.groq_result(changes)

        if not changes:
            return

        for change in changes:
            self._evaluate(identity, change["item"], change["action"])

    def _evaluate(self, identity: ResolvedIdentity, item: str, action: str):
        actor = identity.primary

        if action == "added":
            if actor:
                self.db.add_item(item_name=item, owner=actor)
                self.db.log_event(actor=actor, action="added",
                                  item_name=item, item_owner=actor, scenario="A")
                self.notifier.scenario_a_add(actor, item)
            else:
                self.notifier.status(f"Guest added '{item}' — ownership unassigned.")
                self.db.log_event(actor=None, action="added",
                                  item_name=item, scenario="C")

        elif action == "removed":
            record = self.db.find_item(item)
            owner  = record["owner"] if record else None

            if owner is None:
                self.notifier.status(
                    f"'{item}' removed but not in inventory "
                    f"(actor: {actor or 'unknown'})."
                )
                self.db.log_event(actor=actor, action="removed",
                                  item_name=item, scenario=None)
                return

            if identity.is_guest:
                # Scenario C
                host = self.ble_resolver.find_recent_nearby(window_sec=20)
                self.notifier.scenario_c(owner, item, host)
                self.db.remove_item(item)
                self.db.log_event(actor="guest", action="removed",
                                  item_name=item, item_owner=owner, scenario="C")

            elif actor == owner:
                # Scenario A
                self.notifier.scenario_a_remove(actor, item)
                self.db.remove_item(item)
                self.db.log_event(actor=actor, action="removed",
                                  item_name=item, item_owner=owner, scenario="A")

            else:
                # Scenario B
                self.notifier.scenario_b(actor, owner, item)
                self.db.remove_item(item)
                self.db.log_event(actor=actor, action="removed",
                                  item_name=item, item_owner=owner, scenario="B")

    def shutdown(self):
        self._executor.shutdown(wait=False)