import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from rules import Rule
from notify import format_alert, send_ntfy
from evaluate import HEALTHY, WARNING, ERROR

RECEIVED = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(
            {
                "path": self.path,
                "title": self.headers.get("Title"),
                "agent": self.headers.get("User-Agent"),
                "body": self.rfile.read(length).decode(),
            }
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class TestFormatAlert(unittest.TestCase):
    def setUp(self):
        self.rule = Rule(metric="m", name="VPS disk", direction="above",
                         warning=80.0, error=90.0)

    def test_error_uses_the_red_circle_tag(self):
        title, _, tags = format_alert(self.rule, WARNING, ERROR, 91.5)
        self.assertEqual(tags, "red_circle")
        self.assertIn("VPS disk", title)

    def test_warning_uses_the_yellow_circle_tag(self):
        _, _, tags = format_alert(self.rule, HEALTHY, WARNING, 82.0)
        self.assertEqual(tags, "yellow_circle")

    def test_recovery_uses_the_green_circle_tag(self):
        _, _, tags = format_alert(self.rule, ERROR, HEALTHY, 40.0)
        self.assertEqual(tags, "green_circle")

    def test_title_is_latin_1_encodable(self):
        title, _, _ = format_alert(self.rule, HEALTHY, ERROR, 91.5)
        title.encode("latin-1")

    def test_body_names_both_states_and_the_value(self):
        _, body, _ = format_alert(self.rule, HEALTHY, ERROR, 91.5)
        self.assertIn("91.5", body)
        self.assertIn(HEALTHY, body)
        self.assertIn(ERROR, body)


class TestSendNtfy(unittest.TestCase):
    def setUp(self):
        RECEIVED.clear()
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()

    def test_posts_title_and_body_to_the_topic(self):
        self.assertTrue(
            send_ntfy(self.base, "sometopic", "a title", "a body", "green_circle")
        )
        self.assertEqual(RECEIVED[0]["path"], "/sometopic")
        self.assertEqual(RECEIVED[0]["title"], "a title")
        self.assertEqual(RECEIVED[0]["body"], "a body")

    def test_sends_a_user_agent(self):
        send_ntfy(self.base, "sometopic", "a title", "a body", "green_circle")
        self.assertEqual(RECEIVED[0]["agent"], "arrstack-alerter/1.0")

    def test_unreachable_server_returns_false(self):
        self.assertFalse(
            send_ntfy("http://127.0.0.1:1", "t", "a", "b", "red_circle")
        )


if __name__ == "__main__":
    unittest.main()
