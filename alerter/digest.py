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


def _gb(value):
    return "—" if value is None else ("%.1f GB" % (value / 1e9))


def _signed_gb(value):
    if value is None:
        return "—"
    return "%s%.1f GB" % ("+" if value >= 0 else "", value / 1e9)


def render(data):
    """Subject and HTML body. Always renders every data section; a quiet day
    changes the framing, not the content."""
    date = time.strftime("%d %b %Y", time.localtime(data["now"] or time.time()))
    subject = "arrstack daily digest — " + date
    rows = []
    if data["quiet"]:
        rows.append("<p>Quiet day today, see you tomorrow!</p>")
    else:
        for section, items in sorted(data["arrived"].items()):
            if not items:
                continue
            entries = "".join("<li>%s</li>" % item for item in items)
            rows.append("<h2>%s</h2><ul>%s</ul>" % (section.title(), entries))
    capacity = data["capacity"]
    rows.append(
        "<h2>Capacity</h2><ul>"
        "<li>Storage Box: %s of %s (%s in 24h)</li>"
        "<li>Seeding: %s (%s in 24h)</li>"
        "<li>VPS disk: %s</li>"
        "</ul>"
        % (
            _gb(capacity.get("box_used_bytes")),
            _gb(capacity.get("box_total_bytes")),
            _signed_gb(capacity.get("box_delta_bytes")),
            _gb(capacity.get("seeding_bytes")),
            _signed_gb(capacity.get("seeding_delta_bytes")),
            _gb(capacity.get("vps_disk_used_bytes")),
        )
    )
    transitions = data["health"]["transitions"]
    if transitions:
        entries = "".join(
            "<li>%s: %s &rarr; %s</li>" % (t["rule"], t["from_state"], t["to_state"])
            for t in transitions
        )
        rows.append("<h2>Health</h2><ul>%s</ul>" % entries)
    else:
        rows.append("<h2>Health</h2><p>No alerts in the last 24 hours.</p>")
    body = (
        "<html><body style=\"font-family:system-ui,sans-serif;max-width:640px\">"
        "<h1>arrstack — %s</h1>%s</body></html>" % (date, "".join(rows))
    )
    return subject, body
