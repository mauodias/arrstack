import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arcane_deploy import client


class TestFindEnvironment(unittest.TestCase):
    def test_finds_matching_environment_by_name(self):
        environments = [{"id": "1", "name": "vps"}, {"id": "2", "name": "home"}]
        result = client.find_environment(environments, "home")
        self.assertEqual(result["id"], "2")

    def test_returns_none_when_not_found(self):
        result = client.find_environment([{"id": "1", "name": "vps"}], "missing")
        self.assertIsNone(result)


class TestFindProject(unittest.TestCase):
    def test_finds_matching_project_by_name(self):
        projects = [{"id": "a", "name": "arr-stack"}, {"id": "b", "name": "other"}]
        result = client.find_project(projects, "arr-stack")
        self.assertEqual(result["id"], "a")

    def test_returns_none_when_not_found(self):
        result = client.find_project([], "arr-stack")
        self.assertIsNone(result)


class TestBuildProjectPayload(unittest.TestCase):
    def test_builds_expected_payload_shape(self):
        payload = client.build_project_payload("arr-stack", "services: {}", "FOO=bar")
        self.assertEqual(
            payload,
            {"name": "arr-stack", "composeContent": "services: {}", "envContent": "FOO=bar"},
        )


def _fake_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    mock_response = MagicMock()
    mock_response.read.return_value = body
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = False
    return mock_response


class TestArcaneClient(unittest.TestCase):
    def setUp(self):
        self.client = client.ArcaneClient("https://arcane.example.com/api", "test-key")

    @patch("arcane_deploy.client.urllib.request.urlopen")
    def test_list_environments_returns_data_list(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"success": True, "data": [{"id": "1"}]})
        result = self.client.list_environments()
        self.assertEqual(result, [{"id": "1"}])
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://arcane.example.com/api/environments")
        self.assertEqual(request.get_header("X-api-key"), "test-key")

    @patch("arcane_deploy.client.urllib.request.urlopen")
    def test_create_project_sends_post_with_payload(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"success": True, "data": {"id": "p1"}})
        payload = {"name": "arr-stack", "composeContent": "x", "envContent": "y"}
        result = self.client.create_project("env-1", payload)
        self.assertEqual(result, {"id": "p1"})
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://arcane.example.com/api/environments/env-1/projects")
        self.assertEqual(json.loads(request.data), payload)

    @patch("arcane_deploy.client.urllib.request.urlopen")
    def test_update_project_sends_put(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"success": True, "data": {"id": "p1"}})
        payload = {"name": "arr-stack", "composeContent": "x", "envContent": "y"}
        self.client.update_project("env-1", "p1", payload)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, "https://arcane.example.com/api/environments/env-1/projects/p1")

    @patch("arcane_deploy.client.urllib.request.urlopen")
    def test_deploy_project_uses_up_when_not_redeploy(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"success": True, "data": {"message": "ok"}})
        self.client.deploy_project("env-1", "p1", redeploy=False)
        request = mock_urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/projects/p1/up"))

    @patch("arcane_deploy.client.urllib.request.urlopen")
    def test_deploy_project_uses_redeploy_when_redeploy_true(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"success": True, "data": {"message": "ok"}})
        self.client.deploy_project("env-1", "p1", redeploy=True)
        request = mock_urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/projects/p1/redeploy"))

    @patch("arcane_deploy.client.urllib.request.urlopen")
    def test_get_project_sends_get(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"success": True, "data": {"id": "p1", "status": "running"}})
        result = self.client.get_project("env-1", "p1")
        self.assertEqual(result["status"], "running")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
