import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arcane_deploy import cli


class TestRun(unittest.TestCase):
    def _repo_with(self, tmp_path: Path, compose: str = "services: {}", env: str = "FOO=bar\n"):
        (tmp_path / "docker-compose.yml").write_text(compose)
        (tmp_path / ".env").write_text(env)
        return tmp_path

    def test_missing_required_keys_returns_1(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._repo_with(Path(tmp))
            result = cli.run(repo_root, environ={})
            self.assertEqual(result, 1)

    def test_missing_compose_file_returns_1(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / ".env").write_text("")
            environ = {
                "ARCANE_URL": "https://arcane.example.com/api",
                "ARCANE_API_TOKEN": "key",
                "ARCANE_ENVIRONMENT_NAME": "vps",
            }
            result = cli.run(repo_root, environ=environ)
            self.assertEqual(result, 1)

    @patch("arcane_deploy.cli.ArcaneClient")
    def test_creates_project_when_it_does_not_exist(self, mock_client_cls):
        import tempfile

        mock_client = mock_client_cls.return_value
        mock_client.list_environments.return_value = [{"id": "env-1", "name": "vps"}]
        mock_client.list_projects.return_value = []
        mock_client.create_project.return_value = {"id": "p1"}
        mock_client.get_project.return_value = {
            "status": "running",
            "serviceCount": 1,
            "runningCount": 1,
        }

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._repo_with(Path(tmp))
            environ = {
                "ARCANE_URL": "https://arcane.example.com/api",
                "ARCANE_API_TOKEN": "key",
                "ARCANE_ENVIRONMENT_NAME": "vps",
                "ARCANE_PROJECT_NAME": "arr-stack",
            }
            result = cli.run(repo_root, environ=environ)

        self.assertEqual(result, 0)
        mock_client.list_environments.assert_called_once_with(search="vps")
        mock_client.list_projects.assert_called_once_with("env-1", search="arr-stack")
        mock_client.create_project.assert_called_once()
        mock_client.deploy_project.assert_called_once_with("env-1", "p1", redeploy=False)

    @patch("arcane_deploy.cli.ArcaneClient")
    def test_updates_project_when_it_already_exists(self, mock_client_cls):
        import tempfile

        mock_client = mock_client_cls.return_value
        mock_client.list_environments.return_value = [{"id": "env-1", "name": "vps"}]
        mock_client.list_projects.return_value = [{"id": "p1", "name": "arr-stack"}]
        mock_client.update_project.return_value = {"id": "p1"}
        mock_client.get_project.return_value = {
            "status": "running",
            "serviceCount": 1,
            "runningCount": 1,
        }

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = self._repo_with(Path(tmp))
            environ = {
                "ARCANE_URL": "https://arcane.example.com/api",
                "ARCANE_API_TOKEN": "key",
                "ARCANE_ENVIRONMENT_NAME": "vps",
                "ARCANE_PROJECT_NAME": "arr-stack",
            }
            result = cli.run(repo_root, environ=environ)

        self.assertEqual(result, 0)
        mock_client.list_environments.assert_called_once_with(search="vps")
        mock_client.list_projects.assert_called_once_with("env-1", search="arr-stack")
        mock_client.update_project.assert_called_once_with("env-1", "p1", {
            "name": "arr-stack",
            "composeContent": "services: {}",
            "envContent": "FOO=bar\n",
        })
        mock_client.deploy_project.assert_called_once_with("env-1", "p1", redeploy=True)


if __name__ == "__main__":
    unittest.main()
