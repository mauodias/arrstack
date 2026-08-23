"""Confirmed state per rule, and the log of transitions."""
import sqlite3
from evaluate import HEALTHY

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_state (
    rule TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    pending TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transitions (
    ts INTEGER NOT NULL,
    rule TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    value REAL,
    title TEXT,
    body TEXT
);
CREATE INDEX IF NOT EXISTS transitions_ts ON transitions (ts);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def current(self, rule_name):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM rule_state WHERE rule = ?", (rule_name,)
            ).fetchone()
        return row["state"] if row else None

    def observe(self, rule_name, state, debounce):
        """Record one observation; return the new state only when it sticks.

        A change must be seen `debounce` times in a row, so a value sitting on
        its threshold cannot alternate between notifications.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, pending, pending_count FROM rule_state WHERE rule = ?",
                (rule_name,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO rule_state (rule, state, pending, pending_count)"
                    " VALUES (?, ?, NULL, 0)",
                    (rule_name, state),
                )
                return None
            if state == row["state"]:
                conn.execute(
                    "UPDATE rule_state SET pending = NULL, pending_count = 0"
                    " WHERE rule = ?",
                    (rule_name,),
                )
                return None
            count = row["pending_count"] + 1 if state == row["pending"] else 1
            if count >= debounce:
                conn.execute(
                    "UPDATE rule_state SET state = ?, pending = NULL, pending_count = 0"
                    " WHERE rule = ?",
                    (state, rule_name),
                )
                return state
            conn.execute(
                "UPDATE rule_state SET pending = ?, pending_count = ? WHERE rule = ?",
                (state, count, rule_name),
            )
            return None

    def record(self, ts, rule_name, from_state, to_state, value, title, body):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO transitions"
                " (ts, rule, from_state, to_state, value, title, body)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, rule_name, from_state, to_state, value, title, body),
            )

    def open_alerts(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.* FROM transitions t"
                " JOIN (SELECT rule, MAX(ts) AS ts FROM transitions GROUP BY rule) last"
                "   ON t.rule = last.rule AND t.ts = last.ts"
                " WHERE t.to_state != ?"
                " ORDER BY t.ts DESC",
                (HEALTHY,),
            ).fetchall()
        return [
            {
                "ts": r["ts"],
                "rule": r["rule"],
                "state": r["to_state"],
                "value": r["value"],
                "title": r["title"],
                "body": r["body"],
            }
            for r in rows
        ]

    def transitions_since(self, ts):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transitions WHERE ts >= ? ORDER BY ts", (ts,)
            ).fetchall()
        return [dict(r) for r in rows]
