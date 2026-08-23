"""Entry point: evaluation loop plus a small HTTP surface."""
import html
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import digest
from evaluate import ERROR, HEALTHY, classify, resolve_value
from notify import format_alert, send_email, send_ntfy
from rules import RuleError, load_rules
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


CONFIG = {
    "port": int(os.environ.get("ALERTER_PORT", "8100")),
    "interval": int(os.environ.get("ALERTER_INTERVAL", "300")),
    "metrics_db": os.environ.get("METRICS_DB", "/metrics/metrics.db"),
    "state_db": os.environ.get("ALERTER_DB", "/data/alerts.db"),
    "rules": os.environ.get("ALERTER_RULES", "/app/rules.toml"),
    "ntfy_server": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
    "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
    "resend_key": os.environ.get("RESEND_API_KEY", ""),
    "resend_from": os.environ.get("RESEND_FROM", ""),
    "resend_to": os.environ.get("RESEND_TO", ""),
    "digest_hour": int(os.environ.get("DIGEST_HOUR", "8")),
    "stale_after": int(os.environ.get("ALERTER_STALE_AFTER", "900")),
}

STORE = None


def push(title, body, tags):
    if not CONFIG["ntfy_topic"]:
        log("no NTFY_TOPIC set; would have sent: " + title)
        return False
    return send_ntfy(CONFIG["ntfy_server"], CONFIG["ntfy_topic"], title, body, tags)


def fetchers():
    """Left empty until the arr history calls are added; gather() tolerates it
    and the digest simply reports a quiet day."""
    return {}


def build_digest():
    return digest.render(
        digest.gather(CONFIG["metrics_db"], STORE, int(time.time()), fetchers())
    )


def check_staleness():
    newest = newest_ts(CONFIG["metrics_db"])
    age = None if newest is None else int(time.time()) - newest
    stale = age is None or age > CONFIG["stale_after"]
    state = ERROR if stale else HEALTHY
    previous = STORE.current(STALE_RULE_NAME)
    confirmed = STORE.observe(STALE_RULE_NAME, state, 1)
    if confirmed is None or previous is None:
        return
    title = STALE_RULE_NAME + (" stalled" if stale else " recovered")
    tags = "thumbsdown" if stale else "thumbsup"
    body = "No metrics sample for %s seconds." % age if stale else "Collection resumed."
    STORE.record(int(time.time()), STALE_RULE_NAME, previous, confirmed, age, title, body)
    push(title, body, tags)


def loop():
    while True:
        started = time.time()
        try:
            rules = load_rules(CONFIG["rules"])
            samples = latest_samples(CONFIG["metrics_db"], CONFIG["interval"] * 4)
            evaluate_once(rules, samples, STORE, push)
            check_staleness()
        except RuleError as exc:
            log("rules unusable: " + str(exc))
        except Exception:
            log("evaluation cycle raised:\n" + traceback.format_exc())
        delay = CONFIG["interval"] - (time.time() - started)
        time.sleep(max(5.0, delay))


def digest_loop():
    sent_on = None
    while True:
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if now.tm_hour == CONFIG["digest_hour"] and sent_on != today:
            sent_on = today
            try:
                subject, body = build_digest()
                if CONFIG["resend_key"]:
                    if not send_email(CONFIG["resend_key"], CONFIG["resend_from"],
                                      CONFIG["resend_to"], subject, body):
                        log("digest email failed")
                else:
                    log("no RESEND_API_KEY set; digest not sent")
            except Exception:
                log("digest raised:\n" + traceback.format_exc())
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/healthz":
                self._send(200, "ok", "text/plain; charset=utf-8")
            elif path == "/alerts":
                self._send(200, render_alerts_page(STORE.open_alerts()))
            elif path == "/digest/preview":
                self._send(200, build_digest()[1])
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception:
            log("request failed:\n" + traceback.format_exc())
            self._send(500, "error", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


def main():
    global STORE
    STORE = Store(CONFIG["state_db"])
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=digest_loop, daemon=True).start()
    log("serving on port %d" % CONFIG["port"])
    ThreadingHTTPServer(("0.0.0.0", CONFIG["port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
