import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from state import Store
from evaluate import HEALTHY, WARNING, ERROR


def store():
    return Store(Path(tempfile.mkdtemp()) / "alerts.db")


class TestDebounce(unittest.TestCase):
    def test_first_observation_seeds_without_transition(self):
        s = store()
        self.assertIsNone(s.observe("disk", ERROR, debounce=2))
        self.assertEqual(s.current("disk"), ERROR)

    def test_change_needs_consecutive_observations(self):
        s = store()
        s.observe("disk", HEALTHY, debounce=2)
        self.assertIsNone(s.observe("disk", ERROR, debounce=2))
        self.assertEqual(s.observe("disk", ERROR, debounce=2), ERROR)
        self.assertEqual(s.current("disk"), ERROR)

    def test_flapping_never_transitions(self):
        s = store()
        s.observe("disk", HEALTHY, debounce=2)
        for _ in range(6):
            self.assertIsNone(s.observe("disk", ERROR, debounce=2))
            self.assertIsNone(s.observe("disk", HEALTHY, debounce=2))
        self.assertEqual(s.current("disk"), HEALTHY)

    def test_debounce_of_one_transitions_immediately(self):
        s = store()
        s.observe("disk", HEALTHY, debounce=1)
        self.assertEqual(s.observe("disk", ERROR, debounce=1), ERROR)

    def test_state_survives_reopening(self):
        path = Path(tempfile.mkdtemp()) / "alerts.db"
        Store(path).observe("disk", ERROR, debounce=2)
        self.assertEqual(Store(path).current("disk"), ERROR)


class TestRecord(unittest.TestCase):
    def test_open_alerts_excludes_recovered(self):
        s = store()
        s.record(100, "disk", HEALTHY, ERROR, 95.0, "t1", "b1")
        s.record(200, "swap", HEALTHY, WARNING, 60.0, "t2", "b2")
        s.record(300, "disk", ERROR, HEALTHY, 40.0, "t3", "b3")
        names = [a["rule"] for a in s.open_alerts()]
        self.assertEqual(names, ["swap"])

    def test_open_alert_carries_title_and_body(self):
        s = store()
        s.record(100, "disk", HEALTHY, ERROR, 95.0, "Disk 95%", "details here")
        alert = s.open_alerts()[0]
        self.assertEqual(alert["title"], "Disk 95%")
        self.assertEqual(alert["body"], "details here")
        self.assertEqual(alert["state"], ERROR)

    def test_transitions_since_filters_by_time(self):
        s = store()
        s.record(100, "disk", HEALTHY, ERROR, 95.0, "t", "b")
        s.record(300, "swap", HEALTHY, WARNING, 60.0, "t", "b")
        self.assertEqual(len(s.transitions_since(200)), 1)


class TestDigestMeta(unittest.TestCase):
    def test_no_last_send_before_the_first(self):
        s = store()
        self.assertIsNone(s.last_digest_sent_at())

    def test_mark_digest_sent_is_read_back(self):
        s = store()
        s.mark_digest_sent(12345)
        self.assertEqual(s.last_digest_sent_at(), 12345)

    def test_mark_digest_sent_overwrites(self):
        s = store()
        s.mark_digest_sent(100)
        s.mark_digest_sent(200)
        self.assertEqual(s.last_digest_sent_at(), 200)


class TestTorrentSnapshot(unittest.TestCase):
    def test_no_previous_snapshot_before_the_first_commit(self):
        s = store()
        self.assertIsNone(s.previous_torrent_snapshot())

    def test_committed_snapshot_is_read_back(self):
        s = store()
        s.commit_torrent_snapshot(
            100, [{"hash": "a", "name": "Movie", "bytes": 1000.0}]
        )
        self.assertEqual(
            s.previous_torrent_snapshot(),
            [{"hash": "a", "name": "Movie", "bytes": 1000.0}],
        )

    def test_committing_again_replaces_the_previous_snapshot(self):
        s = store()
        s.commit_torrent_snapshot(100, [{"hash": "a", "name": "Old", "bytes": 1.0}])
        s.commit_torrent_snapshot(200, [{"hash": "b", "name": "New", "bytes": 2.0}])
        hashes = [item["hash"] for item in s.previous_torrent_snapshot()]
        self.assertEqual(hashes, ["b"])

    def test_items_without_a_hash_are_dropped(self):
        s = store()
        s.commit_torrent_snapshot(
            100, [{"hash": "", "name": "Bad", "bytes": 1.0},
                  {"hash": "a", "name": "Good", "bytes": 2.0}]
        )
        hashes = [item["hash"] for item in s.previous_torrent_snapshot()]
        self.assertEqual(hashes, ["a"])


if __name__ == "__main__":
    unittest.main()
