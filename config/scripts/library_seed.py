#!/usr/bin/env python3
"""Repoint a completed torrent at the library files Sonarr or Radarr just imported.

Invoked as a Custom Script by the *arr applications on Import and Upgrade, and
by hand with --sweep or --backfill.

The invariant this module exists to protect: a torrent that has been repointed
is seeding the user's library. It must never be removed with its files, and its
share limits must be disabled before it is allowed to run.
"""

import json
import os
import posixpath
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".mpg", ".mpeg"}
TAG = "library-seed"
NO_LIMIT = -1
THROTTLE_BPS = 1  # during verification, so a failed recheck cannot write to the library
USER_AGENT = "arrstack-library-seed/1.0"


# --- pure logic -------------------------------------------------------------

def video_files(entries):
    """Video entries from a qBittorrent /torrents/files listing."""
    out = []
    for e in entries:
        if posixpath.splitext(e["name"])[1].lower() in VIDEO_EXT:
            out.append(e)
    return out


def strip_root(name, root):
    """Drop a torrent's root folder from an internal path.

    Single-file torrents have no root, so the name is returned unchanged.
    """
    if root and name.startswith(root + "/"):
        return name[len(root) + 1:]
    return name


def torrent_root(entries):
    """The common root folder of a multi-file torrent, or None if single-file."""
    if len(entries) < 2:
        return None
    firsts = {e["name"].split("/", 1)[0] for e in entries if "/" in e["name"]}
    if len(firsts) == 1 and all("/" in e["name"] for e in entries):
        return firsts.pop()
    return None


def missing_videos(entries, target_dir, stat=os.stat):
    """Video files from the torrent that are absent or the wrong size in target_dir.

    Returns a list of (relative_path, expected_size, actual_size_or_None).
    Empty list means every video file is present and correctly sized, which is
    the only condition under which a repoint may proceed.
    """
    root = torrent_root(entries)
    problems = []
    for e in video_files(entries):
        rel = strip_root(e["name"], root)
        path = os.path.join(target_dir, rel)
        try:
            actual = stat(path).st_size
        except (OSError, ValueError):
            problems.append((rel, e["size"], None))
            continue
        if actual != e["size"]:
            problems.append((rel, e["size"], actual))
    return problems


def extras_bytes(entries):
    """Total size of everything that is not a video file."""
    vids = {id(e) for e in video_files(entries)}
    return sum(e["size"] for e in entries if id(e) not in vids)


def split_target(target_dir):
    """A target directory into (parent_savepath, leaf_name).

    qBittorrent is given the parent as its save path; the torrent's root folder
    is then renamed to the leaf so the two agree.
    """
    target_dir = target_dir.rstrip("/")
    return posixpath.dirname(target_dir), posixpath.basename(target_dir)


# --- qBittorrent client -----------------------------------------------------

class QbtError(RuntimeError):
    pass


class Qbt:
    def __init__(self, base, username, password, opener=None):
        self.base = base.rstrip("/") + "/api/v2"
        self.origin = base.rstrip("/")
        self._op = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self._op.addheaders = [("Referer", self.origin), ("User-Agent", USER_AGENT)]
        self._login(username, password)

    def _login(self, u, p):
        body = urllib.parse.urlencode({"username": u, "password": p}).encode()
        r = self._op.open(self.base + "/auth/login", body, timeout=20)
        if r.status not in (200, 204):
            raise QbtError("login failed: %s" % r.status)

    def _post(self, path, fields):
        body = urllib.parse.urlencode(fields).encode()
        return self._op.open(self.base + path, body, timeout=60).read()

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._op.open(url, timeout=60).read()

    def torrent(self, h):
        rows = json.loads(self._get("/torrents/info", {"hashes": h.lower()}))
        return rows[0] if rows else None

    def by_tag(self, tag):
        return json.loads(self._get("/torrents/info", {"tag": tag}))

    def files(self, h):
        return json.loads(self._get("/torrents/files", {"hash": h.lower()}))

    def export(self, h):
        return self._get("/torrents/export", {"hash": h.lower()})

    def remove_keep_files(self, h):
        """The only removal this module performs. deleteFiles is always false."""
        self._post("/torrents/delete", {"hashes": h.lower(), "deleteFiles": "false"})

    def add(self, blob, savepath, category="", tags="", paused=True, skip_checking=True,
            use_download_path=False):
        """Add a torrent.

        skip_checking defaults to True because temp_path_enabled routes an
        *incomplete* torrent to the temp directory rather than its save path.
        A freshly added torrent is 0% complete, so without this it looks for
        its data in /config/incomplete and never sees the library. Marking it
        complete on add makes the save path apply; the recheck that follows is
        what actually verifies the data.

        use_download_path is false because temp_path_enabled relocates any
        torrent that drops below 100% into the temp directory. For a repointed
        torrent that directory move takes the *library* with it -- which is
        exactly what happened to La Jetee on 2026-08-24. Opting the torrent out
        of the download path removes the landmine rather than racing it.
        """
        boundary = "----arrstack%d" % int(time.time() * 1000)
        parts = []
        for k, v in (
            ("savepath", savepath),
            ("category", category),
            ("tags", tags),
            ("paused", "true" if paused else "false"),
            ("stopped", "true" if paused else "false"),
            ("skip_checking", "true" if skip_checking else "false"),
            ("useDownloadPath", "true" if use_download_path else "false"),
            ("autoTMM", "false"),
        ):
            parts.append(
                ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
            )
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="torrents"; filename="t.torrent"\r\n'
             "Content-Type: application/x-bittorrent\r\n\r\n" % boundary).encode()
        )
        parts.append(blob + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base + "/torrents/add",
            data=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=%s" % boundary,
                "Referer": self.origin,
                "User-Agent": USER_AGENT,
            },
        )
        return self._op.open(req, timeout=120).read()

    def rename_folder(self, h, old, new):
        self._post("/torrents/renameFolder", {"hash": h.lower(), "oldPath": old, "newPath": new})

    def disable_share_limits(self, h):
        """All three limits off, and the action set to Stop rather than Default.

        qBittorrent 5.2 requires shareLimitAction. Stop is chosen over Default
        deliberately: with the limits disabled the action should never fire, and
        if it somehow does, a repointed torrent must pause rather than delete
        the library it is seeding.
        """
        self._post(
            "/torrents/setShareLimits",
            {
                "hashes": h.lower(),
                "ratioLimit": NO_LIMIT,
                "seedingTimeLimit": NO_LIMIT,
                "inactiveSeedingTimeLimit": NO_LIMIT,
                "shareLimitAction": "Stop",
            },
        )

    def add_tags(self, h, tags):
        self._post("/torrents/addTags", {"hashes": h.lower(), "tags": tags})

    def set_download_limit(self, h, limit):
        self._post("/torrents/setDownloadLimit", {"hashes": h.lower(), "limit": int(limit)})

    def stop(self, h):
        try:
            self._post("/torrents/stop", {"hashes": h.lower()})
        except urllib.error.HTTPError:
            self._post("/torrents/pause", {"hashes": h.lower()})

    def recheck(self, h):
        self._post("/torrents/recheck", {"hashes": h.lower()})

    def start(self, h):
        try:
            self._post("/torrents/start", {"hashes": h.lower()})
        except urllib.error.HTTPError:
            self._post("/torrents/resume", {"hashes": h.lower()})


# --- repoint ----------------------------------------------------------------

DOWNLOADS_ROOT = os.environ.get("LIBRARY_SEED_DOWNLOADS_ROOT", "/downloads")


def safe_to_discard(path, target_dir, downloads_root=None):
    """Whether `path` is an old /downloads copy that may be deleted.

    Deliberately strict. The repoint has already succeeded by the time this is
    consulted, so a false negative leaves a stale copy -- recoverable. A false
    positive deletes the library.
    """
    root = os.path.realpath(downloads_root or DOWNLOADS_ROOT)
    p = os.path.realpath(path)
    t = os.path.realpath(target_dir)
    if p == root or not p.startswith(root + os.sep):
        return False
    if t == p or t.startswith(p + os.sep):
        return False
    return True


CHECKING_STATES = ("checkingUP", "checkingDL", "checkingResumeData", "queuedForChecking")
# States that are neither checking nor settled. qBittorrent passes through
# "moving" between a finished check and its final state; treating that as a
# finished check reports failure at 99.8% and tears down a good repoint.
TRANSIENT_STATES = ("moving", "allocating", "metaDL", "unknown")


def wait_complete(qbt, h, tolerance_bytes, timeout=1800, interval=5, sleep=time.sleep):
    """Block until the torrent has verified every byte it needs.

    The torrent is added with skip_checking, so amount_left reads 0 from the
    moment it appears -- before anything has been verified. Waiting on that
    alone would report success instantly and discard the only good copy. A
    checking state must therefore be observed to finish, not merely be absent.
    """
    deadline = time.time() + timeout
    seen_checking = False
    while time.time() < deadline:
        t = qbt.torrent(h)
        if t is None:
            return False, "torrent vanished during recheck"
        state = t["state"]
        if state in ("error", "missingFiles"):
            return False, "state=%s" % state
        if state in CHECKING_STATES:
            seen_checking = True
        elif state in TRANSIENT_STATES:
            pass
        elif seen_checking:
            if t.get("progress", 0) < 1.0:
                return False, "check finished at progress=%.4f" % t.get("progress", 0)
            if t.get("amount_left", 1) > tolerance_bytes:
                return False, "amount_left=%s over tolerance %s" % (t.get("amount_left"), tolerance_bytes)
            return True, "verified progress=1.0"
        sleep(interval)
    return False, "timeout after %ss (checking_seen=%s)" % (timeout, seen_checking)


def repoint(qbt, infohash, target_dir, log=print, discard=None):
    """Move a completed torrent onto the imported library files.

    Returns (changed: bool, reason: str).
    """
    t = qbt.torrent(infohash)
    if t is None:
        return False, "torrent not in qBittorrent"
    if TAG in (t.get("tags") or ""):
        return False, "already repointed"
    if t.get("progress", 0) < 1:
        return False, "torrent not complete"

    entries = qbt.files(infohash)
    gaps = missing_videos(entries, target_dir)
    if gaps:
        return False, "waiting for import: %d file(s) missing" % len(gaps)

    old_content = t.get("content_path") or ""
    old_save = t.get("save_path") or ""
    root = torrent_root(entries)
    savepath, leaf = split_target(target_dir)
    tolerance = extras_bytes(entries)

    log("repoint %s -> %s (root=%r leaf=%r tolerance=%dB)" % (infohash[:8], target_dir, root, leaf, tolerance))
    blob = qbt.export(infohash)
    qbt.remove_keep_files(infohash)

    try:
        qbt.add(blob, savepath, category=t.get("category", ""), tags=TAG, paused=True)
        for _ in range(30):
            if qbt.torrent(infohash):
                break
            time.sleep(1)
        else:
            raise QbtError("re-added torrent did not appear")

        if root and root != leaf:
            qbt.rename_folder(infohash, root, leaf)
        qbt.disable_share_limits(infohash)
        qbt.add_tags(infohash, TAG)

        # A stopped torrent does not process a recheck, so it has to run. But a
        # running torrent whose recheck fails would start downloading over the
        # library files. The throttle makes that harmless: nothing meaningful
        # can be written while the check decides.
        qbt.set_download_limit(infohash, THROTTLE_BPS)
        qbt.recheck(infohash)
        qbt.start(infohash)
        ok, why = wait_complete(qbt, infohash, tolerance)
        if not ok:
            qbt.stop(infohash)
            raise QbtError("recheck failed: %s" % why)
        qbt.set_download_limit(infohash, 0)
    except Exception as exc:
        log("FAILED (%s) -- rolling back to %s" % (exc, old_save))
        try:
            qbt.remove_keep_files(infohash)
        except Exception:
            pass
        qbt.add(blob, old_save, category=t.get("category", ""), tags="", paused=False,
                skip_checking=False)
        return False, "rolled back: %s" % exc

    if old_content and safe_to_discard(old_content, target_dir):
        (discard or _discard)(old_content, log)
    else:
        log("kept old copy (not safe to discard): %r" % old_content)
    return True, "repointed"


def _discard(path, log):
    import shutil

    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        log("discarded old copy: %s" % path)
    except OSError as exc:
        log("could not discard %s: %s" % (path, exc))


# --- entry point ------------------------------------------------------------

def event_from_env(env):
    """(infohash, target_dir, eventtype) from a Sonarr or Radarr script env."""
    if env.get("sonarr_eventtype"):
        return (
            (env.get("sonarr_download_id") or "").lower(),
            os.path.dirname(env.get("sonarr_episodefile_path") or ""),
            env["sonarr_eventtype"],
        )
    if env.get("radarr_eventtype"):
        return (
            (env.get("radarr_download_id") or "").lower(),
            os.path.dirname(env.get("radarr_moviefile_path") or ""),
            env["radarr_eventtype"],
        )
    return "", "", ""


def connect():
    return Qbt(
        os.environ.get("QBT_URL", "http://172.28.0.10:8080"),
        os.environ["QBT_USERNAME"],
        os.environ["QBT_PASSWORD"],
    )


def sweep(qbt, log=print):
    """Remove repointed torrents whose files an upgrade has replaced."""
    n = 0
    for t in qbt.by_tag(TAG):
        if t["state"] in ("missingFiles", "error"):
            log("sweep: removing %s (%s)" % (t["name"][:60], t["state"]))
            qbt.remove_keep_files(t["hash"])
            n += 1
    log("sweep: removed %d" % n)
    return n


def main(argv):
    if "--sweep" in argv:
        return 0 if sweep(connect()) >= 0 else 1

    infohash, target_dir, event = event_from_env(os.environ)
    if event in ("Test", "test"):
        print("library-seed: test event OK")
        return 0
    if not infohash or not target_dir:
        print("library-seed: no download id or path in environment; nothing to do")
        return 0

    qbt = connect()
    changed, reason = repoint(qbt, infohash, target_dir)
    print("library-seed: %s (%s)" % ("changed" if changed else "no change", reason))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
