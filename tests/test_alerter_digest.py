import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from state import Store
from evaluate import HEALTHY, ERROR
import digest
from digest import series_at, gather, DAY


def metrics_db(rows):
    path = Path(tempfile.mkdtemp()) / "metrics.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE samples (ts INTEGER, metric TEXT, value REAL)")
    conn.executemany("INSERT INTO samples VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


class TestSeriesAt(unittest.TestCase):
    def test_returns_the_sample_nearest_before_the_timestamp(self):
        path = metrics_db([(100, "a", 1.0), (200, "a", 2.0), (300, "a", 3.0)])
        self.assertEqual(series_at(path, "a", 250), 2.0)

    def test_returns_none_when_nothing_precedes(self):
        path = metrics_db([(300, "a", 3.0)])
        self.assertIsNone(series_at(path, "a", 100))


class TestGather(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.window_start = self.now - DAY
        self.path = metrics_db(
            [
                (self.window_start, "hetzner_box.used_bytes", 700e9),
                (self.now, "hetzner_box.used_bytes", 730e9),
                (self.window_start, "qbt.seeding_bytes", 260e9),
                (self.now, "qbt.seeding_bytes", 284e9),
            ]
        )
        self.store = Store(Path(tempfile.mkdtemp()) / "alerts.db")

    def test_capacity_reports_the_delta(self):
        result = gather(self.path, self.store, self.window_start, self.now, {})
        self.assertAlmostEqual(result["capacity"]["box_delta_bytes"], 30e9, delta=1e6)

    def test_arrivals_come_from_the_injected_fetchers(self):
        result = gather(
            self.path, self.store, self.window_start, self.now,
            {"music": lambda: ["Opeth - Blackwater Park"]},
        )
        self.assertEqual(result["arrived"]["music"], ["Opeth - Blackwater Park"])

    def test_a_failing_fetcher_costs_only_its_section(self):
        def boom():
            raise RuntimeError("Radarr is down")

        result = gather(
            self.path, self.store, self.window_start, self.now,
            {"movies": boom, "music": lambda: ["ok"]},
        )
        self.assertEqual(result["arrived"]["movies"], [])
        self.assertEqual(result["arrived"]["music"], ["ok"])

    def test_quiet_is_true_when_nothing_arrived(self):
        result = gather(
            self.path, self.store, self.window_start, self.now, {"music": lambda: []}
        )
        self.assertTrue(result["quiet"])

    def test_quiet_is_false_when_something_arrived(self):
        result = gather(
            self.path, self.store, self.window_start, self.now, {"music": lambda: ["x"]}
        )
        self.assertFalse(result["quiet"])

    def test_health_section_lists_transitions(self):
        self.store.record(self.now - 100, "VPS disk", HEALTHY, ERROR, 95.0, "t", "b")
        result = gather(self.path, self.store, self.window_start, self.now, {})
        self.assertEqual(len(result["health"]["transitions"]), 1)

    def test_health_uses_the_window_start_not_a_fixed_day(self):
        old_window_start = self.now - 3 * DAY
        self.store.record(self.now - 2 * DAY, "VPS disk", HEALTHY, ERROR, 95.0, "t", "b")
        result = gather(self.path, self.store, old_window_start, self.now, {})
        self.assertEqual(len(result["health"]["transitions"]), 1)
        result_recent = gather(self.path, self.store, self.window_start, self.now, {})
        self.assertEqual(len(result_recent["health"]["transitions"]), 0)

    def test_requests_come_from_the_injected_fetcher(self):
        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            requests_fetcher=lambda: {"marcia": ["Blue Eye Samurai (2023) — tv"]},
        )
        self.assertEqual(result["requests"]["marcia"], ["Blue Eye Samurai (2023) — tv"])

    def test_a_failing_requests_fetcher_costs_only_that_section(self):
        def boom():
            raise RuntimeError("Seerr is down")

        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            requests_fetcher=boom,
        )
        self.assertEqual(result["requests"], {})

    def test_contributed_merges_metric_deltas_with_the_live_ratio(self):
        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            contributed_fetcher=lambda: 1.75,
        )
        self.assertEqual(result["contributed"]["mean_seed_ratio"], 1.75)
        self.assertAlmostEqual(
            result["contributed"]["seeding_bytes"], 284e9, delta=1e6
        )

    def test_a_failing_contributed_fetcher_costs_only_that_field(self):
        def boom():
            raise RuntimeError("qBittorrent is down")

        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            contributed_fetcher=boom,
        )
        self.assertIsNone(result["contributed"]["mean_seed_ratio"])

    def test_left_is_empty_before_any_snapshot_is_committed(self):
        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            left_fetcher=lambda: [{"hash": "a", "name": "Movie", "bytes": 1000.0}],
        )
        self.assertEqual(result["left"], [])

    def test_left_reports_torrents_gone_from_the_new_snapshot(self):
        self.store.commit_torrent_snapshot(
            self.window_start,
            [{"hash": "a", "name": "Gone Movie", "bytes": 1000.0},
             {"hash": "b", "name": "Still Here", "bytes": 2000.0}],
        )
        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            left_fetcher=lambda: [{"hash": "b", "name": "Still Here", "bytes": 2000.0}],
        )
        self.assertEqual(result["left"], [{"name": "Gone Movie", "bytes": 1000.0}])

    def test_a_failing_left_fetcher_costs_only_that_section(self):
        def boom():
            raise RuntimeError("qBittorrent is down")

        self.store.commit_torrent_snapshot(
            self.window_start, [{"hash": "a", "name": "Gone Movie", "bytes": 1000.0}]
        )
        result = gather(
            self.path, self.store, self.window_start, self.now, {}, left_fetcher=boom
        )
        self.assertEqual(result["left"], [])

    def test_backlog_reports_the_delta_over_the_window(self):
        conn = sqlite3.connect(self.path)
        conn.executemany(
            "INSERT INTO samples VALUES (?, ?, ?)",
            [
                (self.window_start, "lidarr.wanted_missing", 10.0),
                (self.now, "lidarr.wanted_missing", 4.0),
                (self.now, "lidarr.queue_count", 2.0),
            ],
        )
        conn.commit()
        conn.close()
        result = gather(self.path, self.store, self.window_start, self.now, {})
        self.assertEqual(result["backlog"]["wanted_missing"], 4.0)
        self.assertEqual(result["backlog"]["wanted_missing_delta"], -6.0)
        self.assertEqual(result["backlog"]["queue_count"], 2.0)

    def test_current_torrents_are_exposed_for_the_caller_to_commit(self):
        result = gather(
            self.path, self.store, self.window_start, self.now, {},
            left_fetcher=lambda: [{"hash": "a", "name": "Movie", "bytes": 1000.0}],
        )
        self.assertEqual(
            result["_current_torrents"], [{"hash": "a", "name": "Movie", "bytes": 1000.0}]
        )


class TestFetchLidarrArrivals(unittest.TestCase):
    def test_groups_by_album_and_resolves_names_in_one_bulk_call(self):
        events = [
            {"albumId": 1, "sourceTitle": "01. Stand"},
            {"albumId": 1, "sourceTitle": "02. Second"},
            {"albumId": 2, "sourceTitle": "01. Other"},
        ]
        albums = [
            {"id": 1, "title": "Blackwater Park", "artist": {"artistName": "Opeth"}},
            {"id": 2, "title": "Ghost Reveries", "artist": {"artistName": "Opeth"}},
        ]
        with mock.patch("digest.http_json", side_effect=[events, albums]):
            result = digest.fetch_lidarr_arrivals("http://lidarr", "key", 0, DAY)
        self.assertEqual(
            result,
            ["Opeth — Blackwater Park (2 tracks)", "Opeth — Ghost Reveries (1 tracks)"],
        )

    def test_no_events_short_circuits_the_bulk_call(self):
        with mock.patch("digest.http_json", side_effect=[[]]) as mocked:
            result = digest.fetch_lidarr_arrivals("http://lidarr", "key", 0, DAY)
        self.assertEqual(result, [])
        self.assertEqual(mocked.call_count, 1)


class TestFetchSonarrArrivals(unittest.TestCase):
    def test_groups_by_series_and_counts_episodes(self):
        events = [{"seriesId": 2}, {"seriesId": 2}, {"seriesId": 5}]
        series = [{"id": 2, "title": "Reacher"}, {"id": 5, "title": "Severance"}]
        with mock.patch("digest.http_json", side_effect=[events, series]):
            result = digest.fetch_sonarr_arrivals("http://sonarr", "key", 0, DAY)
        self.assertEqual(result, ["Reacher (2 episodes)", "Severance (1 episodes)"])


class TestFetchRadarrArrivals(unittest.TestCase):
    def test_dedupes_and_resolves_titles(self):
        events = [{"movieId": 6}, {"movieId": 6}]
        movies = [{"id": 6, "title": "Un Chien Andalou"}]
        with mock.patch("digest.http_json", side_effect=[events, movies]):
            result = digest.fetch_radarr_arrivals("http://radarr", "key", 0, DAY)
        self.assertEqual(result, ["Un Chien Andalou"])


class TestFetchBazarrArrivals(unittest.TestCase):
    def test_filters_by_window_and_renders_episode_and_language(self):
        now = int(time.mktime(time.strptime("08/23/26 14:00:00", digest.BAZARR_TIMESTAMP_FORMAT)))
        window_start = now - DAY
        payload = {
            "data": [
                {
                    "seriesTitle": "Reacher", "episode_number": "1x6",
                    "language": {"name": "Dutch"},
                    "parsed_timestamp": "08/23/26 13:46:35",
                },
                {
                    "seriesTitle": "Too Old", "episode_number": "1x1",
                    "language": {"name": "English"},
                    "parsed_timestamp": "08/20/26 09:00:00",
                },
            ]
        }
        with mock.patch("digest.http_json", return_value=payload):
            result = digest.fetch_bazarr_arrivals("http://bazarr", "key", window_start, now)
        self.assertEqual(result, ["Reacher S01E06 — Dutch"])


class TestFetchRequests(unittest.TestCase):
    def test_groups_by_requester_and_caches_title_lookups(self):
        payload = {
            "results": [
                {
                    "createdAt": "2026-08-23T16:56:34.000Z", "type": "tv",
                    "media": {"tmdbId": 2316},
                    "requestedBy": {"displayName": "mauricio"},
                },
                {
                    "createdAt": "2026-08-23T17:00:00.000Z", "type": "tv",
                    "media": {"tmdbId": 2316},
                    "requestedBy": {"displayName": "mauricio"},
                },
                {
                    "createdAt": "2026-08-23T10:00:00.000Z", "type": "movie",
                    "media": {"tmdbId": 286217},
                    "requestedBy": {"displayName": "marcia"},
                },
            ]
        }
        now = digest._parse_utc_iso("2026-08-23T18:00:00.000Z")
        window_start = now - DAY

        def fake_http_json(url, headers=None, opener=None, timeout=digest.HTTP_TIMEOUT):
            if "request?" in url:
                return payload
            if "/tv/2316" in url:
                return {"name": "The Office", "firstAirDate": "2005-03-24"}
            if "/movie/286217" in url:
                return {"title": "The Martian", "releaseDate": "2015-09-30"}
            raise AssertionError("unexpected url " + url)

        with mock.patch("digest.http_json", side_effect=fake_http_json) as mocked:
            result = digest.fetch_requests("http://seerr", "key", window_start, now)
        self.assertEqual(
            result,
            {
                "mauricio": ["The Office (2005) — tv", "The Office (2005) — tv"],
                "marcia": ["The Martian (2015) — movie"],
            },
        )
        # one request call plus one lookup per distinct tmdbId, not per request
        self.assertEqual(mocked.call_count, 3)

    def test_a_failed_title_lookup_falls_back_to_the_raw_id(self):
        payload = {
            "results": [
                {
                    "createdAt": "2026-08-23T16:56:34.000Z", "type": "tv",
                    "media": {"tmdbId": 999},
                    "requestedBy": {"displayName": "mauricio"},
                },
            ]
        }
        now = digest._parse_utc_iso("2026-08-23T18:00:00.000Z")
        window_start = now - DAY

        def fake_http_json(url, headers=None, opener=None, timeout=digest.HTTP_TIMEOUT):
            if "request?" in url:
                return payload
            raise RuntimeError("Seerr is down")

        with mock.patch("digest.http_json", side_effect=fake_http_json):
            result = digest.fetch_requests("http://seerr", "key", window_start, now)
        self.assertEqual(result, {"mauricio": ["tmdb #999 — tv"]})


class TestFetchMeanSeedRatio(unittest.TestCase):
    def test_averages_ratio_over_seeding_torrents_only(self):
        torrents = [
            {"state": "uploading", "ratio": 2.0},
            {"state": "stalledUP", "ratio": 4.0},
            {"state": "downloading", "ratio": 0.0},
        ]
        with mock.patch("digest._qbt_torrents", return_value=torrents):
            result = digest.fetch_mean_seed_ratio("http://qbt", "u", "p")
        self.assertEqual(result, 3.0)

    def test_none_when_nothing_is_seeding(self):
        with mock.patch("digest._qbt_torrents", return_value=[{"state": "downloading"}]):
            result = digest.fetch_mean_seed_ratio("http://qbt", "u", "p")
        self.assertIsNone(result)


class TestFetchTorrentSnapshot(unittest.TestCase):
    def test_captures_hash_name_and_completed_bytes(self):
        torrents = [{"hash": "a", "name": "Movie", "completed": 1234.0}]
        with mock.patch("digest._qbt_torrents", return_value=torrents):
            result = digest.fetch_torrent_snapshot("http://qbt", "u", "p")
        self.assertEqual(result, [{"hash": "a", "name": "Movie", "bytes": 1234.0}])


if __name__ == "__main__":
    unittest.main()
