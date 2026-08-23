"""Assembling the daily digest's data."""
import calendar
import html
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

from notify import USER_AGENT

DAY = 86400
HTTP_TIMEOUT = 30
LIST_LIMIT = 25

CAPACITY_METRICS = (
    ("box_used_bytes", "hetzner_box.used_bytes"),
    ("box_total_bytes", "hetzner_box.total_bytes"),
    ("seeding_bytes", "qbt.seeding_bytes"),
    ("vps_disk_used_bytes", "vps_disk.used_bytes"),
)

QBT_SEEDING_STATES = (
    "uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP",
    "forcedUP", "checkingUP",
)

BAZARR_TIMESTAMP_FORMAT = "%m/%d/%y %H:%M:%S"


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


def _delta(db_path, metric, window_start, now):
    before = series_at(db_path, metric, window_start)
    after = series_at(db_path, metric, now)
    if before is None or after is None:
        return None
    return after - before


def _diff_left(previous, current):
    """Items present in the previous snapshot and absent from the current
    one. None previous (no snapshot committed yet) reports nothing."""
    if previous is None:
        return []
    current_hashes = {item.get("hash") for item in current if item.get("hash")}
    return [
        {"name": item.get("name", "unknown"), "bytes": float(item.get("bytes") or 0)}
        for item in previous
        if item.get("hash") not in current_hashes
    ]


def gather(db_path, store, window_start, now, fetchers,
           requests_fetcher=None, contributed_fetcher=None, left_fetcher=None):
    """Everything the digest needs, over the window [window_start, now).

    `fetchers` maps a section name to a zero-argument callable returning a
    list of item names. `requests_fetcher` returns a dict of requester name
    to a list of item names. `contributed_fetcher` returns the mean seed
    ratio. `left_fetcher` returns the current torrent snapshot (list of
    {hash, name, bytes}). Each is wrapped: one application being unreachable
    costs its own section and nothing else.
    """
    arrived = {}
    for name, fetch in fetchers.items():
        try:
            arrived[name] = list(fetch())
        except Exception:
            arrived[name] = []

    capacity = {key: series_at(db_path, metric, now) for key, metric in CAPACITY_METRICS}
    capacity["box_delta_bytes"] = _delta(db_path, "hetzner_box.used_bytes", window_start, now)
    capacity["seeding_delta_bytes"] = _delta(db_path, "qbt.seeding_bytes", window_start, now)

    current_torrents = None
    if left_fetcher is not None:
        try:
            current_torrents = list(left_fetcher())
        except Exception:
            current_torrents = None
    # A failed live fetch must not be treated as "the torrent list is now
    # empty" — that would report every previously-seeded torrent as gone.
    left = [] if current_torrents is None else _diff_left(
        store.previous_torrent_snapshot(), current_torrents
    )

    contributed = {
        "uploads_delta": _delta(db_path, "slskd.uploads.total", window_start, now),
        "ul_bytes_delta": _delta(db_path, "qbt.ul_session_bytes", window_start, now),
        "seeding_bytes": series_at(db_path, "qbt.seeding_bytes", now),
        "mean_seed_ratio": None,
    }
    if contributed_fetcher is not None:
        try:
            contributed["mean_seed_ratio"] = contributed_fetcher()
        except Exception:
            contributed["mean_seed_ratio"] = None

    backlog = {
        "wanted_missing": series_at(db_path, "lidarr.wanted_missing", now),
        "wanted_missing_delta": _delta(db_path, "lidarr.wanted_missing", window_start, now),
        "queue_count": series_at(db_path, "lidarr.queue_count", now),
        "success_rate": series_at(db_path, "slskd.downloads.success_rate", now),
        "success_rate_delta": _delta(
            db_path, "slskd.downloads.success_rate", window_start, now
        ),
    }

    requests = {}
    if requests_fetcher is not None:
        try:
            requests = requests_fetcher()
        except Exception:
            requests = {}

    return {
        "now": now,
        "window_start": window_start,
        "arrived": arrived,
        "quiet": not any(arrived.values()),
        "requests": requests,
        "left": left,
        "contributed": contributed,
        "capacity": capacity,
        "backlog": backlog,
        "health": {"transitions": store.transitions_since(window_start),
                   "open": store.open_alerts()},
        # Consumed by the caller to commit a new torrent snapshot, and only
        # when the digest carrying this diff was actually sent.
        "_current_torrents": current_torrents,
    }


def _gb(value):
    return "—" if value is None else ("%.1f GB" % (value / 1e9))


def _signed_gb(value):
    if value is None:
        return "—"
    return "%s%.1f GB" % ("+" if value >= 0 else "", value / 1e9)


def _count(value):
    return "—" if value is None else "%d" % int(value)


def _signed_count(value):
    if value is None:
        return "—"
    return "%s%d" % ("+" if value >= 0 else "", int(value))


def _pct(value):
    return "—" if value is None else "%.1f%%" % value


def _signed_pct(value):
    if value is None:
        return "—"
    return "%s%.1f%%" % ("+" if value >= 0 else "", value)


def _capped(items, limit=LIST_LIMIT):
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _list_html(title, items, limit=LIST_LIMIT):
    if not items:
        return ""
    shown, extra = _capped(items, limit)
    entries = "".join("<li>%s</li>" % html.escape(str(item)) for item in shown)
    if extra:
        entries += "<li>… and %d more</li>" % extra
    return "<h2>%s</h2><ul>%s</ul>" % (html.escape(title), entries)


SECTION_TITLES = {"tv": "TV"}


def render(data):
    """Subject and HTML body. Always renders every data section; a quiet day
    changes the framing, not the content."""
    now = data["now"] or time.time()
    date = time.strftime("%d %b %Y", time.localtime(now))
    subject = "arrstack daily digest — " + date
    window_start = data.get("window_start")

    rows = []
    if data["quiet"]:
        rows.append("<p>Quiet day today, see you tomorrow!</p>")
    else:
        for section, items in sorted(data["arrived"].items()):
            title = SECTION_TITLES.get(section, section.title())
            section_html = _list_html(title, items)
            if section_html:
                rows.append(section_html)

    requests = data.get("requests") or {}
    if requests:
        entries = []
        for requester in sorted(requests):
            items = requests[requester]
            if not items:
                continue
            entries.append(
                "<li><strong>%s</strong>: %s</li>"
                % (html.escape(requester), html.escape(", ".join(items)))
            )
        if entries:
            rows.append("<h2>Requested</h2><ul>%s</ul>" % "".join(entries))

    left = data.get("left") or []
    if left:
        shown, extra = _capped(left)
        entries = "".join(
            "<li>%s (%s)</li>" % (html.escape(item["name"]), _gb(item.get("bytes")))
            for item in shown
        )
        if extra:
            entries += "<li>… and %d more</li>" % extra
        rows.append("<h2>Left</h2><ul>%s</ul>" % entries)

    contributed = data.get("contributed") or {}
    ratio = contributed.get("mean_seed_ratio")
    rows.append(
        "<h2>Contributed</h2><ul>"
        "<li>Soulseek uploads: %s</li>"
        "<li>qBittorrent uploaded: %s</li>"
        "<li>Mean seed ratio: %s</li>"
        "</ul>"
        % (
            _signed_count(contributed.get("uploads_delta")),
            _signed_gb(contributed.get("ul_bytes_delta")),
            "—" if ratio is None else ("%.2f" % ratio),
        )
    )

    capacity = data["capacity"]
    rows.append(
        "<h2>Capacity</h2><ul>"
        "<li>Storage Box: %s of %s (%s)</li>"
        "<li>Seeding: %s (%s)</li>"
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

    backlog = data.get("backlog") or {}
    if backlog:
        rows.append(
            "<h2>Backlog</h2><ul>"
            "<li>Lidarr wanted: %s (%s)</li>"
            "<li>Lidarr queue: %s</li>"
            "<li>Soulseek success rate: %s (%s)</li>"
            "</ul>"
            % (
                _count(backlog.get("wanted_missing")),
                _signed_count(backlog.get("wanted_missing_delta")),
                _count(backlog.get("queue_count")),
                _pct(backlog.get("success_rate")),
                _signed_pct(backlog.get("success_rate_delta")),
            )
        )

    transitions = data["health"]["transitions"]
    if transitions:
        entries = "".join(
            "<li>%s: %s &rarr; %s</li>"
            % (html.escape(t["rule"]), t["from_state"], t["to_state"])
            for t in transitions
        )
        rows.append("<h2>Health</h2><ul>%s</ul>" % entries)
    else:
        rows.append("<h2>Health</h2><p>No alerts in this period.</p>")

    period = (
        "<p class=\"meta\">since %s</p>"
        % time.strftime("%d %b %H:%M", time.localtime(window_start))
        if window_start
        else ""
    )
    body = (
        "<html><body style=\"font-family:system-ui,sans-serif;max-width:640px\">"
        "<h1>arrstack — %s</h1>%s%s</body></html>"
        % (date, period, "".join(rows))
    )
    return subject, body


# ---------------------------------------------------------------------------
# Live application fetches. Each function below makes its own HTTP calls and
# is meant to be wrapped by its caller (see gather() above): one application
# being down must cost only its own section.


def _headers(extra=None):
    headers = {"User-Agent": USER_AGENT}
    if extra:
        headers.update(extra)
    return headers


def http_json(url, headers=None, opener=None, timeout=HTTP_TIMEOUT):
    request = urllib.request.Request(url, headers=_headers(headers))
    open_fn = opener.open if opener else urllib.request.urlopen
    with open_fn(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _since_iso(now, window_start):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start))


def fetch_lidarr_arrivals(url, api_key, window_start, now):
    """Albums that finished importing in the window, as 'Artist — Album (N tracks)'."""
    headers = {"X-Api-Key": api_key}
    since = _since_iso(now, window_start)
    events = http_json(
        url + "/api/v1/history/since?date=" + urllib.parse.quote(since)
        + "&eventType=trackFileImported",
        headers,
    )
    counts = {}
    for event in events:
        album_id = event.get("albumId")
        if album_id is None:
            continue
        counts[album_id] = counts.get(album_id, 0) + 1
    if not counts:
        return []
    qs = "&".join("albumIds=%d" % album_id for album_id in counts)
    albums = http_json(url + "/api/v1/album?" + qs, headers)
    names = {}
    for album in albums:
        artist = (album.get("artist") or {}).get("artistName", "Unknown artist")
        names[album["id"]] = "%s — %s" % (artist, album.get("title", "Unknown album"))
    return sorted(
        "%s (%d tracks)" % (names.get(album_id, "Unknown album"), count)
        for album_id, count in counts.items()
    )


def fetch_sonarr_arrivals(url, api_key, window_start, now):
    """Series that finished importing episodes, as 'Series (N episodes)'."""
    headers = {"X-Api-Key": api_key}
    since = _since_iso(now, window_start)
    events = http_json(
        url + "/api/v3/history/since?date=" + urllib.parse.quote(since)
        + "&eventType=downloadFolderImported",
        headers,
    )
    counts = {}
    for event in events:
        series_id = event.get("seriesId")
        if series_id is None:
            continue
        counts[series_id] = counts.get(series_id, 0) + 1
    if not counts:
        return []
    series_list = http_json(url + "/api/v3/series", headers)
    names = {s["id"]: s.get("title", "Unknown series") for s in series_list}
    return sorted(
        "%s (%d episodes)" % (names.get(series_id, "Unknown series"), count)
        for series_id, count in counts.items()
    )


def fetch_radarr_arrivals(url, api_key, window_start, now):
    """Movies that finished importing, by title."""
    headers = {"X-Api-Key": api_key}
    since = _since_iso(now, window_start)
    events = http_json(
        url + "/api/v3/history/since?date=" + urllib.parse.quote(since)
        + "&eventType=downloadFolderImported",
        headers,
    )
    movie_ids = {e["movieId"] for e in events if e.get("movieId") is not None}
    if not movie_ids:
        return []
    movies = http_json(url + "/api/v3/movie", headers)
    names = {m["id"]: m.get("title", "Unknown movie") for m in movies}
    return sorted(names.get(movie_id, "Unknown movie") for movie_id in movie_ids)


def _season_episode(value):
    try:
        season, episode = str(value).lower().split("x", 1)
        return int(season), int(episode)
    except ValueError:
        return 0, 0


def fetch_bazarr_arrivals(url, api_key, window_start, now, length=500):
    """Subtitles downloaded in the window, as 'Series S01E02 — Dutch'.

    Bazarr's history has no `since` parameter, so a page is fetched and
    filtered locally by its own parsed_timestamp field.
    """
    headers = {"X-API-KEY": api_key}
    payload = http_json(
        url + "/api/episodes/history?start=0&length=%d" % length, headers
    )
    rows = payload.get("data") or []
    results = []
    for row in rows:
        parsed = row.get("parsed_timestamp")
        if not parsed:
            continue
        try:
            ts = time.mktime(time.strptime(parsed, BAZARR_TIMESTAMP_FORMAT))
        except ValueError:
            continue
        if ts < window_start or ts > now:
            continue
        season, episode = _season_episode(row.get("episode_number", ""))
        language = (row.get("language") or {}).get("name", "Unknown language")
        results.append(
            "%s S%02dE%02d — %s"
            % (row.get("seriesTitle", "Unknown series"), season, episode, language)
        )
    return sorted(results)


def _parse_utc_iso(value):
    if not value:
        return None
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _resolve_request_title(url, api_key, media_type, tmdb_id):
    headers = {"X-Api-Key": api_key}
    path = (
        "/api/v1/tv/%s" % tmdb_id if media_type == "tv" else "/api/v1/movie/%s" % tmdb_id
    )
    payload = http_json(url + path, headers)
    if media_type == "tv":
        year = (payload.get("firstAirDate") or "")[:4]
        return "%s (%s)" % (payload.get("name", "Unknown"), year or "?")
    year = (payload.get("releaseDate") or "")[:4]
    return "%s (%s)" % (payload.get("title", "Unknown"), year or "?")


def fetch_requests(url, api_key, window_start, now, take=50):
    """Seerr requests made in the window, grouped by requester.

    One title lookup per distinct tmdbId, cached within the call; a failed
    lookup falls back to the raw id rather than losing the whole request.
    """
    headers = {"X-Api-Key": api_key}
    payload = http_json(url + "/api/v1/request?take=%d&skip=0&sort=added" % take, headers)
    cache = {}
    grouped = {}
    for item in payload.get("results", []):
        created = _parse_utc_iso(item.get("createdAt"))
        if created is None or created < window_start or created > now:
            continue
        media_type = item.get("type")
        tmdb_id = (item.get("media") or {}).get("tmdbId")
        if tmdb_id is None or media_type not in ("tv", "movie"):
            continue
        key = (media_type, tmdb_id)
        if key not in cache:
            try:
                cache[key] = _resolve_request_title(url, api_key, media_type, tmdb_id)
            except Exception:
                cache[key] = "tmdb #%s" % tmdb_id
        requester = (item.get("requestedBy") or {}).get("displayName") or "unknown"
        grouped.setdefault(requester, []).append(
            "%s — %s" % (cache[key], media_type)
        )
    return {requester: sorted(items) for requester, items in grouped.items()}


def _qbt_login(url, username, password):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        url + "/api/v2/auth/login", data=body,
        headers=_headers({"Referer": url}), method="POST",
    )
    with opener.open(request, timeout=HTTP_TIMEOUT) as response:
        response.read()
    if not len(jar):
        raise RuntimeError("qBittorrent login returned no session cookie")
    return opener


def _qbt_torrents(url, username, password):
    opener = _qbt_login(url, username, password)
    return http_json(url + "/api/v2/torrents/info", {"Referer": url}, opener=opener)


def fetch_mean_seed_ratio(url, username, password):
    torrents = _qbt_torrents(url, username, password)
    ratios = [
        float(t.get("ratio", 0)) for t in torrents if t.get("state") in QBT_SEEDING_STATES
    ]
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def fetch_torrent_snapshot(url, username, password):
    torrents = _qbt_torrents(url, username, password)
    return [
        {"hash": t.get("hash"), "name": t.get("name", "unknown"),
         "bytes": float(t.get("completed") or 0)}
        for t in torrents
        if t.get("hash")
    ]
