# *Arr Stack Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the repo (compose file, bootstrap init container, Arcane deploy script, host setup script, README) that deploys the *arr media stack described in `SPEC.md`, and roll it out to the live Strato VPS in verified stages rather than all at once.

**Architecture:** A single `docker-compose.yml` at the project root, pushed to a running Arcane instance via a thin `deploy.py` entrypoint backed by the `arcane_deploy/` package (stdlib-only Python, PEP 723, talking to Arcane's REST API) — split into `env.py` (`.env` parsing), `client.py` (the Arcane HTTP client), and `cli.py` (orchestration), each independently testable. An `alpine`-based `bootstrap` service creates/repairs the project's directory tree on every deploy. All other services build directly from `SPEC.md` Section 5, added to the compose file in dependency-ordered stages — each stage is committed, deployed, and manually verified against the real VPS before the next stage is added, per the operator's request for incremental validation (storage → connectivity → VPN → apps → consumers/dashboard).

**Tech Stack:** Docker Compose, Alpine/POSIX `sh`, Python 3.11+ stdlib only (no `requests`, no `pytest` — `unittest` and `urllib`), `uv` as the script runner.

**Spec:** `SPEC.md` (repo root)

## Global Constraints

- Every path inside the project is relative (`./config/...`, `./data/...`); the only host-absolute path is `/mnt/remote-media` (SPEC.md Section 3.2).
- No service uses Compose's `env_file:` directive; every service lists only the specific `${VAR}` names it needs (SPEC.md Section 4.3).
- No `rclone.conf` file — the Hetzner remote is configured via `RCLONE_CONFIG_HETZNER_BOX_*` env vars, and the password is run through `rclone obscure` before it ever goes into `.env` (SPEC.md Section 4.1).
- `.env` is the only file carrying secrets and is gitignored; `.env.example` is the committed template (SPEC.md Section 9.1).
- `deploy.py` performs push-and-apply only — no host-level pre-flight (kernel modules, `mount --make-rshared`); that's `setup-host.sh`, run manually over SSH exactly once (SPEC.md Section 11).
- Arcane API auth: header `X-API-Key: <token>` on every request; base path is `<ARCANE_URL>/api` (confirmed against the live OpenAPI spec at `https://arcane.mauricio.cc/api/openapi.json`).
- Deploy stages happen in this order and are verified against the real VPS before moving on: bootstrap → rclone-mount → tailscale → gluetun+qbittorrent → prowlarr/radarr/sonarr → seerr/lidarr/slskd/soularr → navidrome/jellyfin/homepage.

---

## Task 1: Repo Scaffolding

**Files:**
- Create: `.gitignore`
- Create: `.env.example`

**Interfaces:**
- Produces: `.env.example` — the full list of env var names every later task's compose service references via `${VAR}`.

- [ ] **Step 1: Create `.gitignore`**

```gitignore
.env
config/
data/
__pycache__/
*.pyc
```

- [ ] **Step 2: Create `.env.example`**

```env
# --- Core (Section 4.2) ---
PUID=1000
PGID=1000
TZ=Europe/Amsterdam
STORAGE_PATH=/mnt/remote-media

# --- Arcane API (Section 9.2) ---
ARCANE_URL=https://arcane.example.com/api
ARCANE_API_TOKEN=
ARCANE_ENVIRONMENT_NAME=
ARCANE_PROJECT_NAME=arr-stack

# --- Hetzner Storage Box / rclone (Section 4.1) ---
# Generate HETZNER_STORAGEBOX_PASS_OBSCURED with: rclone obscure '<password>'
HETZNER_STORAGEBOX_USER=
HETZNER_STORAGEBOX_PASS_OBSCURED=

# --- Tailscale ---
TS_AUTHKEY=

# --- AirVPN / Gluetun ---
VPN_SERVICE_PROVIDER=airvpn
VPN_TYPE=wireguard
WIREGUARD_PRIVATE_KEY=
WIREGUARD_PRESHARED_KEY=
WIREGUARD_ADDRESSES=
SERVER_COUNTRIES=Netherlands,Switzerland
AIRVPN_FORWARDED_PORT=

# --- Soulseek (slskd) ---
SLSKD_USERNAME=
SLSKD_PASSWORD=
SLSKD_API_KEY=

# --- Jellyfin ---
JELLYFIN_PUBLISHED_SERVER_URL=http://arr-vps:8096
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore .env.example
git commit -m "chore: scaffold repo with gitignore and env template"
```

---

## Task 2: Bootstrap Init Script

**Files:**
- Create: `bootstrap/init.sh`
- Test: `tests/bootstrap/test_init.sh`

**Interfaces:**
- Consumes: env vars `WORKSPACE` (project root, defaults `/workspace`), `MEDIA_ROOT` (defaults `/mnt/remote-media`), `PUID`, `PGID` (defaults `1000`); reads `$WORKSPACE/.env` for `HETZNER_STORAGEBOX_USER`/`HETZNER_STORAGEBOX_PASS_OBSCURED`.
- Produces: directory tree under `$WORKSPACE/config/*`, `$WORKSPACE/data/rclone-cache`, `$MEDIA_ROOT/{movies,tv,music,downloads}`, all owned `PUID:PGID`. Exit code 0 on success, non-zero (with a stderr message) if `.env` is missing or incomplete.

- [ ] **Step 1: Write the failing test**

```sh
#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INIT_SCRIPT="$SCRIPT_DIR/../../bootstrap/init.sh"

WORKDIR="$(mktemp -d)"
MEDIA_DIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR" "$MEDIA_DIR"' EXIT

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

REAL_UID="$(id -u)"
REAL_GID="$(id -g)"

echo "Test 1: fails without .env"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when .env is missing"
fi

echo "Test 2: fails with incomplete .env"
echo "HETZNER_STORAGEBOX_USER=someuser" > "$WORKDIR/.env"
if WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" sh "$INIT_SCRIPT" 2>/dev/null; then
    fail "expected non-zero exit when HETZNER_STORAGEBOX_PASS_OBSCURED is missing"
fi

echo "Test 3: succeeds and creates the directory tree with a complete .env"
cat > "$WORKDIR/.env" <<'EOF'
HETZNER_STORAGEBOX_USER=someuser
HETZNER_STORAGEBOX_PASS_OBSCURED=obscuredvalue
EOF
WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" sh "$INIT_SCRIPT" || fail "expected success with a complete .env"

[ -d "$WORKDIR/config/rclone" ] || fail "config/rclone not created"
[ -d "$WORKDIR/config/jellyfin" ] || fail "config/jellyfin not created"
[ -d "$WORKDIR/config/homepage" ] || fail "config/homepage not created"
[ -d "$WORKDIR/data/rclone-cache" ] || fail "data/rclone-cache not created"
[ -d "$MEDIA_DIR/movies" ] || fail "movies dir not created"
[ -d "$MEDIA_DIR/music" ] || fail "music dir not created"
[ -d "$MEDIA_DIR/downloads" ] || fail "downloads dir not created"

echo "Test 4: idempotent (second run also succeeds cleanly)"
WORKSPACE="$WORKDIR" MEDIA_ROOT="$MEDIA_DIR" PUID="$REAL_UID" PGID="$REAL_GID" sh "$INIT_SCRIPT" || fail "second run should also succeed"

echo "All bootstrap tests passed."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mkdir -p tests/bootstrap bootstrap && touch bootstrap/init.sh && chmod +x tests/bootstrap/test_init.sh && sh tests/bootstrap/test_init.sh`
Expected: FAIL on "Test 1" or an error, since `bootstrap/init.sh` doesn't exist/is empty yet.

- [ ] **Step 3: Write `bootstrap/init.sh`**

```sh
#!/bin/sh
set -eu

WORKSPACE="${WORKSPACE:-/workspace}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/remote-media}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

CONFIG_DIRS="rclone tailscale prowlarr radarr sonarr qbittorrent gluetun seerr lidarr slskd soularr navidrome jellyfin homepage"
MEDIA_DIRS="movies tv music downloads"

ENV_FILE="$WORKSPACE/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Populate it from .env.example first." >&2
    exit 1
fi

for key in HETZNER_STORAGEBOX_USER HETZNER_STORAGEBOX_PASS_OBSCURED; do
    if ! grep -q "^${key}=" "$ENV_FILE"; then
        echo "ERROR: $ENV_FILE is missing required key $key" >&2
        exit 1
    fi
done

for dir in $CONFIG_DIRS; do
    mkdir -p "$WORKSPACE/config/$dir"
done

mkdir -p "$WORKSPACE/data/rclone-cache"

for dir in $MEDIA_DIRS; do
    mkdir -p "$MEDIA_ROOT/$dir"
done

chown -R "${PUID}:${PGID}" "$WORKSPACE/config" "$WORKSPACE/data" "$MEDIA_ROOT"

echo "Bootstrap complete."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x bootstrap/init.sh && sh tests/bootstrap/test_init.sh`
Expected: `All bootstrap tests passed.`

- [ ] **Step 5: Commit**

```bash
git add bootstrap/init.sh tests/bootstrap/test_init.sh
git commit -m "feat: add idempotent bootstrap init script"
```

---

## Task 3: Arcane Deploy Script

A small local package (`arcane_deploy/`) plus a thin root-level
`deploy.py` entrypoint — split by responsibility instead of one giant
file, while still using zero external dependencies (stdlib only).

### Task 3a: `.env` Parsing

**Files:**
- Create: `arcane_deploy/__init__.py`
- Create: `arcane_deploy/env.py`
- Test: `tests/test_env.py`

**Interfaces:**
- Produces: `arcane_deploy.env.load_env_file(path: Path) -> dict[str, str]`

- [ ] **Step 1: Write the failing test**

```python
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
```

Save as `tests/test_env.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `mkdir -p arcane_deploy tests && touch arcane_deploy/__init__.py arcane_deploy/env.py && python3 -m unittest tests/test_env.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_env_file'`.

- [ ] **Step 3: Write `arcane_deploy/env.py`**

```python
"""Parsing for the project's local .env file."""
from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Ignores blank lines and comments."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values
```

`arcane_deploy/__init__.py` stays empty — it just marks the directory as
a package.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_env.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add arcane_deploy/__init__.py arcane_deploy/env.py tests/test_env.py
git commit -m "feat: add .env parsing for the deploy script"
```

### Task 3b: Arcane API Client

**Files:**
- Create: `arcane_deploy/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `arcane_deploy.client.find_environment(environments: list[dict], name: str) -> dict | None`, `arcane_deploy.client.find_project(projects: list[dict], name: str) -> dict | None`, `arcane_deploy.client.build_project_payload(name: str, compose_content: str, env_content: str) -> dict`, `arcane_deploy.client.ArcaneClient(base_url: str, api_key: str)` with methods `list_environments()`, `list_projects(environment_id)`, `create_project(environment_id, payload)`, `update_project(environment_id, project_id, payload)`, `deploy_project(environment_id, project_id, redeploy: bool)`, `get_project(environment_id, project_id)`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

Save as `tests/test_client.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `touch arcane_deploy/client.py && python3 -m unittest tests/test_client.py -v`
Expected: FAIL — `AttributeError: module 'arcane_deploy.client' has no attribute 'find_environment'`.

- [ ] **Step 3: Write `arcane_deploy/client.py`**

```python
"""HTTP client for the Arcane REST API."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def find_environment(environments: list[dict], name: str) -> dict | None:
    for env in environments:
        if env.get("name") == name:
            return env
    return None


def find_project(projects: list[dict], name: str) -> dict | None:
    for project in projects:
        if project.get("name") == name:
            return project
    return None


def build_project_payload(name: str, compose_content: str, env_content: str) -> dict:
    return {
        "name": name,
        "composeContent": compose_content,
        "envContent": env_content,
    }


class ArcaneClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8")
            raise RuntimeError(f"{method} {path} failed ({error.code}): {error_body}") from error

    def list_environments(self) -> list[dict]:
        return self._request("GET", "/environments")["data"] or []

    def list_projects(self, environment_id: str) -> list[dict]:
        return self._request("GET", f"/environments/{environment_id}/projects")["data"] or []

    def create_project(self, environment_id: str, payload: dict) -> dict:
        return self._request("POST", f"/environments/{environment_id}/projects", payload)["data"]

    def update_project(self, environment_id: str, project_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/environments/{environment_id}/projects/{project_id}", payload)["data"]

    def deploy_project(self, environment_id: str, project_id: str, redeploy: bool) -> dict:
        action = "redeploy" if redeploy else "up"
        return self._request("POST", f"/environments/{environment_id}/projects/{project_id}/{action}")["data"]

    def get_project(self, environment_id: str, project_id: str) -> dict:
        return self._request("GET", f"/environments/{environment_id}/projects/{project_id}")["data"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests/test_client.py -v`
Expected: 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add arcane_deploy/client.py tests/test_client.py
git commit -m "feat: add Arcane API client"
```

### Task 3c: CLI Entrypoint

**Files:**
- Create: `arcane_deploy/cli.py`
- Create: `deploy.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `arcane_deploy.env.load_env_file` (Task 3a); `arcane_deploy.client.ArcaneClient`, `find_environment`, `find_project`, `build_project_payload` (Task 3b).
- Produces: `arcane_deploy.cli.run(repo_root: Path, environ: dict[str, str]) -> int` — the testable core (takes an explicit `environ` dict instead of reading `os.environ` directly, so tests can inject one).

- [ ] **Step 1: Write the failing test**

```python
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
        mock_client.update_project.assert_called_once_with("env-1", "p1", {
            "name": "arr-stack",
            "composeContent": "services: {}",
            "envContent": "FOO=bar\n",
        })
        mock_client.deploy_project.assert_called_once_with("env-1", "p1", redeploy=True)


if __name__ == "__main__":
    unittest.main()
```

Save as `tests/test_cli.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `touch arcane_deploy/cli.py && python3 -m unittest tests/test_cli.py -v`
Expected: FAIL — `AttributeError: module 'arcane_deploy.cli' has no attribute 'run'`.

- [ ] **Step 3: Write `arcane_deploy/cli.py`**

```python
"""CLI entrypoint: push docker-compose.yml and .env to Arcane, then deploy."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from arcane_deploy.client import ArcaneClient, build_project_payload, find_environment, find_project
from arcane_deploy.env import load_env_file


def run(repo_root: Path, environ: dict[str, str]) -> int:
    compose_file = repo_root / "docker-compose.yml"
    env_file = repo_root / ".env"

    env = {**load_env_file(env_file), **environ}
    arcane_url = env.get("ARCANE_URL")
    api_key = env.get("ARCANE_API_TOKEN")
    environment_name = env.get("ARCANE_ENVIRONMENT_NAME")
    project_name = env.get("ARCANE_PROJECT_NAME", "arr-stack")

    missing = [
        key
        for key, value in {
            "ARCANE_URL": arcane_url,
            "ARCANE_API_TOKEN": api_key,
            "ARCANE_ENVIRONMENT_NAME": environment_name,
        }.items()
        if not value
    ]
    if missing:
        print(f"Missing required .env keys: {', '.join(missing)}", file=sys.stderr)
        return 1

    if not compose_file.exists():
        print(f"{compose_file} not found", file=sys.stderr)
        return 1

    compose_content = compose_file.read_text()
    env_content = env_file.read_text() if env_file.exists() else ""

    client = ArcaneClient(arcane_url, api_key)

    environment = find_environment(client.list_environments(), environment_name)
    if environment is None:
        print(f"No Arcane environment named {environment_name!r}", file=sys.stderr)
        return 1
    environment_id = environment["id"]

    projects = client.list_projects(environment_id)
    existing = find_project(projects, project_name)
    payload = build_project_payload(project_name, compose_content, env_content)

    if existing is None:
        print(f"Creating project {project_name!r}...")
        project = client.create_project(environment_id, payload)
        redeploy = False
    else:
        print(f"Updating project {project_name!r}...")
        project = client.update_project(environment_id, existing["id"], payload)
        redeploy = True

    project_id = project["id"]
    print("Deploying...")
    client.deploy_project(environment_id, project_id, redeploy=redeploy)

    final = client.get_project(environment_id, project_id)
    print(
        f"Status: {final.get('status')} | services: {final.get('serviceCount')} | "
        f"running: {final.get('runningCount')}"
    )
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    return run(repo_root, environ=dict(os.environ))
```

- [ ] **Step 4: Write the root `deploy.py` entrypoint**

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Thin entrypoint: uv run deploy.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arcane_deploy.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest tests/test_cli.py -v`
Expected: 4 tests PASS.

- [ ] **Step 6: Run the full test suite**

Run: `python3 -m unittest discover tests -v`
Expected: all tests across `test_env.py`, `test_client.py`, `test_cli.py`
(and `test_init.sh`'s shell-level checks, run separately per Task 2) PASS.

- [ ] **Step 7: Commit**

```bash
git add arcane_deploy/cli.py deploy.py tests/test_cli.py
git commit -m "feat: add deploy.py CLI entrypoint"
```

---

## Task 4: Stage 1 — Deploy Bootstrap Only

**Files:**
- Create: `docker-compose.yml`

**Interfaces:**
- Consumes: `bootstrap/init.sh` (Task 2), env vars `PUID`/`PGID` from `.env`.

- [ ] **Step 1: Create `docker-compose.yml` with only the bootstrap service**

```yaml
version: "3.8"

services:
  bootstrap:
    image: alpine:latest
    container_name: arr-bootstrap
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
    volumes:
      - .:/workspace
      - /mnt/remote-media:/mnt/remote-media
    command: ["sh", "/workspace/bootstrap/init.sh"]
    restart: "no"
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 1 compose - bootstrap only"
```

- [ ] **Step 3: Populate local `.env` from `.env.example`**

Fill in `PUID`, `PGID`, `TZ`, `ARCANE_URL`, `ARCANE_API_TOKEN`,
`ARCANE_ENVIRONMENT_NAME`, `HETZNER_STORAGEBOX_USER`, and
`HETZNER_STORAGEBOX_PASS_OBSCURED` (via `rclone obscure`). Leave
VPN/Soulseek/Tailscale vars blank for now — this stage doesn't need them.

- [ ] **Step 4: Deploy stage 1**

Run: `uv run deploy.py`
Expected: prints `Creating project 'arr-stack'...`, then `Deploying...`, then a
final `Status: ... | services: 1 | running: 0` line (bootstrap runs to
completion and exits, so `running` is expected to be 0 once it's done).

- [ ] **Step 5: Verify on the VPS (via Arcane UI or `docker ps -a` if you have
  console access)**

Confirm the `arr-bootstrap` container ran and exited 0, and that
`config/*` subdirectories and `/mnt/remote-media/{movies,tv,music,downloads}`
now exist in the project directory. Do not proceed to Task 5 until this
is confirmed.

---

## Task 5: Stage 2 — Add rclone-mount

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `HETZNER_STORAGEBOX_USER`, `HETZNER_STORAGEBOX_PASS_OBSCURED` from `.env`; `bootstrap` service's completion (Task 4).

- [ ] **Step 1: Append the `rclone-mount` service**

```yaml
  rclone-mount:
    image: rclone/rclone:latest
    container_name: arr-rclone
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse:/dev/fuse
    security_opt:
      - apparmor:unconfined
    environment:
      - RCLONE_CONFIG_HETZNER_BOX_TYPE=webdav
      - RCLONE_CONFIG_HETZNER_BOX_URL=https://${HETZNER_STORAGEBOX_USER}.your-storagebox.de
      - RCLONE_CONFIG_HETZNER_BOX_VENDOR=other
      - RCLONE_CONFIG_HETZNER_BOX_USER=${HETZNER_STORAGEBOX_USER}
      - RCLONE_CONFIG_HETZNER_BOX_PASS=${HETZNER_STORAGEBOX_PASS_OBSCURED}
    volumes:
      - ./data/rclone-cache:/cache
      - /mnt/remote-media:/data:shared
    depends_on:
      bootstrap:
        condition: service_completed_successfully
    command: >
      mount hetzner-box: /data
      --allow-other
      --cache-dir /cache
      --dir-cache-time 1000h
      --attr-timeout 1s
      --vfs-cache-mode full
      --vfs-cache-max-age 24h
      --vfs-cache-max-size 100G
      --vfs-read-chunk-size 64M
      --vfs-read-chunk-size-limit 1G
      --buffer-size 32M
      --umask 002
    healthcheck:
      test: ["CMD-SHELL", "ls /data > /dev/null || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
    restart: unless-stopped
```

Insert it under `services:`, after `bootstrap:`.

- [ ] **Step 2: Ensure the host mount-propagation pre-flight has been run**

Per SPEC.md Section 3.1/11.1, `mount --make-rshared /mnt` must already be
in place on the VPS host, and `/dev/fuse` must exist, or this service
will fail to mount. If `setup-host.sh` hasn't been run yet, run the
manual commands from SPEC.md Section 3.1 now (this is the one point
where SSH is unavoidable before storage will work).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 2 compose - add rclone-mount"
```

- [ ] **Step 4: Deploy and verify**

Run: `uv run deploy.py`
Expected: `Status: ... | services: 2 | running: 1` (bootstrap has already
exited; rclone-mount should be running and healthy).

Verify via Arcane or on the VPS: `docker exec arr-rclone rclone about hetzner-box:`
returns used/free totals (confirms the WebDAV credentials and connectivity
are correct), and `ls /mnt/remote-media` on the host shows the mounted
content. Do not proceed to Task 6 until this is confirmed.

---

## Task 6: Stage 3 — Add Tailscale

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `TS_AUTHKEY` from `.env`.

- [ ] **Step 1: Append the `tailscale` service**

```yaml
  tailscale:
    image: tailscale/tailscale:latest
    container_name: arr-tailscale
    hostname: arr-vps
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_STATE_DIR=/var/lib/tailscale
      - TS_USERSPACE=false
    volumes:
      - ./config/tailscale:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
      - SYS_MODULE
    depends_on:
      bootstrap:
        condition: service_completed_successfully
    restart: unless-stopped
```

Insert it under `services:`, after `rclone-mount:`.

- [ ] **Step 2: Populate `TS_AUTHKEY` in `.env`**

Generate a reusable/ephemeral auth key from the Tailscale admin console
and set `TS_AUTHKEY` in `.env`.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 3 compose - add tailscale"
```

- [ ] **Step 4: Deploy and verify**

Run: `uv run deploy.py`
Expected: `services: 3 | running: 2`.

Verify: the `arr-vps` node appears in your Tailscale admin console as
connected, and `docker exec arr-tailscale tailscale status` on the VPS
shows a healthy connection. Do not proceed to Task 7 until this is
confirmed.

---

## Task 7: Stage 4 — Add Gluetun + qBittorrent

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `VPN_SERVICE_PROVIDER`, `VPN_TYPE`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY`, `WIREGUARD_ADDRESSES`, `SERVER_COUNTRIES`, `AIRVPN_FORWARDED_PORT` from `.env` (gluetun); `PUID`/`PGID`/`TZ` (qbittorrent).

- [ ] **Step 1: Append the `gluetun` and `qbittorrent` services**

```yaml
  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: arr-gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    environment:
      - VPN_SERVICE_PROVIDER=${VPN_SERVICE_PROVIDER}
      - VPN_TYPE=${VPN_TYPE}
      - WIREGUARD_PRIVATE_KEY=${WIREGUARD_PRIVATE_KEY}
      - WIREGUARD_PRESHARED_KEY=${WIREGUARD_PRESHARED_KEY}
      - WIREGUARD_ADDRESSES=${WIREGUARD_ADDRESSES}
      - SERVER_COUNTRIES=${SERVER_COUNTRIES}
      - FIREWALL_VPN_INPUT_PORTS=${AIRVPN_FORWARDED_PORT}
      - FIREWALL_OUTBOUND_SUBNETS=100.64.0.0/10
    restart: unless-stopped

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: arr-qbittorrent
    network_mode: "service:gluetun"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - WEBUI_PORT=8080
    volumes:
      - ./config/qbittorrent:/config
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      gluetun:
        condition: service_started
      rclone-mount:
        condition: service_healthy
    restart: unless-stopped
```

Insert them under `services:`, after `tailscale:`.

- [ ] **Step 2: Populate AirVPN credentials in `.env`**

Set `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY`,
`WIREGUARD_ADDRESSES`, and `AIRVPN_FORWARDED_PORT` from your AirVPN
account's WireGuard config generator (with a forwarded port assigned).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 4 compose - add gluetun and qbittorrent"
```

- [ ] **Step 4: Deploy and verify (critical — IP leak test)**

Run: `uv run deploy.py`
Expected: `services: 5 | running: 4`.

Verify per SPEC.md Section 7.1:
```bash
docker exec -it arr-qbittorrent curl https://ifconfig.me
```
The returned IP **must** match an AirVPN exit IP, not the Strato VPS's
own IP. If it matches the VPS IP, stop — do not proceed to Task 8 until
this is fixed, since it means qBittorrent traffic is not actually
isolated.

---

## Task 8: Stage 5 — Add Prowlarr, Radarr, Sonarr

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Append the `prowlarr`, `radarr`, and `sonarr` services**

```yaml
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: arr-prowlarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/prowlarr:/config
    depends_on:
      tailscale:
        condition: service_started
    restart: unless-stopped

  radarr:
    image: lscr.io/linuxserver/radarr:latest
    container_name: arr-radarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/radarr:/config
      - /mnt/remote-media/movies:/movies:rslave
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    container_name: arr-sonarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/sonarr:/config
      - /mnt/remote-media/tv:/tv:rslave
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped
```

Insert them under `services:`, after `qbittorrent:`.

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 5 compose - add prowlarr, radarr, sonarr"
```

- [ ] **Step 3: Deploy and verify**

Run: `uv run deploy.py`
Expected: `services: 8 | running: 7`.

Verify: reach Prowlarr/Radarr/Sonarr's web UIs over Tailscale
(`http://arr-vps:9696`, `:7878`, `:8989`), add Prowlarr as an indexer
source in Radarr/Sonarr, and confirm qBittorrent is reachable from
Radarr/Sonarr as a download client at `http://arr-gluetun:8080` (SPEC.md
Section 7.3). Do not proceed to Task 9 until this is confirmed.

---

## Task 9: Stage 6 — Add Seerr, Lidarr, slskd, Soularr

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `SLSKD_USERNAME`, `SLSKD_PASSWORD`, `SLSKD_API_KEY` from `.env`.

- [ ] **Step 1: Append the `seerr`, `lidarr`, `slskd`, and `soularr` services**

```yaml
  seerr:
    image: fallenbagel/seerr:latest
    container_name: arr-seerr
    network_mode: "service:tailscale"
    environment:
      - TZ=${TZ}
    volumes:
      - ./config/seerr:/app/config
    depends_on:
      radarr:
        condition: service_started
      sonarr:
        condition: service_started
      tailscale:
        condition: service_started
    restart: unless-stopped

  lidarr:
    image: lscr.io/linuxserver/lidarr:latest
    container_name: arr-lidarr
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
    volumes:
      - ./config/lidarr:/config
      - /mnt/remote-media/music:/music:rslave
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  slskd:
    image: slskd/slskd:latest
    container_name: arr-slskd
    network_mode: "service:tailscale"
    environment:
      - TZ=${TZ}
      - SLSKD_USERNAME=${SLSKD_USERNAME}
      - SLSKD_PASSWORD=${SLSKD_PASSWORD}
      - SLSKD_API_KEY=${SLSKD_API_KEY}
    volumes:
      - ./config/slskd:/app
      - /mnt/remote-media/downloads:/downloads:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  soularr:
    image: mrusse08/soularr:latest
    container_name: arr-soularr
    network_mode: "service:tailscale"
    environment:
      - TZ=${TZ}
      - SCRIPT_INTERVAL=300
    volumes:
      - ./config/soularr:/data
    depends_on:
      lidarr:
        condition: service_started
      slskd:
        condition: service_started
      tailscale:
        condition: service_started
    restart: unless-stopped
```

Insert them under `services:`, after `sonarr:`.

- [ ] **Step 2: Populate Soulseek credentials in `.env`**

Set `SLSKD_USERNAME`/`SLSKD_PASSWORD` to your Soulseek account, and
generate `SLSKD_API_KEY` (any random string slskd will accept as its API
key) for soularr to authenticate with.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 6 compose - add seerr, lidarr, slskd, soularr"
```

- [ ] **Step 4: Deploy and verify**

Run: `uv run deploy.py`
Expected: `services: 12 | running: 11`.

Verify: Seerr's UI is reachable at `http://arr-vps:5055` and can see
Radarr/Sonarr as configured services; Lidarr's UI at `:8686` is
reachable and configured with Prowlarr as an indexer source; slskd's UI
at `:5030` shows a connected Soulseek session; soularr's logs
(`docker logs arr-soularr`) show it polling Lidarr's wanted list. Do not
proceed to Task 10 until this is confirmed.

---

## Task 10: Stage 7 — Add Navidrome, Jellyfin, Homepage

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `JELLYFIN_PUBLISHED_SERVER_URL` from `.env`.

- [ ] **Step 1: Append the `navidrome`, `jellyfin`, and `homepage` services**

```yaml
  navidrome:
    image: deluan/navidrome:latest
    container_name: arr-navidrome
    network_mode: "service:tailscale"
    environment:
      - ND_LOGLEVEL=info
      - TZ=${TZ}
    volumes:
      - ./config/navidrome:/data
      - /mnt/remote-media/music:/music:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  jellyfin:
    image: lscr.io/linuxserver/jellyfin:latest
    container_name: arr-jellyfin
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - JELLYFIN_PublishedServerUrl=${JELLYFIN_PUBLISHED_SERVER_URL}
    volumes:
      - ./config/jellyfin:/config
      - /mnt/remote-media/movies:/movies:rslave
      - /mnt/remote-media/tv:/tv:rslave
      - /mnt/remote-media/music:/music:rslave
    depends_on:
      rclone-mount:
        condition: service_healthy
      tailscale:
        condition: service_started
    restart: unless-stopped

  homepage:
    image: ghcr.io/gethomepage/homepage:latest
    container_name: arr-homepage
    network_mode: "service:tailscale"
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - HOMEPAGE_ALLOWED_HOSTS=arr-vps:3000
    volumes:
      - ./config/homepage:/app/config
      - /var/run/docker.sock:/var/run/docker.sock:ro
    depends_on:
      tailscale:
        condition: service_started
    restart: unless-stopped
```

Insert them under `services:`, after `soularr:`.

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: stage 7 compose - add navidrome, jellyfin, homepage"
```

- [ ] **Step 3: Deploy and verify**

Run: `uv run deploy.py`
Expected: `services: 15 | running: 14`.

Verify: Jellyfin's setup wizard is reachable at `http://arr-vps:8096`
and can see the `/movies`, `/tv`, `/music` libraries with content;
Navidrome at `:4533` shows the music library; Homepage at `:3000` loads
and its widgets resolve against the other services' APIs (per SPEC.md
Section 10.1 — this requires manually adding each app's API key to
`config/homepage/services.yaml` on the VPS, which is out of scope for
`deploy.py` per Section 9.3). At this point every service in SPEC.md
Section 5 is deployed and the full stack matches the spec.

---

## Task 11: Host Setup Script

**Files:**
- Create: `setup-host.sh`

**Interfaces:**
- Consumes: nothing (standalone script, run manually via `sudo bash setup-host.sh`).
- Produces: loaded `fuse`/`tun` kernel modules, `/mnt` mounted `rshared`, and a systemd unit making that persist across reboots — matching SPEC.md Section 3.1 and Section 11.1.

- [ ] **Step 1: Write `setup-host.sh`**

```sh
#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (sudo bash setup-host.sh)." >&2
    exit 1
fi

echo "Loading fuse and tun kernel modules..."
modprobe fuse
modprobe tun

echo "Making /mnt a shared mount point..."
mount --make-rshared /mnt

echo "Installing a systemd unit to persist mount propagation across reboots..."
cat > /etc/systemd/system/mnt-make-rshared.service <<'EOF'
[Unit]
Description=Make /mnt a shared mount point for Docker FUSE propagation
DefaultDependencies=no
After=local-fs.target
Before=docker.service

[Service]
Type=oneshot
ExecStart=/bin/mount --make-rshared /mnt
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mnt-make-rshared.service

echo "Host setup complete."
echo "Verify with: ls -la /dev/fuse /dev/net/tun"
```

- [ ] **Step 2: Commit**

```bash
git add setup-host.sh
git commit -m "feat: add one-time host setup script"
```

- [ ] **Step 3: Run it once on the VPS**

```bash
curl -fsSL https://raw.githubusercontent.com/<you>/arrstack/main/setup-host.sh -o setup-host.sh
less setup-host.sh
sudo bash setup-host.sh
```

Verify: `ls -la /dev/fuse /dev/net/tun` shows both devices, and
`findmnt -o TARGET,PROPAGATION /mnt` shows `shared`. This confirms the
one unavoidable SSH step (SPEC.md Section 11) is done and durable across
reboots. This closes out the staged rollout — the whole stack from
SPEC.md is now live and verified end-to-end.

---

## Task 12: README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: nothing (documentation only, written after every other task exists so it can describe the finished repo accurately).

- [ ] **Step 1: Write `README.md`**

```markdown
# arrstack

A self-hosted *arr media stack (Prowlarr, Radarr, Sonarr, Lidarr,
qBittorrent, slskd/soularr, Seerr, Navidrome, Jellyfin, Homepage) running
on a Strato VPS behind Tailscale, with AirVPN-isolated torrent egress and
Hetzner Storage Box-backed storage via an rclone FUSE mount. Deployed
GitOps-style to a running [Arcane](https://arcane.mauricio.cc) instance.

Full architecture, rationale, and every service's configuration:
[`SPEC.md`](./SPEC.md).

## Repo layout

- `docker-compose.yml` — the whole stack, staged into place per
  [`docs/superpowers/plans/2026-08-20-arr-stack-scaffolding.md`](./docs/superpowers/plans/2026-08-20-arr-stack-scaffolding.md)
- `.env.example` — template for the local `.env` (never committed — see
  below)
- `bootstrap/init.sh` — idempotent directory/permissions setup, run as a
  compose service on every deploy
- `arcane_deploy/` — the Arcane API client and CLI logic
- `deploy.py` — entrypoint: `uv run deploy.py`
- `setup-host.sh` — one-time host script (kernel modules, mount
  propagation) — the one step that requires SSH
- `tests/` — `unittest`-based tests for `arcane_deploy/` and a shell test
  for `bootstrap/init.sh`

## First-time setup

1. Copy `.env.example` to `.env` and fill in every value — Arcane API
   token, Hetzner Storage Box credentials (run `rclone obscure
   '<password>'` and paste the output, not the plaintext), Tailscale
   authkey, AirVPN WireGuard config, Soulseek credentials. `.env` is
   gitignored; it never enters version control.
2. SSH into the VPS once to run `setup-host.sh` (loads `fuse`/`tun`
   kernel modules, sets up shared mount propagation — see SPEC.md
   Section 11). This is the only step that needs SSH.
3. Run `uv run deploy.py` to push `docker-compose.yml` and `.env` to
   Arcane and deploy the stack.

## Running the tests

```bash
python3 -m unittest discover tests -v
sh tests/bootstrap/test_init.sh
```

## Deploying changes

Edit `docker-compose.yml` and/or `.env`, then run `uv run deploy.py`
again — it's idempotent and updates the existing Arcane project in
place.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README"
```
