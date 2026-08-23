# Alerting and Daily Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `alerter` container that pushes threshold alerts to ntfy and emails a daily digest via Resend, and move the metrics program out of `docker-compose.yml` into a repo file.

**Architecture:** `alerter` runs beside `metrics`, reads the metrics SQLite read-only through a shared bind mount, evaluates TOML-configured rules into three states, and notifies on every transition. Both containers fetch their Python from GitHub `main` at startup with busybox `wget`, so neither needs a build step. Modules are flat files imported by bare name.

**Tech Stack:** Python 3.12 standard library only (`sqlite3`, `tomllib`, `urllib`, `http.server`, `unittest`), `python:3.12-alpine`, Docker Compose, ntfy, Resend.

**Spec:** `docs/superpowers/specs/2026-08-23-alerting-and-digest-design.md`

## Global Constraints

- **Python standard library only.** No `pip install`, no build step, no new image.
- **`tomllib` requires Python 3.11+.** The image is `python:3.12-alpine`; do not downgrade it.
- **No narrative code comments.** Never explain "was X, now Y" or justify a change against a previous state — that belongs in the commit message. Comments describe code as it currently stands, or explain non-obvious *why*.
- **Flat modules, bare imports.** `alerter/*.py` import each other as `import rules`, not `from alerter import rules`. The container fetches them into one directory.
- **No shell variables in compose `command:` strings.** Docker Compose interpolates `$`. List each `wget` explicitly rather than looping.
- **Tests are `unittest`**, flat in `tests/test_*.py`, run with `python3 -m unittest discover tests -v`.
- **Every external call is individually wrapped.** A failure is logged and skipped, never retried into a flood.
- **ntfy notifications use default priority.** No `high`, no `urgent`.
- **Emoji states:** 🟢 healthy, 🟡 warning, 🔴 error.

---

### Task 1: Move the metrics program into a repo file

The program is currently a 47,811-character string inside `docker-compose.yml`. This is a pure relocation: no behaviour change.

**Files:**
- Create: `metrics/app.py`
- Modify: `docker-compose.yml` (the `metrics` service `command:`)

**Interfaces:**
- Consumes: nothing.
- Produces: `metrics/app.py` served at `https://raw.githubusercontent.com/mauodias/arrstack/main/metrics/app.py`; the metrics SQLite schema `samples(ts INTEGER, metric TEXT, value REAL)` at `/data/metrics.db`, which Task 6 reads.

- [ ] **Step 1: Capture the current page as a baseline**

```bash
curl -s "http://arr-vps:8099/?range=24h" -o /tmp/metrics-before.html
curl -s "http://arr-vps:8099/summary.json" -o /tmp/summary-before.json
wc -c /tmp/metrics-before.html /tmp/summary-before.json
```

- [ ] **Step 2: Extract the program verbatim**

```bash
python3 - <<'PY'
import yaml
d = yaml.safe_load(open("docker-compose.yml"))
cmd = d["services"]["metrics"]["command"]
src = cmd[-1] if isinstance(cmd, list) else cmd
import re
m = re.search(r"<<'?PYEOF'?\n(.*?)\nPYEOF", src, re.S)
prog = m.group(1) if m else src
open("metrics/app.py", "w").write(prog if prog.endswith("\n") else prog + "\n")
print(len(prog.splitlines()), "lines written")
PY
```

If the regex does not match, the program is passed by another mechanism — inspect `src` directly and extract by hand rather than guessing.

- [ ] **Step 3: Verify it parses and is unchanged**

```bash
python3 -m py_compile metrics/app.py && echo "compiles"
grep -c '\$' metrics/app.py
```

Expected: compiles; `$` count is 0.

- [ ] **Step 4: Replace the command with a fetch**

In `docker-compose.yml`, the `metrics` service `command:` becomes:

```yaml
    command:
      - sh
      - -c
      - >-
        wget -qO /app/app.py
        https://raw.githubusercontent.com/mauodias/arrstack/main/metrics/app.py
        && exec python3 /app/app.py
```

Add `working_dir: /app` to the service. Leave `environment`, `volumes`, `healthcheck`, `depends_on`, `mem_limit` and `restart` untouched.

- [ ] **Step 5: Validate compose**

```bash
python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml')); print('YAML ok')"
```

- [ ] **Step 6: Commit, push, deploy**

The container fetches from `main`, so the push must precede the deploy.

```bash
git add metrics/app.py docker-compose.yml
git commit -m "refactor(metrics): serve the collector from a repo file"
git push origin main
uv run deploy.py
```

- [ ] **Step 7: Verify the page is byte-identical**

```bash
until curl -sf -o /dev/null --max-time 5 http://arr-vps:8099/healthz; do sleep 10; done
curl -s "http://arr-vps:8099/?range=24h" -o /tmp/metrics-after.html
diff <(sed -E 's/rendered [^<]*//' /tmp/metrics-before.html) \
     <(sed -E 's/rendered [^<]*//' /tmp/metrics-after.html) && echo "IDENTICAL"
```

The `sed` strips the render timestamp, which legitimately differs. Chart data will also differ if new samples landed between captures; if the diff is only inside `<svg>` and `<script class="chart-data">` blocks, that is expected. Any difference in structure, CSS or JS means the extraction was lossy — stop and fix before continuing.

**This task is a hard gate.** Do not start Task 2 until the page is verified.

---

### Task 2: Rule loading and validation

**Files:**
- Create: `alerter/rules.py`
- Create: `config/alerts/rules.toml`
- Test: `tests/test_alerter_rules.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_rules(path) -> list[Rule]`, raising `RuleError` on invalid input. `Rule` is a dataclass with fields `metric: str`, `name: str`, `direction: str`, `of: str | None`, `warning: float | None`, `error: float | None`, `debounce: int`. Task 3 consumes `Rule`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_rules -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rules'`

- [ ] **Step 3: Implement**

```python
"""Threshold rules, loaded from TOML."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

DIRECTIONS = ("above", "below")
DEFAULT_DEBOUNCE = 2


class RuleError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    metric: str
    name: str
    direction: str
    of: str | None = None
    warning: float | None = None
    error: float | None = None
    debounce: int = DEFAULT_DEBOUNCE


def _one(raw, index):
    for field in ("metric", "name", "direction"):
        if not raw.get(field):
            raise RuleError(f"rule {index}: missing required field {field!r}")
    if raw["direction"] not in DIRECTIONS:
        raise RuleError(
            f"rule {index}: direction must be one of {DIRECTIONS}, got {raw['direction']!r}"
        )
    warning = raw.get("warning")
    error = raw.get("error")
    if warning is None and error is None:
        raise RuleError(f"rule {index}: needs at least one of warning or error")
    return Rule(
        metric=raw["metric"],
        name=raw["name"],
        direction=raw["direction"],
        of=raw.get("of"),
        warning=None if warning is None else float(warning),
        error=None if error is None else float(error),
        debounce=int(raw.get("debounce", DEFAULT_DEBOUNCE)),
    )


def load_rules(path):
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RuleError(f"rules file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"rules file is not valid TOML: {exc}") from exc
    return [_one(raw, i) for i, raw in enumerate(data.get("rule", []))]
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_alerter_rules -v`
Expected: 6 passed

- [ ] **Step 5: Write the starting rules file**

`config/alerts/rules.toml`:

```toml
# Thresholds for the alerter container. Metric names match the samples table
# written by metrics/app.py. `of` divides one metric by another to give a
# percentage, because the collector stores absolute values.

[[rule]]
metric    = "vps_disk.used_bytes"
of        = "vps_disk.total_bytes"
name      = "VPS disk"
direction = "above"
warning   = 80
error     = 90

[[rule]]
metric    = "hetzner_box.used_bytes"
of        = "hetzner_box.total_bytes"
name      = "Storage Box"
direction = "above"
warning   = 80
error     = 92

[[rule]]
metric    = "vps_mem.used_bytes"
of        = "vps_mem.total_bytes"
name      = "VPS memory"
direction = "above"
warning   = 85
error     = 94

[[rule]]
metric    = "vps_swap.used_bytes"
of        = "vps_swap.total_bytes"
name      = "VPS swap"
direction = "above"
warning   = 50
error     = 80

[[rule]]
metric    = "slskd.connected"
name      = "Soulseek connection"
direction = "below"
error     = 1
debounce  = 3

[[rule]]
metric    = "slskd.downloads.success_rate"
name      = "Soulseek success rate"
direction = "below"
warning   = 75
error     = 60
```

- [ ] **Step 6: Verify the real file loads**

```bash
python3 -c "
import sys; sys.path.insert(0, 'alerter')
from rules import load_rules
for r in load_rules('config/alerts/rules.toml'): print(' ', r.name, r.direction, r.warning, r.error)
"
```

- [ ] **Step 7: Commit**

```bash
git add alerter/rules.py config/alerts/rules.toml tests/test_alerter_rules.py
git commit -m "feat(alerter): load threshold rules from TOML"
```

---

### Task 3: Classify a value into a state

**Files:**
- Create: `alerter/evaluate.py`
- Test: `tests/test_alerter_evaluate.py`

**Interfaces:**
- Consumes: `Rule` from Task 2.
- Produces: `classify(rule, value) -> str` returning `"healthy"`, `"warning"` or `"error"`; `resolve_value(rule, samples) -> float | None` where `samples` is a `dict[str, float]` of the latest value per metric; the constants `HEALTHY`, `WARNING`, `ERROR`. Task 4 consumes all of these.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_evaluate -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evaluate'`

- [ ] **Step 3: Implement**

```python
"""Turning a sample into a state."""

HEALTHY = "healthy"
WARNING = "warning"
ERROR = "error"


def resolve_value(rule, samples):
    """The comparable number for a rule, or None when it cannot be computed.

    A rule with `of` divides one metric by another: the collector stores
    absolute bytes, while thresholds are written as percentages.
    """
    value = samples.get(rule.metric)
    if value is None:
        return None
    if rule.of is None:
        return float(value)
    total = samples.get(rule.of)
    if total is None or float(total) == 0.0:
        return None
    return 100.0 * float(value) / float(total)


def _breached(direction, value, threshold):
    if threshold is None:
        return False
    return value >= threshold if direction == "above" else value <= threshold


def classify(rule, value):
    if _breached(rule.direction, value, rule.error):
        return ERROR
    if _breached(rule.direction, value, rule.warning):
        return WARNING
    return HEALTHY
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_alerter_evaluate -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add alerter/evaluate.py tests/test_alerter_evaluate.py
git commit -m "feat(alerter): classify metric values into three states"
```

---

### Task 4: Debounce and transition persistence

**Files:**
- Create: `alerter/state.py`
- Test: `tests/test_alerter_state.py`

**Interfaces:**
- Consumes: `HEALTHY`, `WARNING`, `ERROR` from Task 3.
- Produces: `Store(path)` with methods `current(rule_name) -> str | None`, `observe(rule_name, state, debounce) -> str | None` returning the new state when a transition is confirmed and `None` otherwise, `record(ts, rule_name, from_state, to_state, value, title, body)`, `open_alerts() -> list[dict]`, and `transitions_since(ts) -> list[dict]`. Tasks 6, 7 and 8 consume these.

- [ ] **Step 1: Write the failing test**

```python
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alerter"))
from state import Store
from evaluate import HEALTHY, WARNING, ERROR


def store():
    return Store(Path(tempfile.mkdtemp()) / "alerts.db")


class TestDebounce(unittest.TestCase):
    def test_first_observation_seeds_without_transition(self):
        s = store()
        self.assertIsNone(s.observe("disk", ERROR, debounce=2))
        self.assertEqual(s.current("disk"), ERROR)

    def test_change_needs_consecutive_observations(self):
        s = store()
        s.observe("disk", HEALTHY, debounce=2)
        self.assertIsNone(s.observe("disk", ERROR, debounce=2))
        self.assertEqual(s.observe("disk", ERROR, debounce=2), ERROR)
        self.assertEqual(s.current("disk"), ERROR)

    def test_flapping_never_transitions(self):
        s = store()
        s.observe("disk", HEALTHY, debounce=2)
        for _ in range(6):
            self.assertIsNone(s.observe("disk", ERROR, debounce=2))
            self.assertIsNone(s.observe("disk", HEALTHY, debounce=2))
        self.assertEqual(s.current("disk"), HEALTHY)

    def test_debounce_of_one_transitions_immediately(self):
        s = store()
        s.observe("disk", HEALTHY, debounce=1)
        self.assertEqual(s.observe("disk", ERROR, debounce=1), ERROR)

    def test_state_survives_reopening(self):
        path = Path(tempfile.mkdtemp()) / "alerts.db"
        Store(path).observe("disk", ERROR, debounce=2)
        self.assertEqual(Store(path).current("disk"), ERROR)


class TestRecord(unittest.TestCase):
    def test_open_alerts_excludes_recovered(self):
        s = store()
        s.record(100, "disk", HEALTHY, ERROR, 95.0, "t1", "b1")
        s.record(200, "swap", HEALTHY, WARNING, 60.0, "t2", "b2")
        s.record(300, "disk", ERROR, HEALTHY, 40.0, "t3", "b3")
        names = [a["rule"] for a in s.open_alerts()]
        self.assertEqual(names, ["swap"])

    def test_open_alert_carries_title_and_body(self):
        s = store()
        s.record(100, "disk", HEALTHY, ERROR, 95.0, "Disk 95%", "details here")
        alert = s.open_alerts()[0]
        self.assertEqual(alert["title"], "Disk 95%")
        self.assertEqual(alert["body"], "details here")
        self.assertEqual(alert["state"], ERROR)

    def test_transitions_since_filters_by_time(self):
        s = store()
        s.record(100, "disk", HEALTHY, ERROR, 95.0, "t", "b")
        s.record(300, "swap", HEALTHY, WARNING, 60.0, "t", "b")
        self.assertEqual(len(s.transitions_since(200)), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_state -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'state'`

- [ ] **Step 3: Implement**

```python
"""Confirmed state per rule, and the log of transitions."""
import sqlite3
from evaluate import HEALTHY

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_state (
    rule TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    pending TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transitions (
    ts INTEGER NOT NULL,
    rule TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    value REAL,
    title TEXT,
    body TEXT
);
CREATE INDEX IF NOT EXISTS transitions_ts ON transitions (ts);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def current(self, rule_name):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM rule_state WHERE rule = ?", (rule_name,)
            ).fetchone()
        return row["state"] if row else None

    def observe(self, rule_name, state, debounce):
        """Record one observation; return the new state only when it sticks.

        A change must be seen `debounce` times in a row, so a value sitting on
        its threshold cannot alternate between notifications.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, pending, pending_count FROM rule_state WHERE rule = ?",
                (rule_name,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO rule_state (rule, state, pending, pending_count)"
                    " VALUES (?, ?, NULL, 0)",
                    (rule_name, state),
                )
                return None
            if state == row["state"]:
                conn.execute(
                    "UPDATE rule_state SET pending = NULL, pending_count = 0"
                    " WHERE rule = ?",
                    (rule_name,),
                )
                return None
            count = row["pending_count"] + 1 if state == row["pending"] else 1
            if count >= debounce:
                conn.execute(
                    "UPDATE rule_state SET state = ?, pending = NULL, pending_count = 0"
                    " WHERE rule = ?",
                    (state, rule_name),
                )
                return state
            conn.execute(
                "UPDATE rule_state SET pending = ?, pending_count = ? WHERE rule = ?",
                (state, count, rule_name),
            )
            return None

    def record(self, ts, rule_name, from_state, to_state, value, title, body):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO transitions"
                " (ts, rule, from_state, to_state, value, title, body)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, rule_name, from_state, to_state, value, title, body),
            )

    def open_alerts(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.* FROM transitions t"
                " JOIN (SELECT rule, MAX(ts) AS ts FROM transitions GROUP BY rule) last"
                "   ON t.rule = last.rule AND t.ts = last.ts"
                " WHERE t.to_state != ?"
                " ORDER BY t.ts DESC",
                (HEALTHY,),
            ).fetchall()
        return [
            {
                "ts": r["ts"],
                "rule": r["rule"],
                "state": r["to_state"],
                "value": r["value"],
                "title": r["title"],
                "body": r["body"],
            }
            for r in rows
        ]

    def transitions_since(self, ts):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transitions WHERE ts >= ? ORDER BY ts", (ts,)
            ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_alerter_state -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add alerter/state.py tests/test_alerter_state.py
git commit -m "feat(alerter): debounce state changes and log transitions"
```

---

### Task 5: ntfy delivery

**Files:**
- Create: `alerter/notify.py`
- Test: `tests/test_alerter_notify.py`

**Interfaces:**
- Consumes: `HEALTHY`, `WARNING`, `ERROR` from Task 3.
- Produces: `format_alert(rule, from_state, to_state, value) -> (title, body, tags)`; `send_ntfy(server, topic, title, body, tags) -> bool`. Task 6 consumes both.

**Critical:** HTTP headers are latin-1, so an emoji in the `Title` header raises
`UnicodeEncodeError`. ntfy renders emoji from the `Tags` header using shortcodes
(`green_circle`, `yellow_circle`, `red_circle`). Verified against the live topic.

- [ ] **Step 1: Write the failing test**

```python
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

    def test_unreachable_server_returns_false(self):
        self.assertFalse(
            send_ntfy("http://127.0.0.1:1", "t", "a", "b", "red_circle")
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_notify -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'notify'`

- [ ] **Step 3: Implement**

```python
"""Composing and pushing notifications."""
import urllib.error
import urllib.request

from evaluate import HEALTHY, WARNING, ERROR

TAGS = {HEALTHY: "green_circle", WARNING: "yellow_circle", ERROR: "red_circle"}
HEADLINE = {
    HEALTHY: "recovered",
    WARNING: "warning",
    ERROR: "error",
}


def format_alert(rule, from_state, to_state, value):
    title = "%s %s" % (rule.name, HEADLINE[to_state])
    shown = "unknown" if value is None else ("%.1f" % value)
    body = "%s moved from %s to %s.\nCurrent value: %s" % (
        rule.name,
        from_state,
        to_state,
        shown,
    )
    return title, body, TAGS[to_state]


def send_ntfy(server, topic, title, body, tags):
    """Push one notification. Returns False rather than raising: a failed
    notification must not interrupt evaluation of the remaining rules."""
    url = "%s/%s" % (server.rstrip("/"), topic)
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20):
            return True
    except (urllib.error.URLError, OSError):
        return False
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_alerter_notify -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add alerter/notify.py tests/test_alerter_notify.py
git commit -m "feat(alerter): format and push ntfy notifications"
```

---

### Task 6: The evaluation loop and HTTP surface

**Files:**
- Create: `alerter/app.py`
- Test: `tests/test_alerter_app.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5, and the metrics `samples` table from Task 1.
- Produces: `latest_samples(db_path, horizon_seconds) -> dict[str, float]`; `newest_ts(db_path) -> int | None`; `evaluate_once(rules, samples, store, send) -> int` returning the number of notifications sent; `render_alerts_page(alerts) -> str`. Tasks 7 and 8 extend this module's HTTP routing.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_app -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: Implement the data and evaluation half**

```python
"""Entry point: evaluation loop plus a small HTTP surface."""
import html
import os
import sqlite3
import sys
import time
import traceback

from evaluate import ERROR, HEALTHY, classify, resolve_value
from notify import format_alert
from state import Store

STALE_RULE_NAME = "Metrics collection"


def log(message):
    sys.stdout.write("[alerter] " + message + "\n")
    sys.stdout.flush()


def latest_samples(db_path, horizon_seconds):
    """Newest value per metric within the horizon, or {} if unreadable.

    Returning empty rather than raising keeps the loop alive when metrics is
    down, which is exactly when the staleness rule needs to fire.
    """
    cutoff = int(time.time()) - horizon_seconds
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT metric, value FROM samples WHERE ts >= ?"
            " AND ts = (SELECT MAX(ts) FROM samples s2 WHERE s2.metric = samples.metric)",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {metric: float(value) for metric, value in rows}


def newest_ts(db_path):
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT MAX(ts) FROM samples").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None or row[0] is None else int(row[0])


def evaluate_once(rules, samples, store, send):
    """One pass over every rule. Returns the number of notifications sent."""
    sent = 0
    now = int(time.time())
    for rule in rules:
        value = resolve_value(rule, samples)
        if value is None:
            continue
        state = classify(rule, value)
        previous = store.current(rule.name)
        confirmed = store.observe(rule.name, state, rule.debounce)
        if confirmed is None or previous is None:
            continue
        title, body, tags = format_alert(rule, previous, confirmed, value)
        store.record(now, rule.name, previous, confirmed, value, title, body)
        if send(title, body, tags):
            sent += 1
        else:
            log("notification failed for " + rule.name)
    return sent
```

- [ ] **Step 4: Implement the alerts page**

Append to `alerter/app.py`:

```python
PAGE_CSS = """
:root { --bg:#0f172a; --card:#16203a; --line:#2b3950; --fg:#e8eef8; --dim:#a7b4cb; }
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:14px/1.5 ui-sans-serif, system-ui, sans-serif; color-scheme:dark; }
h1 { font-size:15px; margin:0 0 12px; }
details { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:10px 12px; margin-bottom:8px; }
summary { cursor:pointer; font-weight:600; }
pre { white-space:pre-wrap; color:var(--dim); margin:8px 0 0; font-size:12.5px; }
.meta { color:var(--dim); font-size:12px; margin-top:6px; }
.quiet { color:var(--dim); }
"""


def render_alerts_page(alerts):
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Open alerts</title><style>", PAGE_CSS, "</style></head><body>",
        "<h1>Open alerts</h1>",
    ]
    if not alerts:
        parts.append("<p class=\"quiet\">No open alerts.</p>")
    for alert in alerts:
        when = time.strftime("%d %b %H:%M", time.localtime(alert["ts"]))
        value = "" if alert["value"] is None else (" &middot; %.1f" % alert["value"])
        parts.append(
            "<details><summary>%s</summary><pre>%s</pre>"
            "<p class=\"meta\">since %s%s</p></details>"
            % (
                html.escape(alert["title"] or alert["rule"]),
                html.escape(alert["body"] or ""),
                when,
                value,
            )
        )
    parts.append("</body></html>")
    return "".join(parts)
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_alerter_app -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add alerter/app.py tests/test_alerter_app.py
git commit -m "feat(alerter): evaluation loop and open-alerts page"
```

---

### Task 7: Digest data gathering

**Files:**
- Modify: `alerter/app.py` (add the HTTP server and wiring)
- Create: `alerter/digest.py`
- Test: `tests/test_alerter_digest.py`

**Interfaces:**
- Consumes: `latest_samples` and `newest_ts` from Task 6; `Store.transitions_since` from Task 4.
- Produces: `series_at(db_path, metric, ts) -> float | None`; `gather(db_path, store, now, fetchers) -> dict` where `fetchers` is a `dict[str, callable]` each returning a list of item names, so tests inject stubs instead of live APIs. Task 8 consumes `gather`'s return value.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_digest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'digest'`

- [ ] **Step 3: Implement**

```python
"""Assembling the daily digest's data."""
import sqlite3
import time

DAY = 86400

CAPACITY_METRICS = (
    ("box_used_bytes", "hetzner_box.used_bytes"),
    ("box_total_bytes", "hetzner_box.total_bytes"),
    ("seeding_bytes", "qbt.seeding_bytes"),
    ("vps_disk_used_bytes", "vps_disk.used_bytes"),
)


def series_at(db_path, metric, ts):
    """The most recent sample at or before ts, or None."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM samples WHERE metric = ? AND ts <= ?"
            " ORDER BY ts DESC LIMIT 1",
            (metric, ts),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else float(row[0])


def _delta(db_path, metric, now):
    before = series_at(db_path, metric, now - DAY)
    after = series_at(db_path, metric, now)
    if before is None or after is None:
        return None
    return after - before


def gather(db_path, store, now, fetchers):
    """Everything the digest needs.

    `fetchers` maps a section name to a zero-argument callable returning a list
    of item names. Each is wrapped: one application being unreachable costs its
    own section and nothing else.
    """
    arrived = {}
    for name, fetch in fetchers.items():
        try:
            arrived[name] = list(fetch())
        except Exception:
            arrived[name] = []
    capacity = {key: series_at(db_path, metric, now) for key, metric in CAPACITY_METRICS}
    capacity["box_delta_bytes"] = _delta(db_path, "hetzner_box.used_bytes", now)
    capacity["seeding_delta_bytes"] = _delta(db_path, "qbt.seeding_bytes", now)
    return {
        "now": now,
        "arrived": arrived,
        "quiet": not any(arrived.values()),
        "capacity": capacity,
        "health": {"transitions": store.transitions_since(now - DAY),
                   "open": store.open_alerts()},
    }
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest tests.test_alerter_digest -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add alerter/digest.py tests/test_alerter_digest.py
git commit -m "feat(alerter): gather daily digest data"
```

---

### Task 8: Digest rendering, Resend delivery, and the HTTP server

**Files:**
- Modify: `alerter/digest.py` (add `render`)
- Modify: `alerter/notify.py` (add `send_email`)
- Modify: `alerter/app.py` (HTTP server, scheduler, `main`)
- Test: `tests/test_alerter_render.py`

**Interfaces:**
- Consumes: `gather` from Task 7, `Store` from Task 4.
- Produces: `render(data) -> (subject, html)`; `send_email(api_key, sender, recipient, subject, html) -> bool`; a running HTTP server exposing `/healthz`, `/alerts` and `/digest/preview`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest tests.test_alerter_render -v`
Expected: FAIL — `ImportError: cannot import name 'render'`

- [ ] **Step 3: Add `render` to `alerter/digest.py`**

```python
def _gb(value):
    return "—" if value is None else ("%.1f GB" % (value / 1e9))


def _signed_gb(value):
    if value is None:
        return "—"
    return "%s%.1f GB" % ("+" if value >= 0 else "", value / 1e9)


def render(data):
    """Subject and HTML body. Always renders every data section; a quiet day
    changes the framing, not the content."""
    date = time.strftime("%d %b %Y", time.localtime(data["now"] or time.time()))
    subject = "arrstack daily digest — " + date
    rows = []
    if data["quiet"]:
        rows.append("<p>Quiet day today, see you tomorrow!</p>")
    else:
        for section, items in sorted(data["arrived"].items()):
            if not items:
                continue
            entries = "".join("<li>%s</li>" % item for item in items)
            rows.append("<h2>%s</h2><ul>%s</ul>" % (section.title(), entries))
    capacity = data["capacity"]
    rows.append(
        "<h2>Capacity</h2><ul>"
        "<li>Storage Box: %s of %s (%s in 24h)</li>"
        "<li>Seeding: %s (%s in 24h)</li>"
        "<li>VPS disk: %s</li>"
        "</ul>"
        % (
            _gb(capacity.get("box_used_bytes")),
            _gb(capacity.get("box_total_bytes")),
            _signed_gb(capacity.get("box_delta_bytes")),
            _gb(capacity.get("seeding_bytes")),
            _signed_gb(capacity.get("seeding_delta_bytes")),
            _gb(capacity.get("vps_disk_used_bytes")),
        )
    )
    transitions = data["health"]["transitions"]
    if transitions:
        entries = "".join(
            "<li>%s: %s &rarr; %s</li>" % (t["rule"], t["from_state"], t["to_state"])
            for t in transitions
        )
        rows.append("<h2>Health</h2><ul>%s</ul>" % entries)
    else:
        rows.append("<h2>Health</h2><p>No alerts in the last 24 hours.</p>")
    body = (
        "<html><body style=\"font-family:system-ui,sans-serif;max-width:640px\">"
        "<h1>arrstack — %s</h1>%s</body></html>" % (date, "".join(rows))
    )
    return subject, body
```

- [ ] **Step 4: Add `send_email` to `alerter/notify.py`**

```python
import json

RESEND_ENDPOINT = "https://api.resend.com/emails"


def send_email(api_key, sender, recipient, subject, body_html,
               endpoint=RESEND_ENDPOINT):
    """Send one email through Resend. Returns False rather than raising: a
    day's digest is not worth retrying into a flood."""
    payload = json.dumps(
        {"from": sender, "to": [recipient], "subject": subject, "html": body_html}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            return True
    except (urllib.error.URLError, OSError):
        return False
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m unittest tests.test_alerter_render -v`
Expected: 6 passed

- [ ] **Step 6: Add the HTTP server, scheduler and `main` to `alerter/app.py`**

```python
import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import digest
from notify import send_email, send_ntfy
from rules import RuleError, load_rules

CONFIG = {
    "port": int(os.environ.get("ALERTER_PORT", "8100")),
    "interval": int(os.environ.get("ALERTER_INTERVAL", "300")),
    "metrics_db": os.environ.get("METRICS_DB", "/metrics/metrics.db"),
    "state_db": os.environ.get("ALERTER_DB", "/data/alerts.db"),
    "rules": os.environ.get("ALERTER_RULES", "/app/rules.toml"),
    "ntfy_server": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
    "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
    "resend_key": os.environ.get("RESEND_API_KEY", ""),
    "resend_from": os.environ.get("RESEND_FROM", ""),
    "resend_to": os.environ.get("RESEND_TO", ""),
    "digest_hour": int(os.environ.get("DIGEST_HOUR", "8")),
    "stale_after": int(os.environ.get("ALERTER_STALE_AFTER", "900")),
}

STORE = Store(CONFIG["state_db"])


def push(title, body, tags):
    if not CONFIG["ntfy_topic"]:
        log("no NTFY_TOPIC set; would have sent: " + title)
        return False
    return send_ntfy(CONFIG["ntfy_server"], CONFIG["ntfy_topic"], title, body, tags)


def fetchers():
    """Left empty until the arr history calls are added; gather() tolerates it
    and the digest simply reports a quiet day."""
    return {}


def build_digest():
    return digest.render(
        digest.gather(CONFIG["metrics_db"], STORE, int(time.time()), fetchers())
    )


def check_staleness():
    newest = newest_ts(CONFIG["metrics_db"])
    age = None if newest is None else int(time.time()) - newest
    stale = age is None or age > CONFIG["stale_after"]
    state = ERROR if stale else HEALTHY
    previous = STORE.current(STALE_RULE_NAME)
    confirmed = STORE.observe(STALE_RULE_NAME, state, 1)
    if confirmed is None or previous is None:
        return
    title = STALE_RULE_NAME + (" stalled" if stale else " recovered")
    tags = "red_circle" if stale else "green_circle"
    body = "No metrics sample for %s seconds." % age if stale else "Collection resumed."
    STORE.record(int(time.time()), STALE_RULE_NAME, previous, confirmed, age, title, body)
    push(title, body, tags)


def loop():
    while True:
        started = time.time()
        try:
            rules = load_rules(CONFIG["rules"])
            samples = latest_samples(CONFIG["metrics_db"], CONFIG["interval"] * 4)
            evaluate_once(rules, samples, STORE, push)
            check_staleness()
        except RuleError as exc:
            log("rules unusable: " + str(exc))
        except Exception:
            log("evaluation cycle raised:\n" + traceback.format_exc())
        delay = CONFIG["interval"] - (time.time() - started)
        time.sleep(max(5.0, delay))


def digest_loop():
    sent_on = None
    while True:
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if now.tm_hour == CONFIG["digest_hour"] and sent_on != today:
            sent_on = today
            try:
                subject, body = build_digest()
                if CONFIG["resend_key"]:
                    if not send_email(CONFIG["resend_key"], CONFIG["resend_from"],
                                      CONFIG["resend_to"], subject, body):
                        log("digest email failed")
                else:
                    log("no RESEND_API_KEY set; digest not sent")
            except Exception:
                log("digest raised:\n" + traceback.format_exc())
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/healthz":
                self._send(200, "ok", "text/plain; charset=utf-8")
            elif path == "/alerts":
                self._send(200, render_alerts_page(STORE.open_alerts()))
            elif path == "/digest/preview":
                self._send(200, build_digest()[1])
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception:
            log("request failed:\n" + traceback.format_exc())
            self._send(500, "error", "text/plain; charset=utf-8")

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=digest_loop, daemon=True).start()
    log("serving on port %d" % CONFIG["port"])
    ThreadingHTTPServer(("0.0.0.0", CONFIG["port"]), Handler).serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run the whole suite**

Run: `python3 -m unittest discover tests -v`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add alerter/ tests/
git commit -m "feat(alerter): render the digest, send it, and serve the HTTP surface"
```

---

### Task 9: Ship it

**Files:**
- Modify: `docker-compose.yml` (add the `alerter` service)
- Modify: `bootstrap/init.sh` (fetch `rules.toml`)
- Modify: `.gitignore` (allow `config/alerts/rules.toml`)
- Modify: `tests/bootstrap/test_init.sh` (fixture for the new file)
- Modify: `.env.example`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a running `alerter` container reachable at `http://arr-vps:8100/`.

- [ ] **Step 1: Allow the rules file past .gitignore**

`config/*` is ignored with per-file exceptions. Add, after the existing homepage exceptions:

```
!config/alerts/
config/alerts/*
!config/alerts/rules.toml
```

Verify: `git check-ignore -v config/alerts/rules.toml` must print nothing.

- [ ] **Step 2: Fetch it in bootstrap**

In `bootstrap/init.sh`, add `alerts` to `CONFIG_DIRS`, and after the Homepage fetch loop:

```sh
ALERTS_RULES_URL="${ALERTS_RULES_URL:-https://raw.githubusercontent.com/mauodias/arrstack/main/config/alerts/rules.toml}"
if ! wget -qO "$WORKSPACE/config/alerts/rules.toml" "$ALERTS_RULES_URL"; then
    echo "ERROR: failed to fetch rules.toml from $ALERTS_RULES_URL" >&2
    exit 1
fi
echo "Fetched config/alerts/rules.toml"
```

- [ ] **Step 3: Add the test fixture**

In `tests/bootstrap/test_init.sh`, beside the existing fixture writes:

```sh
printf -- '[[rule]]\nmetric = "m"\nname = "n"\ndirection = "above"\nerror = 1\n' > "$FIXTURE_DIR/rules.toml"
```

and extend the fake `wget` shim's case statement to serve `$FAKE_ALERTS_URL` from that fixture, mirroring how `$FAKE_SOULARR_URL` is handled.

- [ ] **Step 4: Run the bootstrap test**

Run: `sh tests/bootstrap/test_init.sh`
Expected: all bootstrap tests passed

- [ ] **Step 5: Add the service**

```yaml
  alerter:
    image: python:3.12-alpine
    container_name: arr-alerter
    network_mode: service:tailscale
    working_dir: /app
    environment:
      - TZ=${TZ}
      - ALERTER_PORT=8100
      - ALERTER_INTERVAL=300
      - METRICS_DB=/metrics/metrics.db
      - ALERTER_DB=/data/alerts.db
      - ALERTER_RULES=/rules/rules.toml
      - NTFY_SERVER=${NTFY_SERVER}
      - NTFY_TOPIC=${NTFY_TOPIC}
      - RESEND_API_KEY=${RESEND_API_KEY}
      - RESEND_FROM=${RESEND_FROM}
      - RESEND_TO=${RESEND_TO}
      - DIGEST_HOUR=${DIGEST_HOUR}
    volumes:
      - ./data/metrics:/metrics:ro
      - ./data/alerter:/data
      - ./config/alerts:/rules:ro
    command:
      - sh
      - -c
      - >-
        wget -qO /app/app.py https://raw.githubusercontent.com/mauodias/arrstack/main/alerter/app.py
        && wget -qO /app/rules.py https://raw.githubusercontent.com/mauodias/arrstack/main/alerter/rules.py
        && wget -qO /app/evaluate.py https://raw.githubusercontent.com/mauodias/arrstack/main/alerter/evaluate.py
        && wget -qO /app/state.py https://raw.githubusercontent.com/mauodias/arrstack/main/alerter/state.py
        && wget -qO /app/notify.py https://raw.githubusercontent.com/mauodias/arrstack/main/alerter/notify.py
        && wget -qO /app/digest.py https://raw.githubusercontent.com/mauodias/arrstack/main/alerter/digest.py
        && exec python3 /app/app.py
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=5).read()"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 20s
    depends_on:
      tailscale:
        condition: service_started
      metrics:
        condition: service_started
    mem_limit: 64m
    restart: unless-stopped
```

Each `wget` is written out rather than looped because Compose interpolates `$` in `command:` strings.

- [ ] **Step 6: Add the new variables to `.env.example`**

```
# --- Alerting (ntfy) ---
# Public ntfy.sh topics are readable AND writable by anyone who knows the name,
# so use a long random one.
NTFY_SERVER=https://ntfy.sh
NTFY_TOPIC=

# --- Daily digest (Resend) ---
RESEND_API_KEY=
RESEND_FROM=
RESEND_TO=
DIGEST_HOUR=8
```

- [ ] **Step 7: Validate and commit**

```bash
python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml')); print('YAML ok')"
sh -n bootstrap/init.sh && echo "shell ok"
python3 -m unittest discover tests -v
sh tests/bootstrap/test_init.sh
git add -A
git commit -m "feat(alerter): add the alerter service"
```

- [ ] **Step 8: Deploy and verify**

The user must fill in `NTFY_TOPIC`, `RESEND_API_KEY`, `RESEND_FROM` and `RESEND_TO` in `.env` before this step. Ask them; do not invent values.

```bash
git push origin main
uv run deploy.py
until curl -sf -o /dev/null --max-time 5 http://arr-vps:8100/healthz; do sleep 10; done
curl -s http://arr-vps:8100/alerts | head -20
curl -s http://arr-vps:8100/digest/preview | head -40
```

Expected: `/alerts` reports no open alerts on the first run, because the first cycle seeds state silently. `/digest/preview` renders a capacity section with real numbers.

- [ ] **Step 9: Confirm the first cycle is silent**

Watch for two evaluation intervals and confirm no ntfy notification arrives. If one does, the seeding logic in `evaluate_once` is wrong — a fresh deployment must not fire every rule at once.

---

## Deferred

Not in this plan, and deliberately:

- **The arr history fetchers.** `fetchers()` returns `{}`, so the digest reports a quiet day every day until they are added. The seam is defined and tested with stubs; wiring Lidarr, Sonarr, Radarr and Bazarr history is a follow-up once the delivery path is proven end to end.
- **The torrent snapshot diff** for "what left". Needs a daily snapshot table; add it with the fetchers.
- **Contributed and backlog sections.** Same reason.

Shipping the alert path first means the noisiest, most valuable half is live and proven before the digest grows.
