"""Assembling the daily digest's data."""
import sqlite3
import time

DAY = 86400

CAPACITY_METRICS = (
    ("box_used_bytes", "hetzner_box.used_bytes"),
    ("box_total_bytes", "hetzner_box.total_bytes"),
    ("seeding_bytes", "qbt.seeding_bytes"),
    ("vps_disk_used_bytes", "vps_disk.used_bytes"),
)


def series_at(db_path, metric, ts):
    """The most recent sample at or before ts, or None."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM samples WHERE metric = ? AND ts <= ?"
            " ORDER BY ts DESC LIMIT 1",
            (metric, ts),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else float(row[0])


def _delta(db_path, metric, now):
    before = series_at(db_path, metric, now - DAY)
    after = series_at(db_path, metric, now)
    if before is None or after is None:
        return None
    return after - before


def gather(db_path, store, now, fetchers):
    """Everything the digest needs.

    `fetchers` maps a section name to a zero-argument callable returning a list
    of item names. Each is wrapped: one application being unreachable costs its
    own section and nothing else.
    """
    arrived = {}
    for name, fetch in fetchers.items():
        try:
            arrived[name] = list(fetch())
        except Exception:
            arrived[name] = []
    capacity = {key: series_at(db_path, metric, now) for key, metric in CAPACITY_METRICS}
    capacity["box_delta_bytes"] = _delta(db_path, "hetzner_box.used_bytes", now)
    capacity["seeding_delta_bytes"] = _delta(db_path, "qbt.seeding_bytes", now)
    return {
        "now": now,
        "arrived": arrived,
        "quiet": not any(arrived.values()),
        "capacity": capacity,
        "health": {"transitions": store.transitions_since(now - DAY),
                   "open": store.open_alerts()},
    }
