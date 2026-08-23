"""Entry point: evaluation loop plus a small HTTP surface."""
import html
import os
import sqlite3
import sys
import time
import traceback

from evaluate import ERROR, HEALTHY, classify, resolve_value
from notify import format_alert
from state import Store

STALE_RULE_NAME = "Metrics collection"


def log(message):
    sys.stdout.write("[alerter] " + message + "\n")
    sys.stdout.flush()


def latest_samples(db_path, horizon_seconds):
    """Newest value per metric within the horizon, or {} if unreadable.

    Returning empty rather than raising keeps the loop alive when metrics is
    down, which is exactly when the staleness rule needs to fire.
    """
    cutoff = int(time.time()) - horizon_seconds
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT metric, value FROM samples WHERE ts >= ?"
            " AND ts = (SELECT MAX(ts) FROM samples s2 WHERE s2.metric = samples.metric)",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {metric: float(value) for metric, value in rows}


def newest_ts(db_path):
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT MAX(ts) FROM samples").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None or row[0] is None else int(row[0])


def evaluate_once(rules, samples, store, send):
    """One pass over every rule. Returns the number of notifications sent."""
    sent = 0
    now = int(time.time())
    for rule in rules:
        value = resolve_value(rule, samples)
        if value is None:
            continue
        state = classify(rule, value)
        previous = store.current(rule.name)
        confirmed = store.observe(rule.name, state, rule.debounce)
        if confirmed is None or previous is None:
            continue
        title, body, tags = format_alert(rule, previous, confirmed, value)
        store.record(now, rule.name, previous, confirmed, value, title, body)
        if send(title, body, tags):
            sent += 1
        else:
            log("notification failed for " + rule.name)
    return sent


PAGE_CSS = """
:root { --bg:#0f172a; --card:#16203a; --line:#2b3950; --fg:#e8eef8; --dim:#a7b4cb; }
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:14px/1.5 ui-sans-serif, system-ui, sans-serif; color-scheme:dark; }
h1 { font-size:15px; margin:0 0 12px; }
details { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:10px 12px; margin-bottom:8px; }
summary { cursor:pointer; font-weight:600; }
pre { white-space:pre-wrap; color:var(--dim); margin:8px 0 0; font-size:12.5px; }
.meta { color:var(--dim); font-size:12px; margin-top:6px; }
.quiet { color:var(--dim); }
"""


def render_alerts_page(alerts):
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Open alerts</title><style>", PAGE_CSS, "</style></head><body>",
        "<h1>Open alerts</h1>",
    ]
    if not alerts:
        parts.append("<p class=\"quiet\">No open alerts.</p>")
    for alert in alerts:
        when = time.strftime("%d %b %H:%M", time.localtime(alert["ts"]))
        value = "" if alert["value"] is None else (" &middot; %.1f" % alert["value"])
        parts.append(
            "<details><summary>%s</summary><pre>%s</pre>"
            "<p class=\"meta\">since %s%s</p></details>"
            % (
                html.escape(alert["title"] or alert["rule"]),
                html.escape(alert["body"] or ""),
                when,
                value,
            )
        )
    parts.append("</body></html>")
    return "".join(parts)
