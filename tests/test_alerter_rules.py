import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from rules import load_rules, RuleError


def write(text):
    path = Path(tempfile.mkdtemp()) / "rules.toml"
    path.write_text(text)
    return path


class TestLoadRules(unittest.TestCase):
    def test_parses_a_minimal_rule(self):
        path = write(
            '[[rule]]\n'
            'metric = "slskd.connected"\n'
            'name = "Soulseek connection"\n'
            'direction = "below"\n'
            'error = 1\n'
        )
        rules = load_rules(path)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].metric, "slskd.connected")
        self.assertEqual(rules[0].direction, "below")
        self.assertEqual(rules[0].error, 1.0)
        self.assertIsNone(rules[0].warning)
        self.assertIsNone(rules[0].of)

    def test_debounce_defaults_to_two(self):
        path = write(
            '[[rule]]\nmetric = "m"\nname = "n"\ndirection = "above"\nerror = 1\n'
        )
        self.assertEqual(load_rules(path)[0].debounce, 2)

    def test_reads_the_of_field(self):
        path = write(
            '[[rule]]\nmetric = "vps_disk.used_bytes"\nof = "vps_disk.total_bytes"\n'
            'name = "VPS disk"\ndirection = "above"\nwarning = 80\nerror = 90\n'
        )
        self.assertEqual(load_rules(path)[0].of, "vps_disk.total_bytes")

    def test_rejects_an_unknown_direction(self):
        path = write(
            '[[rule]]\nmetric = "m"\nname = "n"\ndirection = "sideways"\nerror = 1\n'
        )
        with self.assertRaises(RuleError):
            load_rules(path)

    def test_rejects_a_rule_with_no_threshold(self):
        path = write('[[rule]]\nmetric = "m"\nname = "n"\ndirection = "above"\n')
        with self.assertRaises(RuleError):
            load_rules(path)

    def test_missing_file_raises(self):
        with self.assertRaises(RuleError):
            load_rules(Path("/nonexistent/rules.toml"))


if __name__ == "__main__":
    unittest.main()
