"""Collects stack metrics into SQLite and serves them as server-rendered SVG charts."""
import http.cookiejar
import http.server
import json
import os
import socketserver
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

DB_PATH = os.environ.get("METRICS_DB", "/data/metrics.db")
PORT = int(os.environ.get("METRICS_PORT", "8099"))
INTERVAL = int(os.environ.get("METRICS_INTERVAL", "300"))
DISK_PATH = os.environ.get("METRICS_DISK_PATH", "/hostdisk/qbittorrent")
QBT_URL = os.environ.get("QBT_URL", "http://172.28.0.10:8080").rstrip("/")
QBT_USERNAME = os.environ.get("QBT_USERNAME", "")
QBT_PASSWORD = os.environ.get("QBT_PASSWORD", "")
LIDARR_URL = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "")
RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
HETZNER_API_TOKEN = os.environ.get("HETZNER_API_TOKEN", "")
SLSKD_URL = os.environ.get("SLSKD_URL", "http://172.28.0.10:5030").rstrip("/")
SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY", "")
HTTP_TIMEOUT = 30

TORRENT_STATES = (
    "downloading", "stalledDL", "queuedDL", "uploading", "stalledUP",
    "queuedUP", "pausedDL", "pausedUP", "stoppedDL", "stoppedUP",
    "checkingDL", "checkingUP", "error", "missingFiles", "metaDL",
)

TORRENT_SEEDING_STATES = (
    "uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP",
    "forcedUP", "checkingUP",
)


def log(msg):
    print("[metrics] " + msg, flush=True)


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = db_connect()
    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "ts INTEGER NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_metric_ts ON samples (metric, ts)"
        )
    conn.close()


def http_json(url, headers=None, opener=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {})
    open_fn = opener.open if opener else urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def collect_disk():
    st = os.statvfs(DISK_PATH)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {
        "vps_disk.total_bytes": float(total),
        "vps_disk.used_bytes": float(used),
        "vps_disk.free_bytes": float(free),
    }


def collect_memory():
    """/proc/meminfo inside a container without its own memory namespace
    reports the host's figures, which is what matters here: the kernel
    OOM killer scores against host-wide pressure, not per-container."""
    fields = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                fields[parts[0].rstrip(":")] = float(parts[1]) * 1024.0
    total = fields.get("MemTotal", 0.0)
    avail = fields.get("MemAvailable", 0.0)
    swap_total = fields.get("SwapTotal", 0.0)
    swap_free = fields.get("SwapFree", 0.0)
    out = {
        "vps_mem.total_bytes": total,
        "vps_mem.available_bytes": avail,
        "vps_mem.used_bytes": total - avail,
        "vps_swap.total_bytes": swap_total,
        "vps_swap.used_bytes": swap_total - swap_free,
    }
    return out


_PREV_CPU = None


def collect_cpu():
    """/proc/stat and /proc/loadavg inside a container without its own PID
    namespace report host-wide figures, same as /proc/meminfo above."""
    global _PREV_CPU
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("cpu "):
                fields = [float(x) for x in line.split()[1:]]
                break
        else:
            raise RuntimeError("no cpu line in /proc/stat")
    idle = fields[3] + fields[4] if len(fields) > 4 else fields[3]
    total = sum(fields)
    out = {}
    if _PREV_CPU is not None:
        prev_idle, prev_total = _PREV_CPU
        d_total = total - prev_total
        d_idle = idle - prev_idle
        if d_total > 0:
            out["vps_cpu.percent"] = max(0.0, min(100.0, (d_total - d_idle) * 100.0 / d_total))
    _PREV_CPU = (idle, total)
    with open("/proc/loadavg") as fh:
        load1 = float(fh.read().split()[0])
    out["vps_cpu.load1"] = load1
    return out


def collect_hetzner():
    if not HETZNER_API_TOKEN:
        raise RuntimeError("HETZNER_API_TOKEN is not set")
    data = http_json(
        "https://api.hetzner.com/v1/storage_boxes",
        headers={"Authorization": "Bearer " + HETZNER_API_TOKEN},
    )
    boxes = data.get("storage_boxes") or []
    if not boxes:
        raise RuntimeError("no storage boxes returned")
    box = boxes[0]
    return {
        "hetzner_box.used_bytes": float(box["stats"]["size"]),
        "hetzner_box.total_bytes": float(box["storage_box_type"]["size"]),
    }


def collect_qbittorrent():
    # qBittorrent names its session cookie QBT_SID_<port>, so the jar tracks it
    # by whatever name the server sends rather than a hardcoded one.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode(
        {"username": QBT_USERNAME, "password": QBT_PASSWORD}
    ).encode("utf-8")
    login = urllib.request.Request(
        QBT_URL + "/api/v2/auth/login", data=body, headers={"Referer": QBT_URL}
    )
    with opener.open(login, timeout=HTTP_TIMEOUT) as resp:
        resp.read()
    if not len(jar):
        raise RuntimeError("qBittorrent login returned no session cookie")

    info = http_json(
        QBT_URL + "/api/v2/transfer/info", headers={"Referer": QBT_URL}, opener=opener
    )
    torrents = http_json(
        QBT_URL + "/api/v2/torrents/info", headers={"Referer": QBT_URL}, opener=opener
    )
    out = {
        # transfer/info speeds and limits are all bytes per second.
        "qbt.dl_speed_bps": float(info.get("dl_info_speed", 0)),
        "qbt.ul_speed_bps": float(info.get("up_info_speed", 0)),
        "qbt.dl_session_bytes": float(info.get("dl_info_data", 0)),
        "qbt.ul_session_bytes": float(info.get("up_info_data", 0)),
        "qbt.torrents_total": float(len(torrents)),
    }
    counts = {state: 0 for state in TORRENT_STATES}
    downloads_bytes = 0.0
    seeding_bytes = 0.0
    in_progress_bytes = 0.0
    for t in torrents:
        state = t.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
        completed = float(t.get("completed") or 0)
        downloads_bytes += completed
        if state in TORRENT_SEEDING_STATES:
            seeding_bytes += completed
        else:
            in_progress_bytes += completed
    for state, n in counts.items():
        out["qbt.torrents." + state] = float(n)
    out["qbt.downloads_bytes"] = downloads_bytes
    out["qbt.seeding_bytes"] = seeding_bytes
    out["qbt.in_progress_bytes"] = in_progress_bytes
    return out


def collect_lidarr():
    if not LIDARR_API_KEY:
        raise RuntimeError("LIDARR_API_KEY is not set")
    headers = {"X-Api-Key": LIDARR_API_KEY}
    missing = http_json(
        LIDARR_URL + "/api/v1/wanted/missing?pageSize=1", headers=headers
    )
    queue = http_json(LIDARR_URL + "/api/v1/queue?pageSize=1", headers=headers)
    artists = http_json(LIDARR_URL + "/api/v1/artist", headers=headers)
    size = 0.0
    for a in artists:
        stats = a.get("statistics") or {}
        size += float(stats.get("sizeOnDisk") or 0)
    return {
        "lidarr.wanted_missing": float(missing.get("totalRecords", 0)),
        "lidarr.queue_count": float(queue.get("totalRecords", 0)),
        "lidarr.size_on_disk_bytes": size,
        "lidarr.artist_count": float(len(artists)),
    }


def collect_radarr():
    if not RADARR_API_KEY:
        raise RuntimeError("RADARR_API_KEY is not set")
    headers = {"X-Api-Key": RADARR_API_KEY}
    movies = http_json(RADARR_URL + "/api/v3/movie", headers=headers)
    size = 0.0
    for m in movies:
        size += float(m.get("sizeOnDisk") or 0)
    return {
        "radarr.size_on_disk_bytes": size,
        "radarr.movie_count": float(len(movies)),
    }


def collect_sonarr():
    if not SONARR_API_KEY:
        raise RuntimeError("SONARR_API_KEY is not set")
    headers = {"X-Api-Key": SONARR_API_KEY}
    series = http_json(SONARR_URL + "/api/v3/series", headers=headers)
    size = 0.0
    for s in series:
        stats = s.get("statistics") or {}
        size += float(stats.get("sizeOnDisk") or 0)
    return {
        "sonarr.size_on_disk_bytes": size,
        "sonarr.series_count": float(len(series)),
    }


def _slskd_transfer_files(payload):
    files = []
    for user in payload:
        for directory in user.get("directories") or []:
            files.extend(directory.get("files") or [])
    return files


def collect_slskd():
    if not SLSKD_API_KEY:
        raise RuntimeError("SLSKD_API_KEY is not set")
    headers = {"X-API-Key": SLSKD_API_KEY}
    app = http_json(SLSKD_URL + "/api/v0/application", headers=headers)
    downloads = http_json(SLSKD_URL + "/api/v0/transfers/downloads", headers=headers)
    uploads = http_json(SLSKD_URL + "/api/v0/transfers/uploads", headers=headers)

    server = app.get("server") or {}
    stats = ((app.get("user") or {}).get("statistics")) or {}
    scanning = bool((app.get("shares") or {}).get("scanning"))

    dl_files = _slskd_transfer_files(downloads)
    ul_files = _slskd_transfer_files(uploads)

    dl_active = [f for f in dl_files if "InProgress" in f.get("state", "")]
    dl_queued = [f for f in dl_files if "Queued" in f.get("state", "")]
    dl_completed = [f for f in dl_files if f.get("state", "").startswith("Completed")]
    dl_outcomes = {"succeeded": 0, "errored": 0, "cancelled": 0, "rejected": 0, "aborted": 0}
    for f in dl_completed:
        state = f.get("state", "")
        for outcome in dl_outcomes:
            if outcome.capitalize() in state:
                dl_outcomes[outcome] += 1
                break
    success_rate = (
        dl_outcomes["succeeded"] * 100.0 / len(dl_completed) if dl_completed else 0.0
    )

    ul_active = [f for f in ul_files if "InProgress" in f.get("state", "")]

    out = {
        "slskd.connected": 1.0 if server.get("isLoggedIn") else 0.0,
        "slskd.downloads.active": float(len(dl_active)),
        "slskd.downloads.queued": float(len(dl_queued)),
        "slskd.downloads.speed_bps": float(sum(f.get("averageSpeed", 0) for f in dl_active)),
        "slskd.downloads.succeeded": float(dl_outcomes["succeeded"]),
        "slskd.downloads.errored": float(dl_outcomes["errored"]),
        "slskd.downloads.cancelled": float(dl_outcomes["cancelled"]),
        "slskd.downloads.rejected": float(dl_outcomes["rejected"]),
        "slskd.downloads.aborted": float(dl_outcomes["aborted"]),
        "slskd.downloads.success_rate": success_rate,
        "slskd.uploads.active": float(len(ul_active)),
        "slskd.uploads.speed_bps": float(sum(f.get("averageSpeed", 0) for f in ul_active)),
        "slskd.uploads.total": float(stats.get("uploadCount", 0)),
        "slskd.scanning": 1.0 if scanning else 0.0,
    }
    # user.statistics reports a share of zero files for the whole
    # duration of a rescan, which slskd runs on every start. Omitting
    # the sample holds the chart at its last value rather than drawing
    # a drop to zero and back on each redeploy.
    if not scanning:
        out["slskd.shared.files"] = float(stats.get("fileCount", 0))
        out["slskd.shared.dirs"] = float(stats.get("directoryCount", 0))
    return out


OLD_COLLECTION_PATH = os.environ.get("OLD_COLLECTION_PATH", "/old-collection")
OLD_COLLECTION_EVERY = int(os.environ.get("OLD_COLLECTION_EVERY", "3600"))
_OLD_COLLECTION_CACHE = {"at": 0.0, "value": None}


def collect_old_collection():
    """Size and folder count of the legacy library still awaiting import.

    Walking several thousand files over the FUSE mount is far too expensive to
    repeat every cycle, and the figure only moves when an import runs, so the
    result is cached and refreshed on its own slower schedule.
    """
    now = time.time()
    cached = _OLD_COLLECTION_CACHE["value"]
    if cached is not None and now - _OLD_COLLECTION_CACHE["at"] < OLD_COLLECTION_EVERY:
        return dict(cached)
    total = 0
    files = 0
    folders = 0
    for entry in os.scandir(OLD_COLLECTION_PATH):
        if entry.is_dir(follow_symlinks=False):
            folders += 1
    for root, _dirs, names in os.walk(OLD_COLLECTION_PATH):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                continue
    out = {
        "old_collection.size_bytes": float(total),
        "old_collection.folders": float(folders),
        "old_collection.files": float(files),
        "old_collection.scanned_at": float(int(now)),
    }
    _OLD_COLLECTION_CACHE["at"] = now
    _OLD_COLLECTION_CACHE["value"] = out
    return dict(out)


COLLECTORS = (
    ("disk", collect_disk),
    ("memory", collect_memory),
    ("cpu", collect_cpu),
    ("hetzner", collect_hetzner),
    ("qbittorrent", collect_qbittorrent),
    ("lidarr", collect_lidarr),
    ("radarr", collect_radarr),
    ("sonarr", collect_sonarr),
    ("slskd", collect_slskd),
    ("old_collection", collect_old_collection),
)


def collect_once():
    """Runs every collector, isolating failures so one dead service never
    suppresses the others or ends the thread."""
    ts = int(time.time())
    rows = []
    for name, fn in COLLECTORS:
        try:
            for metric, value in fn().items():
                rows.append((ts, metric, float(value)))
            rows.append((ts, "collect_ok." + name, 1.0))
        except Exception as exc:
            rows.append((ts, "collect_ok." + name, 0.0))
            log("collector " + name + " failed: " + repr(exc))
    try:
        conn = db_connect()
        with conn:
            conn.executemany("INSERT INTO samples (ts, metric, value) VALUES (?, ?, ?)", rows)
        conn.close()
    except Exception:
        log("failed to write samples:\n" + traceback.format_exc())
    return len(rows)


def collector_loop():
    while True:
        started = time.time()
        try:
            n = collect_once()
            log("wrote " + str(n) + " samples")
        except Exception:
            log("collection cycle raised:\n" + traceback.format_exc())
        delay = INTERVAL - (time.time() - started)
        time.sleep(delay if delay > 0 else 1)


RANGES = (("24h", 86400), ("7d", 604800), ("30d", 2592000))
RANGE_SECONDS = dict(RANGES)


def series_for(conn, metric, since):
    cur = conn.execute(
        "SELECT ts, value FROM samples WHERE metric = ? AND ts >= ? ORDER BY ts",
        (metric, since),
    )
    return cur.fetchall()


def latest(conn, metric):
    cur = conn.execute(
        "SELECT ts, value FROM samples WHERE metric = ? ORDER BY ts DESC LIMIT 1", (metric,)
    )
    return cur.fetchone()


def downsample(points, buckets=180):
    """Averages points into fixed-width time buckets so a 30-day window costs
    the same to render as a 24-hour one."""
    if len(points) <= buckets:
        return points
    lo = points[0][0]
    hi = points[-1][0]
    span = max(hi - lo, 1)
    acc = {}
    for ts, value in points:
        idx = int((ts - lo) * buckets / span)
        if idx >= buckets:
            idx = buckets - 1
        t, v, n = acc.get(idx, (0, 0.0, 0))
        acc[idx] = (t + ts, v + value, n + 1)
    return [(t // n, v / n) for _, (t, v, n) in sorted(acc.items())]


def ratio_series(num, den):
    by_ts = dict(den)
    out = []
    for ts, value in num:
        total = by_ts.get(ts)
        if total:
            out.append((ts, value * 100.0 / total))
    return out


def fmt_bytes(v):
    """Decimal units, matching how Homepage's own widgets render byte
    counts. The same figure in binary units reads about 7% smaller at
    GB scale, which looks like two services disagreeing about storage."""
    v = float(v)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(v) < 1000 or unit == "PB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (v, unit)
        v /= 1000.0
    return str(v)


def fmt_bps(v):
    return fmt_bytes(v) + "/s"


def fmt_pct(v):
    return "%.1f%%" % v


def fmt_count(v):
    return "%.0f" % v


FORMATTERS = {"bytes": fmt_bytes, "bps": fmt_bps, "pct": fmt_pct, "count": fmt_count}


def nice_ticks(lo, hi, target=4):
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / target
    mag = 10.0 ** int(("%e" % raw).split("e")[1])
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if raw <= step:
            break
    start = step * int(lo / step)
    if start > lo:
        start -= step
    ticks = []
    t = start
    # Runs one step past `hi` so the topmost gridline is always at or above the
    # largest value, instead of leaving the peak floating in unlabelled space.
    while t < hi + step * 1.001:
        if t >= lo - step * 0.001:
            ticks.append(t)
        t += step
    return ticks


def esc(s):
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# The SVG scales uniformly to the card width, so these user units are chosen
# close to the rendered pixel size at a typical card width — that keeps the
# axis type legible instead of shrunken or horizontally squashed.
W, H = 412, 172
PAD_L, PAD_R, PAD_T, PAD_B = 64, 10, 12, 24


def prep_chart(series, zero_based=True):
    """Computes the geometry (time/value bounds and downsampled points) shared
    by the server-rendered SVG and the JSON payload the crosshair script reads,
    so both draw from identical coordinates."""
    drawable = [s for s in series if len(s[2]) >= 1]
    if not drawable:
        return None

    all_ts = [p[0] for _, _, pts in drawable for p in pts]
    all_v = [p[1] for _, _, pts in drawable for p in pts]
    t0, t1 = min(all_ts), max(all_ts)
    if t1 == t0:
        t1 = t0 + 1
    vmin = 0.0 if zero_based else min(all_v)
    vmax = max(all_v)
    if vmax <= vmin:
        vmax = vmin + 1.0
    ticks = nice_ticks(vmin, vmax)
    vmin, vmax = min(vmin, ticks[0]), max(vmax, ticks[-1])
    downsampled = [(label, color, downsample(pts)) for label, color, pts in drawable]
    return {
        "t0": t0, "t1": t1, "vmin": vmin, "vmax": vmax,
        "ticks": ticks, "series": downsampled,
    }


def render_svg(geom, fmt_key):
    """Renders the SVG markup for a chart whose geometry was already computed
    by prep_chart. Native <title> tooltips keep working with JS disabled."""
    fmt = FORMATTERS[fmt_key]
    t0, t1, vmin, vmax = geom["t0"], geom["t1"], geom["vmin"], geom["vmax"]

    def x_of(ts):
        return PAD_L + (ts - t0) * (W - PAD_L - PAD_R) / (t1 - t0)

    def y_of(v):
        return PAD_T + (vmax - v) * (H - PAD_T - PAD_B) / (vmax - vmin)

    parts = [
        '<svg class="chart" viewBox="0 0 %d %d" role="img" preserveAspectRatio="xMidYMid meet">' % (W, H)
    ]
    for t in geom["ticks"]:
        y = y_of(t)
        parts.append(
            '<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (PAD_L, y, W - PAD_R, y)
        )
        parts.append(
            '<text class="ytick" x="%d" y="%.1f">%s</text>' % (PAD_L - 7, y + 3.8, esc(fmt(t)))
        )

    span = t1 - t0
    x_fmt = "%H:%M" if span <= 172800 else "%d %b"
    for i in range(4):
        ts = t0 + span * i / 3.0
        x = x_of(ts)
        anchor = "start" if i == 0 else ("end" if i == 3 else "middle")
        parts.append(
            '<text class="xtick" x="%.1f" y="%d" text-anchor="%s">%s</text>'
            % (x, H - 7, anchor, esc(time.strftime(x_fmt, time.localtime(ts))))
        )

    for label, color, pts in geom["series"]:
        coords = " ".join("%.1f,%.1f" % (x_of(ts), y_of(v)) for ts, v in pts)
        if len(pts) == 1:
            parts.append(
                '<circle cx="%.1f" cy="%.1f" r="4" fill="%s"/>'
                % (x_of(pts[0][0]), y_of(pts[0][1]), color)
            )
        else:
            parts.append(
                '<polyline class="line" points="%s" stroke="%s"/>' % (coords, color)
            )
        last_ts, last_v = pts[-1]
        parts.append(
            '<circle cx="%.1f" cy="%.1f" r="3" fill="%s" stroke="var(--surface-2)" stroke-width="2"/>'
            % (x_of(last_ts), y_of(last_v), color)
        )
        # Native tooltips carry the exact reading without any script.
        for ts, v in pts[:: max(1, len(pts) // 40)]:
            parts.append(
                '<circle cx="%.1f" cy="%.1f" r="7" fill="transparent"><title>%s  %s: %s</title></circle>'
                % (
                    x_of(ts), y_of(v),
                    esc(time.strftime("%d %b %H:%M", time.localtime(ts))),
                    esc(label), esc(fmt(v)),
                )
            )
    parts.append(
        '<line class="axis" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
        % (PAD_L, y_of(vmin), W - PAD_R, y_of(vmin))
    )
    parts.append("</svg>")
    return "".join(parts)


def empty_svg():
    return (
        '<svg class="chart" viewBox="0 0 %d %d" role="img" aria-label="no data yet">'
        '<text x="%d" y="%d" class="empty">No samples collected yet</text></svg>'
        % (W, H, W // 2, H // 2)
    )


def chart_json(geom, fmt_key):
    """A small payload alongside the SVG so the crosshair script can read the
    exact same coordinates and values that were rendered, without
    reimplementing the y-axis value formatters in JS from scratch."""
    if geom is None:
        return "null"
    return json.dumps({
        "t0": geom["t0"], "t1": geom["t1"],
        "vmin": geom["vmin"], "vmax": geom["vmax"],
        "fmt": fmt_key,
        "series": [
            {"label": label, "color": color, "points": pts}
            for label, color, pts in geom["series"]
        ],
    })


def svg_chart(series, fmt_key, zero_based=True):
    """Renders one time-series chart as standalone inline SVG.

    `series` is a list of (label, color, points) where points are (ts, value).
    """
    geom = prep_chart(series, zero_based)
    if geom is None:
        return empty_svg()
    return render_svg(geom, fmt_key)


def card(title, subtitle, series, fmt_key, zero_based=True):
    fmt = FORMATTERS[fmt_key]
    geom = prep_chart(series, zero_based)
    svg = render_svg(geom, fmt_key) if geom else empty_svg()
    legend = ""
    if len(series) > 1:
        legend = '<div class="legend">' + "".join(
            '<span class="key"><i style="background:%s"></i>%s%s</span>'
            % (color, esc(label), (" &middot; " + esc(fmt(pts[-1][1]))) if pts else "")
            for label, color, pts in series
        ) + "</div>"
    else:
        label, color, pts = series[0]
        if pts:
            subtitle = fmt(pts[-1][1]) + " &middot; " + subtitle
    data_script = (
        '<script type="application/json" class="chart-data">%s</script>'
        % chart_json(geom, fmt_key)
    )
    return (
        '<section class="card" data-title="%s"><h2>%s</h2><p class="sub">%s</p>%s%s%s</section>'
        % (esc(title), esc(title), subtitle, legend, svg, data_script)
    )


def tile(label, value, note):
    return (
        '<div class="tile"><span class="tile-label">%s</span>'
        '<span class="tile-value">%s</span><span class="tile-note">%s</span></div>'
        % (esc(label), esc(value), esc(note))
    )


SERIES_1 = "#3987e5"
SERIES_2 = "#d95926"
SERIES_3 = "#199e70"
SERIES_4 = "#c98500"
SERIES_5 = "#8a63d1"

CSS = """
:root {
  --surface-0: #0f172a; --surface-1: #16203a; --surface-2: #1e293b;
  --border: #2b3950;
  --text-primary: #e8eef8; --text-secondary: #a7b4cb; --text-muted: #75849e;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 14px;
  background: var(--surface-0); color: var(--text-primary);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  color-scheme: dark;
}
header { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline;
         justify-content: space-between; margin-bottom: 12px; }
h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: .01em; }
.ranges { display: flex; gap: 6px; }
.ranges a {
  color: var(--text-secondary); text-decoration: none; font-size: 12px;
  padding: 3px 9px; border: 1px solid var(--border); border-radius: 999px;
}
.ranges a.on { color: var(--text-primary); background: var(--surface-2); border-color: #3c4c68; }
.tiles { display: grid; gap: 8px; margin-bottom: 12px;
         grid-template-columns: repeat(auto-fit, minmax(124px, 1fr)); }
.tile { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 8px; padding: 9px 11px; display: flex; flex-direction: column; gap: 1px; }
.tile-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase;
              letter-spacing: .06em; }
.tile-value { font-size: 19px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile-note { font-size: 11px; color: var(--text-secondary); }
.group { margin-bottom: 14px; }
.group-title { font-size: 11px; font-weight: 600; color: var(--text-muted);
                text-transform: uppercase; letter-spacing: .08em;
                margin: 0 0 8px; padding-bottom: 5px; border-bottom: 1px solid var(--border); }
.grid-cards { display: grid; gap: 10px;
              grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); }
/* Grid and flex items default to min-width:auto, which floors them at the
   SVG's intrinsic width and pushes the page into horizontal overflow inside a
   narrow iframe. */
.tile, .card { min-width: 0; }
.card { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 8px; padding: 11px 12px 6px; }
.card h2 { font-size: 13px; font-weight: 600; margin: 0; }
.sub { font-size: 11.5px; color: var(--text-secondary); margin: 2px 0 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 4px; font-size: 11.5px;
          color: var(--text-secondary); }
.key { display: inline-flex; align-items: center; gap: 5px; }
.key i { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
svg.chart { display: block; width: 100%; min-width: 0; height: auto; }
.grid { stroke: #26324a; stroke-width: 1; }
.axis { stroke: #3a4a66; stroke-width: 1; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.ytick { fill: var(--text-muted); font-size: 10.5px; text-anchor: end; }
.xtick { fill: var(--text-muted); font-size: 10.5px; }
.empty { fill: var(--text-muted); font-size: 12px; text-anchor: middle; }
details { margin-top: 12px; }
summary { cursor: pointer; font-size: 12px; color: var(--text-secondary); }
table { border-collapse: collapse; margin-top: 8px; font-size: 12px; width: 100%; }
th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid var(--border); }
th { color: var(--text-muted); font-weight: 500; }
td.num { font-variant-numeric: tabular-nums; }
footer { margin-top: 12px; font-size: 11px; color: var(--text-muted); }
.bad { color: #e66767; }
.card { position: relative; cursor: zoom-in; }
.card:focus-visible { outline: 2px solid #3987e5; outline-offset: 2px; }
.crosshair-line { stroke: var(--text-secondary); stroke-width: 1; pointer-events: none; }
.crosshair-dot { pointer-events: none; }
.crosshair-rect { fill: var(--surface-2); stroke: var(--border); pointer-events: none; }
.crosshair-text { fill: var(--text-primary); font-size: 9.5px; pointer-events: none; }
.chart-overlay { position: fixed; inset: 0; background: rgba(4, 8, 16, .82);
                  z-index: 1000; display: none; align-items: center; justify-content: center;
                  padding: clamp(14px, 4vw, 40px); }
.chart-overlay.open { display: flex; }
.chart-overlay-panel { position: relative; width: 100%; max-width: 900px;
                        background: var(--surface-1); border: 1px solid var(--border);
                        border-radius: 10px; padding: 16px 18px 10px; cursor: default; }
.chart-overlay-panel svg.chart { height: min(60vh, 520px); width: 100%; }
.chart-overlay-close { position: absolute; top: 10px; right: 10px; width: 30px; height: 30px;
                        background: var(--surface-2); color: var(--text-primary);
                        border: 1px solid var(--border); border-radius: 6px; font-size: 15px;
                        line-height: 1; cursor: pointer; }
.chart-overlay-close:hover { background: var(--surface-1); }
"""


SCRIPT_JS = """
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var W = 412, H = 172, PAD_L = 64, PAD_R = 10, PAD_T = 12, PAD_B = 24;

  function fmtBytes(v) {
    v = Number(v);
    var units = ["B", "KB", "MB", "GB", "TB", "PB"];
    for (var i = 0; i < units.length; i++) {
      var unit = units[i];
      if (Math.abs(v) < 1000 || unit === "PB") {
        return (unit === "B" ? v.toFixed(0) : v.toFixed(1)) + " " + unit;
      }
      v = v / 1000.0;
    }
    return String(v);
  }
  function fmtBps(v) { return fmtBytes(v) + "/s"; }
  function fmtPct(v) { return Number(v).toFixed(1) + "%"; }
  function fmtCount(v) { return String(Math.round(Number(v))); }
  var FORMATTERS = { bytes: fmtBytes, bps: fmtBps, pct: fmtPct, count: fmtCount };

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function fmtTs(ts) {
    var d = new Date(ts * 1000);
    return pad2(d.getDate()) + " " + MONTHS[d.getMonth()] + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function xOf(geom, ts) {
    return PAD_L + (ts - geom.t0) * (W - PAD_L - PAD_R) / (geom.t1 - geom.t0);
  }
  function yOf(geom, v) {
    return PAD_T + (geom.vmax - v) * (H - PAD_T - PAD_B) / (geom.vmax - geom.vmin);
  }
  function nearestIndex(points, ts) {
    var best = 0, bestDist = Infinity;
    for (var i = 0; i < points.length; i++) {
      var d = Math.abs(points[i][0] - ts);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    return best;
  }

  function attachCrosshair(svg, geom) {
    if (!geom || !geom.series || !geom.series.length) return;
    var fmt = FORMATTERS[geom.fmt] || fmtCount;
    var lineCount = geom.series.length + 1;

    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "crosshair");
    g.style.display = "none";

    var line = document.createElementNS(NS, "line");
    line.setAttribute("class", "crosshair-line");
    line.setAttribute("y1", String(PAD_T));
    line.setAttribute("y2", String(H - PAD_B));
    g.appendChild(line);

    var dots = [];
    for (var i = 0; i < geom.series.length; i++) {
      var dot = document.createElementNS(NS, "circle");
      dot.setAttribute("class", "crosshair-dot");
      dot.setAttribute("r", "4");
      dot.setAttribute("fill", geom.series[i].color);
      g.appendChild(dot);
      dots.push(dot);
    }

    var rect = document.createElementNS(NS, "rect");
    rect.setAttribute("class", "crosshair-rect");
    rect.setAttribute("rx", "3");
    g.appendChild(rect);

    var lines = [];
    for (var j = 0; j < lineCount; j++) {
      var t = document.createElementNS(NS, "text");
      t.setAttribute("class", "crosshair-text");
      g.appendChild(t);
      lines.push(t);
    }
    svg.appendChild(g);

    function move(clientX) {
      var box = svg.getBoundingClientRect();
      if (box.width === 0 || !geom.series[0].points.length) { g.style.display = "none"; return; }
      var relX = (clientX - box.left) / box.width * W;
      if (relX < PAD_L || relX > W - PAD_R) { g.style.display = "none"; return; }
      var ts = geom.t0 + (relX - PAD_L) * (geom.t1 - geom.t0) / (W - PAD_L - PAD_R);
      var idx = nearestIndex(geom.series[0].points, ts);
      var snapTs = geom.series[0].points[idx][0];
      var x = xOf(geom, snapTs);
      line.setAttribute("x1", String(x));
      line.setAttribute("x2", String(x));

      var maxLen = 0;
      var tsText = fmtTs(snapTs);
      lines[0].textContent = tsText;
      maxLen = tsText.length;
      for (var k = 0; k < geom.series.length; k++) {
        var s = geom.series[k];
        var pi = nearestIndex(s.points, snapTs);
        var pt = s.points[pi];
        if (pt) {
          dots[k].style.display = "";
          dots[k].setAttribute("cx", String(x));
          dots[k].setAttribute("cy", String(yOf(geom, pt[1])));
        } else {
          dots[k].style.display = "none";
        }
        var text = s.label + ": " + (pt ? fmt(pt[1]) : "-");
        lines[k + 1].textContent = text;
        if (text.length > maxLen) maxLen = text.length;
      }

      var boxW = Math.max(72, maxLen * 5.2 + 12);
      var boxH = lineCount * 13 + 6;
      var boxX = x + 8;
      if (boxX + boxW > W - 2) boxX = x - 8 - boxW;
      if (boxX < 2) boxX = 2;
      var boxY = PAD_T;
      rect.setAttribute("x", String(boxX));
      rect.setAttribute("y", String(boxY));
      rect.setAttribute("width", String(boxW));
      rect.setAttribute("height", String(boxH));
      for (var m = 0; m < lines.length; m++) {
        lines[m].setAttribute("x", String(boxX + 6));
        lines[m].setAttribute("y", String(boxY + 12 + m * 13));
      }
      g.style.display = "";
    }

    svg.addEventListener("pointermove", function (e) { move(e.clientX); });
    svg.addEventListener("pointerleave", function () { g.style.display = "none"; });
  }

  function readGeom(card) {
    var el = card.querySelector("script.chart-data");
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  var overlay = null, overlayPanel = null, overlayTitle = null, lastFocused = null;

  function buildOverlay() {
    overlay = document.createElement("div");
    overlay.className = "chart-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    overlayPanel = document.createElement("div");
    overlayPanel.className = "chart-overlay-panel";

    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "chart-overlay-close";
    closeBtn.setAttribute("aria-label", "Close expanded chart");
    closeBtn.textContent = "\\u2715";
    closeBtn.addEventListener("click", closeExpanded);

    overlayPanel.appendChild(closeBtn);
    overlay.appendChild(overlayPanel);
    document.body.appendChild(overlay);

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closeExpanded();
    });
  }

  function closeExpanded() {
    if (!overlay || !overlay.classList.contains("open")) return;
    overlay.classList.remove("open");
    while (overlayPanel.lastChild && overlayPanel.lastChild !== overlayPanel.firstChild) {
      overlayPanel.removeChild(overlayPanel.lastChild);
    }
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  function expandCard(card) {
    if (!overlay) buildOverlay();
    lastFocused = card;

    var clone = card.cloneNode(true);
    clone.removeAttribute("tabindex");
    clone.removeAttribute("role");
    clone.removeAttribute("aria-label");
    clone.style.cursor = "default";

    overlayPanel.appendChild(clone);
    overlay.classList.add("open");

    var svg = clone.querySelector("svg.chart");
    var geom = readGeom(clone);
    if (svg && geom) attachCrosshair(svg, geom);

    var closeBtn = overlayPanel.querySelector(".chart-overlay-close");
    if (closeBtn) closeBtn.focus();
  }

  function initCard(card) {
    var svg = card.querySelector("svg.chart");
    var geom = readGeom(card);
    if (svg && geom) attachCrosshair(svg, geom);

    card.setAttribute("tabindex", "0");
    card.setAttribute("role", "button");
    var title = card.getAttribute("data-title") || "chart";
    card.setAttribute("aria-label", "Expand chart: " + title);

    card.addEventListener("click", function () { expandCard(card); });
    card.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        expandCard(card);
      }
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeExpanded();
  });

  function init() {
    var cards = document.querySelectorAll(".card");
    for (var i = 0; i < cards.length; i++) initCard(cards[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
"""


def render_page(range_key):
    window = RANGE_SECONDS[range_key]
    since = int(time.time()) - window
    conn = db_connect()
    try:
        disk_used = series_for(conn, "vps_disk.used_bytes", since)
        disk_total = series_for(conn, "vps_disk.total_bytes", since)
        box_used = series_for(conn, "hetzner_box.used_bytes", since)
        box_total = series_for(conn, "hetzner_box.total_bytes", since)
        dl = series_for(conn, "qbt.dl_speed_bps", since)
        ul = series_for(conn, "qbt.ul_speed_bps", since)
        missing = series_for(conn, "lidarr.wanted_missing", since)
        queue = series_for(conn, "lidarr.queue_count", since)
        lib = series_for(conn, "lidarr.size_on_disk_bytes", since)
        active = series_for(conn, "qbt.torrents.downloading", since)
        queued = series_for(conn, "qbt.torrents.queuedDL", since)
        mem_total = series_for(conn, "vps_mem.total_bytes", since)
        mem_used = series_for(conn, "vps_mem.used_bytes", since)
        swap_total = series_for(conn, "vps_swap.total_bytes", since)
        swap_used = series_for(conn, "vps_swap.used_bytes", since)
        slskd_connected = series_for(conn, "slskd.connected", since)
        slskd_dl_active = series_for(conn, "slskd.downloads.active", since)
        slskd_dl_queued = series_for(conn, "slskd.downloads.queued", since)
        slskd_dl_speed = series_for(conn, "slskd.downloads.speed_bps", since)
        slskd_success_rate = series_for(conn, "slskd.downloads.success_rate", since)
        slskd_succeeded = series_for(conn, "slskd.downloads.succeeded", since)
        slskd_errored = series_for(conn, "slskd.downloads.errored", since)
        cpu_pct = series_for(conn, "vps_cpu.percent", since)
        cpu_load1 = series_for(conn, "vps_cpu.load1", since)
        radarr_size = series_for(conn, "radarr.size_on_disk_bytes", since)
        sonarr_size = series_for(conn, "sonarr.size_on_disk_bytes", since)
        downloads_bytes = series_for(conn, "qbt.downloads_bytes", since)
        seeding_bytes = series_for(conn, "qbt.seeding_bytes", since)
        in_progress_bytes = series_for(conn, "qbt.in_progress_bytes", since)

        tiles = []
        row = latest(conn, "vps_disk.used_bytes")
        tot = latest(conn, "vps_disk.total_bytes")
        if row and tot and tot[1]:
            tiles.append(tile("VPS disk", fmt_pct(row[1] * 100.0 / tot[1]),
                              fmt_bytes(row[1]) + " of " + fmt_bytes(tot[1])))
        row = latest(conn, "hetzner_box.used_bytes")
        tot = latest(conn, "hetzner_box.total_bytes")
        if row and tot and tot[1]:
            tiles.append(tile("Storage Box", fmt_pct(row[1] * 100.0 / tot[1]),
                              fmt_bytes(row[1]) + " of " + fmt_bytes(tot[1])))
        row = latest(conn, "qbt.dl_speed_bps")
        if row:
            tiles.append(tile("Download", fmt_bps(row[1]), "qBittorrent now"))
        row = latest(conn, "qbt.ul_speed_bps")
        if row:
            tiles.append(tile("Upload", fmt_bps(row[1]), "qBittorrent now"))
        row = latest(conn, "lidarr.wanted_missing")
        if row:
            tiles.append(tile("Wanted", fmt_count(row[1]), "Lidarr missing albums"))
        row = latest(conn, "lidarr.size_on_disk_bytes")
        if row:
            tiles.append(tile("Music library", fmt_bytes(row[1]), "Lidarr on disk"))
        row = latest(conn, "slskd.downloads.speed_bps")
        if row:
            tiles.append(tile("Soulseek DL", fmt_bps(row[1]), "slskd now"))
        row = latest(conn, "slskd.downloads.success_rate")
        if row:
            tiles.append(tile("Soulseek success", fmt_pct(row[1]), "of completed downloads"))

        groups = [
            ("Host", [
                card("VPS CPU used", "host-wide utilisation",
                     [("Used", SERIES_1, cpu_pct)], "pct"),
                card("VPS load average", "1-minute load average",
                     [("Load", SERIES_2, cpu_load1)], "count", zero_based=False),
                card("VPS memory used", "host-wide; an OOM kill is preceded by this pinning high",
                     [("Used", SERIES_2, ratio_series(mem_used, mem_total))], "pct"),
                card("VPS swap used", "headroom before the kernel starts killing processes",
                     [("Used", SERIES_4, swap_used), ("Total", SERIES_3, swap_total)], "bytes"),
                card("VPS disk used", "partition holding qBittorrent's incomplete path",
                     [("Used", SERIES_3, ratio_series(disk_used, disk_total))], "pct"),
            ]),
            ("Storage", [
                card("Hetzner Storage Box used", "remote media library",
                     [("Used", SERIES_4, ratio_series(box_used, box_total))], "pct"),
                card("Library sizes", "size on disk by *arr service",
                     [("Movies", SERIES_1, radarr_size), ("TV", SERIES_2, sonarr_size),
                      ("Music", SERIES_3, lib)], "bytes", zero_based=False),
                card("Storage composition", "libraries plus downloads against the box total",
                     [("Movies", SERIES_1, radarr_size), ("TV", SERIES_2, sonarr_size),
                      ("Music", SERIES_3, lib), ("Downloads", SERIES_5, downloads_bytes),
                      ("Box used", SERIES_4, box_used)], "bytes", zero_based=False),
            ]),
            ("Torrents", [
                card("qBittorrent transfer rate", "global session speed",
                     [("Download", SERIES_1, dl), ("Upload", SERIES_2, ul)], "bps"),
                card("Torrents by state", "counts from torrents/info",
                     [("Downloading", SERIES_1, active), ("Queued", SERIES_2, queued)], "count"),
                card("Torrent footprint", "disk used by finished vs in-progress torrents",
                     [("Seeding", SERIES_3, seeding_bytes), ("Downloading", SERIES_2, in_progress_bytes)],
                     "bytes", zero_based=False),
            ]),
            ("Soulseek", [
                card("Soulseek transfer rate", "in-progress download speed",
                     [("Download", SERIES_1, slskd_dl_speed)], "bps"),
                card("Soulseek downloads by state", "active and queued files",
                     [("Active", SERIES_1, slskd_dl_active), ("Queued", SERIES_2, slskd_dl_queued)], "count"),
                card("Soulseek success rate", "succeeded &divide; all completed downloads",
                     [("Success rate", SERIES_3, slskd_success_rate)], "pct", zero_based=False),
                card("Soulseek outcomes (cumulative)", "running totals since slskd started",
                     [("Succeeded", SERIES_3, slskd_succeeded), ("Errored", SERIES_2, slskd_errored)], "count"),
                card("Soulseek connection", "1 = logged in to the Soulseek network",
                     [("Connected", SERIES_4, slskd_connected)], "count"),
            ]),
            ("Library", [
                card("Lidarr backlog", "wanted-missing and queue",
                     [("Wanted missing", SERIES_1, missing), ("Queue", SERIES_2, queue)], "count"),
            ]),
        ]

        status = []
        for name, _ in COLLECTORS:
            row = latest(conn, "collect_ok." + name)
            if row is None:
                status.append((name, "no data", "", "bad"))
            else:
                status.append((
                    name,
                    "ok" if row[1] else "failing",
                    time.strftime("%d %b %H:%M", time.localtime(row[0])),
                    "" if row[1] else "bad",
                ))
        total_samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    finally:
        conn.close()

    ranges_html = "".join(
        '<a class="%s" href="?range=%s">%s</a>' % ("on" if k == range_key else "", k, k)
        for k, _ in RANGES
    )
    table_rows = "".join(
        '<tr><td>%s</td><td class="%s">%s</td><td class="num">%s</td></tr>'
        % (esc(n), cls, esc(s), esc(t))
        for n, s, t, cls in status
    )
    groups_html = "".join(
        '<div class="group"><p class="group-title">%s</p>'
        '<div class="grid-cards">%s</div></div>'
        % (esc(name), "".join(group_cards))
        for name, group_cards in groups
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>arrstack metrics</title><style>%s</style></head><body>"
        "<header><h1>arrstack metrics</h1><nav class=\"ranges\">%s</nav></header>"
        "<div class=\"tiles\">%s</div>%s"
        "<details><summary>Collector status</summary>"
        "<table><thead><tr><th>Source</th><th>State</th><th>Last sample</th></tr></thead>"
        "<tbody>%s</tbody></table></details>"
        "<footer>%s samples stored &middot; collected every %d min &middot; rendered %s</footer>"
        "<script>%s</script>"
        "</body></html>"
        % (CSS, ranges_html, "".join(tiles), groups_html, table_rows,
           total_samples, INTERVAL // 60,
           time.strftime("%d %b %H:%M", time.localtime()),
           SCRIPT_JS)
    )


SUMMARY_METRICS = (
    "old_collection.size_bytes",
    "old_collection.folders",
    "old_collection.files",
    "old_collection.scanned_at",
    "vps_cpu.percent",
    "vps_cpu.load1",
    "radarr.size_on_disk_bytes",
    "sonarr.size_on_disk_bytes",
    "qbt.downloads_bytes",
    "qbt.seeding_bytes",
    "qbt.in_progress_bytes",
    "vps_mem.used_bytes",
    "vps_mem.available_bytes",
    "vps_swap.used_bytes",
    "vps_swap.total_bytes",
    "slskd.connected",
    "slskd.downloads.active",
    "slskd.downloads.queued",
    "slskd.downloads.speed_bps",
    "slskd.downloads.succeeded",
    "slskd.downloads.errored",
    "slskd.downloads.cancelled",
    "slskd.downloads.rejected",
    "slskd.downloads.aborted",
    "slskd.downloads.success_rate",
    "slskd.uploads.active",
    "slskd.uploads.speed_bps",
    "slskd.uploads.total",
    "slskd.scanning",
    "slskd.shared.files",
    "slskd.shared.dirs",
)


def render_summary():
    conn = db_connect()
    try:
        out = {}
        for metric in SUMMARY_METRICS:
            row = latest(conn, metric)
            out[metric] = row[1] if row else None
        return out
    finally:
        conn.close()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return
        if parsed.path == "/summary.json":
            try:
                self._send(200, json.dumps(render_summary()), "application/json; charset=utf-8")
            except Exception:
                log("summary failed:\n" + traceback.format_exc())
                self._send(500, "{}", "application/json; charset=utf-8")
            return
        if parsed.path not in ("/", "/index.html"):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        wanted = urllib.parse.parse_qs(parsed.query).get("range", ["24h"])[0]
        if wanted not in RANGE_SECONDS:
            wanted = "24h"
        try:
            self._send(200, render_page(wanted), "text/html; charset=utf-8")
        except Exception:
            log("render failed:\n" + traceback.format_exc())
            self._send(500, "render failed", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    db_init()
    threading.Thread(target=collector_loop, daemon=True).start()
    log("serving on port " + str(PORT))
    Server(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
