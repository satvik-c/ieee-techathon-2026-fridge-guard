"""
FridgeGuard — SQLite database layer.

Tables:
  inventory  — items currently in the fridge (name, owner, timestamp)
  events     — log of all fridge interactions
  temp_log   — temperature readings from DHT11
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Optional


# Words that are unreliable for matching — either filler or color words
# that Groq frequently gets wrong due to fridge lighting conditions.
_STOPWORDS = {
    # filler
    "and", "or", "a", "the", "with", "of", "in", "on",
    # colors — intentionally excluded so color mismatches don't block matching
    "red", "orange", "yellow", "green", "blue", "purple", "pink",
    "black", "white", "grey", "gray", "silver", "gold", "brown",
    "dark", "light", "bright",
}

# Container/shape type words. If a query and a stored item have DIFFERENT
# container words, they cannot be the same item — skip that match entirely.
_CONTAINER_WORDS = {
    "box", "can", "bottle", "jar", "bag", "carton", "pouch",
    "tube", "cup", "bowl", "tray", "jug", "block", "wrapper", "packet",
    "cylindrical", "rectangular",
}


class DB:
    def __init__(self, db_path: str = "fridgeguard.db"):
        self.db_path = db_path
        self.conn    = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name   TEXT NOT NULL,
                owner       TEXT NOT NULL,
                added_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                actor       TEXT,
                action      TEXT NOT NULL,
                item_name   TEXT,
                item_owner  TEXT,
                scenario    TEXT
            );

            CREATE TABLE IF NOT EXISTS temp_log (
                timestamp   TEXT PRIMARY KEY,
                temp_f      REAL,
                humidity    REAL
            );
        """)
        self.conn.commit()

    # ── Inventory ────────────────────────────────────────────

    def add_item(self, item_name: str, owner: str) -> int:
        """Add an item to the fridge. Returns the row ID."""
        cur = self.conn.execute(
            "INSERT INTO inventory (item_name, owner, added_at) VALUES (?, ?, ?)",
            (item_name.lower(), owner, datetime.now().isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def remove_item(self, item_name: str) -> Optional[dict]:
        """
        Remove an item from inventory by name.
        Uses the same fuzzy matching as find_item to locate the row.
        Returns the removed item's info or None.
        """
        record = self.find_item(item_name)
        if record:
            self.conn.execute(
                "DELETE FROM inventory WHERE id = ?", (record["id"],)
            )
            self.conn.commit()
            return record
        return None

    def find_item(self, item_name: str) -> Optional[dict]:
        """
        Look up an item by name.

        Matching strategy:
          1. Exact match (case-insensitive) — always wins if found.
          2. Fuzzy match with container-type guard — if the query and a
             stored item have different container words (e.g. "box" vs "can"),
             that row is skipped entirely regardless of other word overlap.
             Requires at least 2 word matches to avoid false positives.
        """
        # 1. Exact match
        row = self.conn.execute(
            "SELECT * FROM inventory "
            "WHERE LOWER(item_name) = LOWER(?) "
            "ORDER BY added_at DESC LIMIT 1",
            (item_name,),
        ).fetchone()
        if row:
            return dict(row)

        # 2. Fuzzy match with container-type guard
        rows = self.conn.execute(
            "SELECT * FROM inventory ORDER BY added_at DESC"
        ).fetchall()
        if not rows:
            return None

        query_words     = set(item_name.lower().split()) - _STOPWORDS
        query_container = query_words & _CONTAINER_WORDS
        best_row        = None
        best_score      = 0

        for row in rows:
            stored_words     = set(row["item_name"].lower().split()) - _STOPWORDS
            stored_container = stored_words & _CONTAINER_WORDS

            # If both have container words and they don't overlap → wrong item
            # type entirely, skip regardless of other word overlap
            if query_container and stored_container and not (query_container & stored_container):
                continue

            overlap = len(query_words & stored_words)
            if overlap > best_score and overlap >= 2:
                best_score = overlap
                best_row   = row

        if best_row:
            print(f"[DB] Fuzzy matched '{item_name}' "
                  f"→ '{best_row['item_name']}' (score={best_score})")
            return dict(best_row)

        return None

    def get_inventory(self) -> list[dict]:
        """Return all items currently in the fridge."""
        rows = self.conn.execute(
            "SELECT * FROM inventory ORDER BY added_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_inventory_by_owner(self, owner: str) -> list[dict]:
        """Return all items owned by a specific roommate."""
        rows = self.conn.execute(
            "SELECT * FROM inventory "
            "WHERE LOWER(owner) = LOWER(?) ORDER BY added_at DESC",
            (owner,),
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_inventory(self):
        """Wipe all inventory (for testing/reset)."""
        self.conn.execute("DELETE FROM inventory")
        self.conn.commit()

    # ── Event logging ────────────────────────────────────────

    def log_event(
        self,
        actor:      Optional[str],
        action:     str,
        item_name:  Optional[str] = None,
        item_owner: Optional[str] = None,
        scenario:   Optional[str] = None,
    ):
        self.conn.execute(
            """INSERT INTO events
               (timestamp, actor, action, item_name, item_owner, scenario)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(), actor or "guest", action,
             item_name, item_owner, scenario),
        )
        self.conn.commit()

    def get_recent_events(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Temperature logging ──────────────────────────────────

    def log_temp(self, temp_f: float, humidity: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO temp_log "
            "(timestamp, temp_f, humidity) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), temp_f, humidity),
        )
        self.conn.commit()

    def get_recent_temps(self, minutes: int = 10) -> list[dict]:
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        rows   = self.conn.execute(
            "SELECT * FROM temp_log "
            "WHERE timestamp > ? ORDER BY timestamp DESC",
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Cleanup ──────────────────────────────────────────────

    def close(self):
        self.conn.close()