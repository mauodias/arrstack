import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from state import Store
from evaluate import HEALTHY, ERROR
from digest import series_at, gather


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
        day = 86400
        self.path = metrics_db(
            [
                (self.now - day, "hetzner_box.used_bytes", 700e9),
                (self.now, "hetzner_box.used_bytes", 730e9),
                (self.now - day, "qbt.seeding_bytes", 260e9),
                (self.now, "qbt.seeding_bytes", 284e9),
            ]
        )
        self.store = Store(Path(tempfile.mkdtemp()) / "alerts.db")

    def test_capacity_reports_the_delta(self):
        result = gather(self.path, self.store, self.now, {})
        self.assertAlmostEqual(result["capacity"]["box_delta_bytes"], 30e9, delta=1e6)

    def test_arrivals_come_from_the_injected_fetchers(self):
        result = gather(
            self.path, self.store, self.now,
            {"music": lambda: ["Opeth - Blackwater Park"]},
        )
        self.assertEqual(result["arrived"]["music"], ["Opeth - Blackwater Park"])

    def test_a_failing_fetcher_costs_only_its_section(self):
        def boom():
            raise RuntimeError("Radarr is down")

        result = gather(
            self.path, self.store, self.now,
            {"movies": boom, "music": lambda: ["ok"]},
        )
        self.assertEqual(result["arrived"]["movies"], [])
        self.assertEqual(result["arrived"]["music"], ["ok"])

    def test_quiet_is_true_when_nothing_arrived(self):
        result = gather(self.path, self.store, self.now, {"music": lambda: []})
        self.assertTrue(result["quiet"])

    def test_quiet_is_false_when_something_arrived(self):
        result = gather(self.path, self.store, self.now, {"music": lambda: ["x"]})
        self.assertFalse(result["quiet"])

    def test_health_section_lists_transitions(self):
        self.store.record(self.now - 100, "VPS disk", HEALTHY, ERROR, 95.0, "t", "b")
        result = gather(self.path, self.store, self.now, {})
        self.assertEqual(len(result["health"]["transitions"]), 1)


if __name__ == "__main__":
    unittest.main()
