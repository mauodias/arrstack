import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config", "scripts"))
import library_seed as L  # noqa: E402

PACK = [
    {"name": "Pack.S06-GRP/RARBG.txt", "size": 30},
    {"name": "Pack.S06-GRP/Subs/e01/2_English.srt", "size": 70},
    {"name": "Pack.S06-GRP/e01.mkv", "size": 1000},
    {"name": "Pack.S06-GRP/e02.mkv", "size": 2000},
]
SINGLE = [{"name": "Silo.S03E01.mkv", "size": 500}]


def fake_stat(sizes):
    class S:
        def __init__(self, n):
            self.st_size = n

    def stat(path):
        if path not in sizes:
            raise FileNotFoundError(path)
        return S(sizes[path])

    return stat


def test_root_detection():
    assert L.torrent_root(PACK) == "Pack.S06-GRP"
    assert L.torrent_root(SINGLE) is None


def test_video_filter_and_extras():
    assert [e["name"] for e in L.video_files(PACK)] == [
        "Pack.S06-GRP/e01.mkv",
        "Pack.S06-GRP/e02.mkv",
    ]
    assert L.extras_bytes(PACK) == 100
    assert L.extras_bytes(SINGLE) == 0


def test_strip_root():
    assert L.strip_root("Pack.S06-GRP/e01.mkv", "Pack.S06-GRP") == "e01.mkv"
    assert L.strip_root("Silo.S03E01.mkv", None) == "Silo.S03E01.mkv"


def test_split_target():
    assert L.split_target("/tv/The Office (US)/Season 06") == (
        "/tv/The Office (US)",
        "Season 06",
    )
    assert L.split_target("/tv/Show/Season 01/") == ("/tv/Show", "Season 01")


def test_all_present_is_empty():
    st = fake_stat({"/tv/S/Season 06/e01.mkv": 1000, "/tv/S/Season 06/e02.mkv": 2000})
    assert L.missing_videos(PACK, "/tv/S/Season 06", stat=st) == []


def test_partial_import_reports_missing():
    st = fake_stat({"/tv/S/Season 06/e01.mkv": 1000})
    out = L.missing_videos(PACK, "/tv/S/Season 06", stat=st)
    assert out == [("e02.mkv", 2000, None)]


def test_size_mismatch_reported():
    st = fake_stat({"/tv/S/Season 06/e01.mkv": 1000, "/tv/S/Season 06/e02.mkv": 7})
    out = L.missing_videos(PACK, "/tv/S/Season 06", stat=st)
    assert out == [("e02.mkv", 2000, 7)]


def test_empty_target_reports_all():
    st = fake_stat({})
    assert len(L.missing_videos(PACK, "/tv/S/Season 06", stat=st)) == 2


def test_extras_are_never_required():
    """Subtitles and .txt are absent from the library and must not block."""
    st = fake_stat({"/tv/S/Season 06/e01.mkv": 1000, "/tv/S/Season 06/e02.mkv": 2000})
    assert L.missing_videos(PACK, "/tv/S/Season 06", stat=st) == []


def test_single_file_torrent_maps_flat():
    st = fake_stat({"/movies/Film (2019)/Silo.S03E01.mkv": 500})
    assert L.missing_videos(SINGLE, "/movies/Film (2019)", stat=st) == []


def test_source_never_contains_delete_files_true():
    """The invariant, enforced against the source itself.

    Every removal path must pass deleteFiles=false. A literal 'true' next to
    deleteFiles anywhere in the module is a bug that could delete the library.
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "config", "scripts", "library_seed.py")).read()
    lowered = src.lower().replace(" ", "")
    assert "deletefiles=true" not in lowered
    assert "deletefiles':true" not in lowered
    assert 'deletefiles":true' not in lowered


# --- procedure tests --------------------------------------------------------

class StubQbt:
    def __init__(self, torrent, entries, fail_recheck=False):
        self._t = dict(torrent)
        self._entries = entries
        self.calls = []
        self.fail_recheck = fail_recheck
        self.removed_with_files = False
        self.skip_checking_calls = []

    def torrent(self, h):
        return dict(self._t) if self._t else None

    def files(self, h):
        return self._entries

    def export(self, h):
        self.calls.append(("export", h))
        return b"d8:announce..e"

    def remove_keep_files(self, h):
        self.calls.append(("remove_keep_files", h))

    def add(self, blob, savepath, category="", tags="", paused=True, skip_checking=True):
        self.calls.append(("add", savepath, category, tags, paused))
        self.skip_checking_calls.append(skip_checking)
        self._t["tags"] = tags
        self._t["save_path"] = savepath

    def rename_folder(self, h, old, new):
        self.calls.append(("rename_folder", old, new))

    def disable_share_limits(self, h):
        self.calls.append(("disable_share_limits", h))

    def add_tags(self, h, tags):
        self.calls.append(("add_tags", tags))

    def recheck(self, h):
        self.calls.append(("recheck", h))
        self._t["amount_left"] = 999999 if self.fail_recheck else 100

    def start(self, h):
        self.calls.append(("start", h))

    def stop(self, h):
        self.calls.append(("stop", h))

    def set_download_limit(self, h, limit):
        self.calls.append(("set_download_limit", limit))


PACK_T = {
    "hash": "abc123", "progress": 1.0, "tags": "", "category": "tv-sonarr",
    "content_path": "/downloads/Pack.S06-GRP", "save_path": "/downloads",
    "state": "stalledUP", "amount_left": 0,
}


def _names(calls):
    return [c[0] for c in calls]


def test_repoint_happy_path_sequence():
    q = StubQbt(PACK_T, PACK)
    L.missing_videos = lambda *a, **k: []
    changed, reason = L.repoint(q, "abc123", "/tv/S/Season 06",
                                log=lambda *a: None, discard=lambda p, log: None)
    assert changed, reason
    seq = _names(q.calls)
    assert seq.index("remove_keep_files") < seq.index("add")
    assert seq.index("disable_share_limits") < seq.index("start")
    assert ("rename_folder", "Pack.S06-GRP", "Season 06") in q.calls
    assert ("add", "/tv/S", "tv-sonarr", "library-seed", True) in q.calls


def test_repoint_skips_when_already_tagged():
    t = dict(PACK_T, tags="library-seed")
    q = StubQbt(t, PACK)
    changed, reason = L.repoint(q, "abc123", "/tv/S/Season 06", log=lambda *a: None)
    assert not changed and reason == "already repointed"
    assert q.calls == []


def test_repoint_rolls_back_on_recheck_failure():
    q = StubQbt(PACK_T, PACK, fail_recheck=True)
    L.missing_videos = lambda *a, **k: []
    L.wait_complete = lambda *a, **k: (False, "state=missingFiles")
    changed, reason = L.repoint(q, "abc123", "/tv/S/Season 06",
                                log=lambda *a: None, discard=lambda p, log: None)
    assert not changed
    assert "rolled back" in reason
    assert ("add", "/downloads", "tv-sonarr", "", False) in q.calls


def test_safe_to_discard_refuses_library_paths():
    assert L.safe_to_discard("/downloads/Pack", "/tv/S/Season 06", "/downloads")
    assert not L.safe_to_discard("/tv/S/Season 06", "/tv/S/Season 06", "/downloads")
    assert not L.safe_to_discard("/downloads", "/tv/S/Season 06", "/downloads")
    assert not L.safe_to_discard("/tv/S", "/tv/S/Season 06", "/downloads")


def test_event_from_env_both_apps():
    s = {"sonarr_eventtype": "Download", "sonarr_download_id": "ABC",
         "sonarr_episodefile_path": "/tv/S/Season 06/e01.mkv"}
    assert L.event_from_env(s) == ("abc", "/tv/S/Season 06", "Download")
    r = {"radarr_eventtype": "Download", "radarr_download_id": "DEF",
         "radarr_moviefile_path": "/movies/F (2019)/f.mkv"}
    assert L.event_from_env(r) == ("def", "/movies/F (2019)", "Download")
    assert L.event_from_env({}) == ("", "", "")


def test_disable_share_limits_sends_stop_action():
    """qBittorrent 5.2 requires shareLimitAction; it must never be a removing one."""
    sent = {}

    class Cap(L.Qbt):
        def __init__(self):
            pass

        def _post(self, path, fields):
            sent["path"] = path
            sent["fields"] = fields

    Cap().disable_share_limits("abc")
    assert sent["path"] == "/torrents/setShareLimits"
    f = sent["fields"]
    assert f["ratioLimit"] == f["seedingTimeLimit"] == f["inactiveSeedingTimeLimit"] == -1
    assert f["shareLimitAction"] == "Stop"
    assert "Remove" not in str(f["shareLimitAction"])


def test_repoint_throttles_before_start_and_clears_after():
    """A failed recheck must not be able to write over the library."""
    q = StubQbt(PACK_T, PACK)
    L.missing_videos = lambda *a, **k: []
    L.wait_complete = lambda *a, **k: (True, "ok")
    ok, _ = L.repoint(q, "abc123", "/tv/S/Season 06",
                      log=lambda *a: None, discard=lambda p, log: None)
    assert ok
    seq = _names(q.calls)
    assert seq.index("set_download_limit") < seq.index("start")
    assert ("set_download_limit", L.THROTTLE_BPS) in q.calls
    assert ("set_download_limit", 0) in q.calls
    assert q.calls.index(("set_download_limit", 0)) > seq.index("start")


def test_repoint_stops_torrent_when_recheck_fails():
    q = StubQbt(PACK_T, PACK)
    L.missing_videos = lambda *a, **k: []
    L.wait_complete = lambda *a, **k: (False, "timeout")
    ok, reason = L.repoint(q, "abc123", "/tv/S/Season 06",
                           log=lambda *a: None, discard=lambda p, log: None)
    assert not ok and "rolled back" in reason
    assert ("stop", "abc123") in q.calls


def test_add_sends_skip_checking_flag():
    """temp_path routes incomplete torrents away from the save path."""
    captured = []

    class Cap(L.Qbt):
        def __init__(self):
            self.base = "http://x/api/v2"
            self.origin = "http://x"

            class Op:
                addheaders = []

                def open(self, req, timeout=None):
                    captured.append(req.data)

                    class R:
                        @staticmethod
                        def read():
                            return b"Ok."

                    return R()

            self._op = Op()

    Cap().add(b"blob", "/tv/Show")
    assert b'name="skip_checking"\r\n\r\ntrue' in captured[-1]
    Cap().add(b"blob", "/downloads", skip_checking=False)
    assert b'name="skip_checking"\r\n\r\nfalse' in captured[-1]


def test_rollback_readd_checks_normally():
    """The rollback returns to a proven-good copy, so it must verify it."""
    q = StubQbt(PACK_T, PACK)
    L.missing_videos = lambda *a, **k: []
    L.wait_complete = lambda *a, **k: (False, "timeout")
    L.repoint(q, "abc123", "/tv/S/Season 06", log=lambda *a: None, discard=lambda p, log: None)
    assert q.skip_checking_calls == [True, False]


class Clock:
    """A qBittorrent that reports a scripted sequence of states."""

    def __init__(self, states):
        self.states = list(states)

    def torrent(self, h):
        if not self.states:
            return None
        return self.states.pop(0)


def test_wait_complete_requires_a_check_to_have_run():
    """skip_checking makes amount_left 0 immediately; that is not verification."""
    q = Clock([
        {"state": "stoppedUP", "progress": 0.0, "amount_left": 0},
        {"state": "checkingUP", "progress": 0.5, "amount_left": 0},
        {"state": "checkingUP", "progress": 0.9, "amount_left": 0},
        {"state": "stalledUP", "progress": 1.0, "amount_left": 0},
    ])
    ok, why = L.wait_complete(q, "h", 0, timeout=2, interval=0, sleep=lambda s: None)
    assert ok, why
    assert "verified" in why


def test_wait_complete_rejects_check_that_finished_incomplete():
    q = Clock([
        {"state": "checkingUP", "progress": 0.2, "amount_left": 9},
        {"state": "stalledDL", "progress": 0.4, "amount_left": 9},
    ])
    ok, why = L.wait_complete(q, "h", 0, timeout=2, interval=0, sleep=lambda s: None)
    assert not ok and "progress=0.4000" in why


def test_wait_complete_reports_missing_files():
    q = Clock([{"state": "missingFiles", "progress": 0.0, "amount_left": 1}])
    ok, why = L.wait_complete(q, "h", 0, timeout=2, interval=0, sleep=lambda s: None)
    assert not ok and "missingFiles" in why
