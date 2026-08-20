import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest
from arcane_deploy.env import load_env_file


class TestLoadEnvFile(unittest.TestCase):
    def test_parses_simple_key_values(self):
        path = Path("test_fixture_1.env")
        path.write_text("FOO=bar\nBAZ=qux\n")
        try:
            result = load_env_file(path)
        finally:
            path.unlink()
        self.assertEqual(result, {"FOO": "bar", "BAZ": "qux"})

    def test_ignores_comments_and_blank_lines(self):
        path = Path("test_fixture_2.env")
        path.write_text("# comment\n\nFOO=bar\n")
        try:
            result = load_env_file(path)
        finally:
            path.unlink()
        self.assertEqual(result, {"FOO": "bar"})

    def test_missing_file_returns_empty_dict(self):
        result = load_env_file(Path("does-not-exist.env"))
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
