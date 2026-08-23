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
CREATE TABLE IF NOT EXISTS digest_meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS torrent_snapshot (
    ts INTEGER NOT NULL,
    hash TEXT NOT NULL,
    name TEXT NOT NULL,
    bytes REAL NOT NULL
);
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

    def last_digest_sent_at(self):
        """The ts of the last successfully sent digest, or None before the first."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM digest_meta WHERE key = 'last_digest_sent_at'"
            ).fetchone()
        return None if row is None else int(row["value"])

    def mark_digest_sent(self, ts):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO digest_meta (key, value) VALUES ('last_digest_sent_at', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (ts,),
            )

    def previous_torrent_snapshot(self):
        """Items from the last committed snapshot, or None before the first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hash, name, bytes FROM torrent_snapshot"
            ).fetchall()
        if not rows:
            return None
        return [dict(r) for r in rows]

    def commit_torrent_snapshot(self, ts, items):
        """Replace the stored snapshot. Call only after the digest that used
        the previous snapshot for its diff has actually been sent."""
        with self._connect() as conn:
            conn.execute("DELETE FROM torrent_snapshot")
            conn.executemany(
                "INSERT INTO torrent_snapshot (ts, hash, name, bytes) VALUES (?, ?, ?, ?)",
                [
                    (ts, item["hash"], item.get("name", "unknown"),
                     float(item.get("bytes") or 0))
                    for item in items
                    if item.get("hash")
                ],
            )
