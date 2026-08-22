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

    environment = find_environment(client.list_environments(search=environment_name), environment_name)
    if environment is None:
        print(f"No Arcane environment named {environment_name!r}", file=sys.stderr)
        return 1
    environment_id = environment["id"]

    projects = client.list_projects(environment_id, search=project_name)
    existing = find_project(projects, project_name)
    payload = build_project_payload(project_name, compose_content, env_content)

    if existing is None:
        print(f"Creating project {project_name!r}...")
        project = client.create_project(environment_id, payload)
        project_id = project["id"]
        print("Deploying...")
        client.deploy_project(environment_id, project_id, redeploy=False)
    else:
        print(f"Updating project {project_name!r}...")
        project = client.update_project(environment_id, existing["id"], payload)
        project_id = project["id"]
        # Every container must be recreated fresh on every deploy: one left
        # running across an update keeps its bind mounts frozen at whatever
        # the host mount state was when it was created, which silently
        # breaks propagation-dependent mounts after a host config change
        # (see SPEC.md Section 3.1).
        print("Bringing down existing containers...")
        client.down_project(environment_id, project_id)
        print("Deploying...")
        client.deploy_project(environment_id, project_id, redeploy=False)

    final = client.get_project(environment_id, project_id)
    print(
        f"Status: {final.get('status')} | services: {final.get('serviceCount')} | "
        f"running: {final.get('runningCount')}"
    )
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    return run(repo_root, environ=dict(os.environ))
