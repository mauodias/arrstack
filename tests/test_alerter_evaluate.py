import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from rules import Rule
from evaluate import classify, resolve_value, HEALTHY, WARNING, ERROR


def rule(**kw):
    base = dict(metric="m", name="n", direction="above", warning=80.0, error=90.0)
    base.update(kw)
    return Rule(**base)


class TestClassify(unittest.TestCase):
    def test_above_healthy_below_warning(self):
        self.assertEqual(classify(rule(), 50.0), HEALTHY)

    def test_above_warning_at_threshold(self):
        self.assertEqual(classify(rule(), 80.0), WARNING)

    def test_above_error_at_threshold(self):
        self.assertEqual(classify(rule(), 90.0), ERROR)

    def test_below_direction_inverts(self):
        r = rule(direction="below", warning=75.0, error=60.0)
        self.assertEqual(classify(r, 90.0), HEALTHY)
        self.assertEqual(classify(r, 75.0), WARNING)
        self.assertEqual(classify(r, 60.0), ERROR)

    def test_error_only_rule_never_warns(self):
        r = rule(warning=None, error=90.0)
        self.assertEqual(classify(r, 85.0), HEALTHY)
        self.assertEqual(classify(r, 95.0), ERROR)

    def test_warning_only_rule_never_errors(self):
        r = rule(warning=80.0, error=None)
        self.assertEqual(classify(r, 99.0), WARNING)


class TestResolveValue(unittest.TestCase):
    def test_plain_metric(self):
        self.assertEqual(resolve_value(rule(metric="a"), {"a": 42.0}), 42.0)

    def test_ratio_becomes_a_percentage(self):
        r = rule(metric="used", of="total")
        self.assertAlmostEqual(resolve_value(r, {"used": 25.0, "total": 200.0}), 12.5)

    def test_missing_metric_returns_none(self):
        self.assertIsNone(resolve_value(rule(metric="a"), {}))

    def test_missing_denominator_returns_none(self):
        r = rule(metric="used", of="total")
        self.assertIsNone(resolve_value(r, {"used": 1.0}))

    def test_zero_denominator_returns_none(self):
        r = rule(metric="used", of="total")
        self.assertIsNone(resolve_value(r, {"used": 1.0, "total": 0.0}))


if __name__ == "__main__":
    unittest.main()
