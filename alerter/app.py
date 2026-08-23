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
    "lidarr_url": os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/"),
    "lidarr_api_key": os.environ.get("LIDARR_API_KEY", ""),
    "sonarr_url": os.environ.get("SONARR_URL", "http://localhost:8989").rstrip("/"),
    "sonarr_api_key": os.environ.get("SONARR_API_KEY", ""),
    "radarr_url": os.environ.get("RADARR_URL", "http://localhost:7878").rstrip("/"),
    "radarr_api_key": os.environ.get("RADARR_API_KEY", ""),
    "bazarr_url": os.environ.get("BAZARR_URL", "http://localhost:6767").rstrip("/"),
    "bazarr_api_key": os.environ.get("BAZARR_API_KEY", ""),
    "seerr_url": os.environ.get("SEERR_URL", "http://localhost:5055").rstrip("/"),
    "seerr_api_key": os.environ.get("SEERR_API_KEY", ""),
    "qbt_url": os.environ.get("QBT_URL", "http://172.28.0.10:8080").rstrip("/"),
    "qbt_username": os.environ.get("QBT_USERNAME", ""),
    "qbt_password": os.environ.get("QBT_PASSWORD", ""),
}

STORE = None


def push(title, body, tags):
    if not CONFIG["ntfy_topic"]:
        log("no NTFY_TOPIC set; would have sent: " + title)
        return False
    return send_ntfy(CONFIG["ntfy_server"], CONFIG["ntfy_topic"], title, body, tags)


def fetchers():
    result = {}
    window_start, now = _digest_window()
    if CONFIG["lidarr_api_key"]:
        result["music"] = lambda: digest.fetch_lidarr_arrivals(
            CONFIG["lidarr_url"], CONFIG["lidarr_api_key"], window_start, now
        )
    if CONFIG["sonarr_api_key"]:
        result["tv"] = lambda: digest.fetch_sonarr_arrivals(
            CONFIG["sonarr_url"], CONFIG["sonarr_api_key"], window_start, now
        )
    if CONFIG["radarr_api_key"]:
        result["movies"] = lambda: digest.fetch_radarr_arrivals(
            CONFIG["radarr_url"], CONFIG["radarr_api_key"], window_start, now
        )
    if CONFIG["bazarr_api_key"]:
        result["subtitles"] = lambda: digest.fetch_bazarr_arrivals(
            CONFIG["bazarr_url"], CONFIG["bazarr_api_key"], window_start, now
        )
    return result


def requests_fetcher():
    if not CONFIG["seerr_api_key"]:
        return {}
    window_start, now = _digest_window()
    return digest.fetch_requests(CONFIG["seerr_url"], CONFIG["seerr_api_key"], window_start, now)


def contributed_fetcher():
    if not CONFIG["qbt_username"]:
        return None
    return digest.fetch_mean_seed_ratio(
        CONFIG["qbt_url"], CONFIG["qbt_username"], CONFIG["qbt_password"]
    )


def left_fetcher():
    if not CONFIG["qbt_username"]:
        return []
    return digest.fetch_torrent_snapshot(
        CONFIG["qbt_url"], CONFIG["qbt_username"], CONFIG["qbt_password"]
    )


_WINDOW = None


def _digest_window():
    """The (window_start, now) pair for the digest build currently in
    progress. fetchers() closures need the same pair build_digest() computed,
    rather than each recomputing time.time() independently."""
    if _WINDOW is None:
        now = int(time.time())
        return now - digest.DAY, now
    return _WINDOW


def build_digest():
    """Renders the digest and returns (subject, body, data). Never mutates
    STORE: /digest/preview can call this freely, and the send path commits
    the window and torrent snapshot only after Resend confirms delivery."""
    global _WINDOW
    now = int(time.time())
    window_start = STORE.last_digest_sent_at()
    if window_start is None:
        window_start = now - digest.DAY
    _WINDOW = (window_start, now)
    try:
        data = digest.gather(
            CONFIG["metrics_db"], STORE, window_start, now, fetchers(),
            requests_fetcher=requests_fetcher,
            contributed_fetcher=contributed_fetcher,
            left_fetcher=left_fetcher,
        )
    finally:
        _WINDOW = None
    subject, body = digest.render(data)
    return subject, body, data


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


def send_digest_once():
    """Builds, sends and — only on confirmed delivery — commits the digest's
    window and torrent snapshot. A failed send leaves both in place so the
    next run covers what this one missed."""
    subject, body, data = build_digest()
    if not CONFIG["resend_key"]:
        log("no RESEND_API_KEY set; digest not sent")
        return False
    if not send_email(CONFIG["resend_key"], CONFIG["resend_from"],
                      CONFIG["resend_to"], subject, body):
        log("digest email failed")
        return False
    STORE.mark_digest_sent(data["now"])
    if data["_current_torrents"] is not None:
        STORE.commit_torrent_snapshot(data["now"], data["_current_torrents"])
    return True


def digest_loop():
    sent_on = None
    while True:
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if now.tm_hour == CONFIG["digest_hour"] and sent_on != today:
            sent_on = today
            try:
                send_digest_once()
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
