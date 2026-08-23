import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from rules import Rule
from state import Store
from evaluate import HEALTHY, ERROR
from app import latest_samples, newest_ts, evaluate_once, render_alerts_page


def metrics_db(rows):
    path = Path(tempfile.mkdtemp()) / "metrics.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE samples (ts INTEGER, metric TEXT, value REAL)")
    conn.executemany("INSERT INTO samples VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


class TestLatestSamples(unittest.TestCase):
    def test_returns_the_newest_value_per_metric(self):
        now = int(time.time())
        path = metrics_db([(now - 60, "a", 1.0), (now, "a", 2.0), (now, "b", 3.0)])
        self.assertEqual(latest_samples(path, 3600), {"a": 2.0, "b": 3.0})

    def test_ignores_samples_outside_the_horizon(self):
        now = int(time.time())
        path = metrics_db([(now - 99999, "a", 1.0)])
        self.assertEqual(latest_samples(path, 3600), {})

    def test_unreadable_database_returns_empty(self):
        self.assertEqual(latest_samples(Path("/nonexistent/x.db"), 3600), {})

    def test_newest_ts_reports_the_latest_sample(self):
        now = int(time.time())
        path = metrics_db([(now - 60, "a", 1.0), (now, "a", 2.0)])
        self.assertEqual(newest_ts(path), now)


class TestEvaluateOnce(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.store = Store(Path(tempfile.mkdtemp()) / "alerts.db")
        self.rule = Rule(metric="d", name="VPS disk", direction="above",
                         warning=80.0, error=90.0, debounce=1)

    def send(self, title, body, tags):
        self.sent.append((title, body, tags))
        return True

    def test_first_cycle_is_silent(self):
        evaluate_once([self.rule], {"d": 95.0}, self.store, self.send)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.current("VPS disk"), ERROR)

    def test_transition_sends_one_notification(self):
        evaluate_once([self.rule], {"d": 10.0}, self.store, self.send)
        count = evaluate_once([self.rule], {"d": 95.0}, self.store, self.send)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("VPS disk", self.sent[0][0])

    def test_steady_state_sends_nothing(self):
        evaluate_once([self.rule], {"d": 95.0}, self.store, self.send)
        for _ in range(3):
            evaluate_once([self.rule], {"d": 96.0}, self.store, self.send)
        self.assertEqual(self.sent, [])

    def test_missing_metric_is_skipped(self):
        evaluate_once([self.rule], {}, self.store, self.send)
        self.assertIsNone(self.store.current("VPS disk"))


class TestAlertsPage(unittest.TestCase):
    def test_lists_open_alerts_with_expandable_bodies(self):
        html = render_alerts_page(
            [{"ts": 0, "rule": "VPS disk", "state": ERROR, "value": 95.0,
              "title": "\U0001F534 VPS disk error", "body": "details"}]
        )
        self.assertIn("<details", html)
        self.assertIn("VPS disk", html)
        self.assertIn("details", html)

    def test_says_so_when_nothing_is_open(self):
        html = render_alerts_page([])
        self.assertIn("No open alerts", html)


if __name__ == "__main__":
    unittest.main()
