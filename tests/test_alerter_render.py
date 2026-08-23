import sys
import threading
import time
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

    def test_window_start_is_labelled_in_the_body(self):
        d = data(quiet=True)
        d["window_start"] = time.mktime(time.strptime("23 Aug 2026 08:00", "%d %b %Y %H:%M"))
        _, body = render(d)
        self.assertIn("since 23 Aug 08:00", body)

    def test_requested_section_lists_by_requester(self):
        d = data(quiet=True)
        d["requests"] = {"marcia": ["Blue Eye Samurai (2023) — tv"]}
        _, body = render(d)
        self.assertIn("marcia", body)
        self.assertIn("Blue Eye Samurai", body)

    def test_left_section_lists_reclaimed_torrents(self):
        d = data(quiet=True)
        d["left"] = [{"name": "Old Movie", "bytes": 5e9}]
        _, body = render(d)
        self.assertIn("Old Movie", body)
        self.assertIn("5.0 GB", body)

    def test_contributed_section_always_renders(self):
        d = data(quiet=True)
        d["contributed"] = {"uploads_delta": 12, "ul_bytes_delta": 2e9, "mean_seed_ratio": 1.5}
        _, body = render(d)
        self.assertIn("Contributed", body)
        self.assertIn("1.50", body)

    def test_backlog_section_renders_when_present(self):
        d = data(quiet=True)
        d["backlog"] = {"wanted_missing": 4, "wanted_missing_delta": -2,
                        "queue_count": 1, "success_rate": 92.0, "success_rate_delta": 3.0}
        _, body = render(d)
        self.assertIn("Backlog", body)
        self.assertIn("92.0%", body)

    def test_long_lists_are_capped_with_a_more_line(self):
        items = ["Album %d" % i for i in range(40)]
        _, body = render(data(quiet=False, arrived={"music": items}))
        self.assertIn("… and 15 more", body)
        self.assertNotIn("Album 39", body)

    def test_html_is_escaped(self):
        _, body = render(data(quiet=False, arrived={"music": ["<script>bad</script>"]}))
        self.assertNotIn("<script>bad</script>", body)


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
