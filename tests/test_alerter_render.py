import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from digest import render
from notify import send_email

RECEIVED = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(self.rfile.read(length).decode())
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def data(quiet, arrived=None):
    return {
        "now": 0,
        "arrived": arrived or {},
        "quiet": quiet,
        "capacity": {"box_used_bytes": 730e9, "box_total_bytes": 1099e9,
                     "box_delta_bytes": 30e9, "seeding_bytes": 284e9,
                     "seeding_delta_bytes": 24e9, "vps_disk_used_bytes": 190e9},
        "health": {"transitions": [], "open": []},
    }


class TestRender(unittest.TestCase):
    def test_quiet_day_still_reports_capacity(self):
        subject, body = render(data(quiet=True))
        self.assertIn("Quiet day", body)
        self.assertIn("730", body)

    def test_busy_day_lists_arrivals(self):
        _, body = render(data(quiet=False, arrived={"music": ["Opeth - Orchid"]}))
        self.assertIn("Opeth - Orchid", body)
        self.assertNotIn("Quiet day", body)

    def test_subject_is_not_empty(self):
        subject, _ = render(data(quiet=True))
        self.assertTrue(subject.strip())

    def test_output_is_html(self):
        _, body = render(data(quiet=True))
        self.assertTrue(body.lstrip().startswith("<"))


class TestSendEmail(unittest.TestCase):
    def setUp(self):
        RECEIVED.clear()
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()

    def test_posts_a_json_payload(self):
        ok = send_email("key", "a@b.c", "d@e.f", "subj", "<p>hi</p>",
                        endpoint=self.base)
        self.assertTrue(ok)
        self.assertIn("subj", RECEIVED[0])

    def test_unreachable_endpoint_returns_false(self):
        self.assertFalse(
            send_email("key", "a@b.c", "d@e.f", "s", "<p>h</p>",
                       endpoint="http://127.0.0.1:1")
        )


if __name__ == "__main__":
    unittest.main()
