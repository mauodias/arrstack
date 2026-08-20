"""HTTP client for the Arcane REST API."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
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
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json", "User-Agent": "arcane-deploy/1.0"}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8")
            raise RuntimeError(f"{method} {path} failed ({error.code}): {error_body}") from error

    def list_environments(self, search: str | None = None) -> list[dict]:
        path = "/environments"
        if search is not None:
            path += f"?{urllib.parse.urlencode({'search': search})}"
        return self._request("GET", path)["data"] or []

    def list_projects(self, environment_id: str, search: str | None = None) -> list[dict]:
        path = f"/environments/{environment_id}/projects"
        if search is not None:
            path += f"?{urllib.parse.urlencode({'search': search})}"
        return self._request("GET", path)["data"] or []

    def create_project(self, environment_id: str, payload: dict) -> dict:
        return self._request("POST", f"/environments/{environment_id}/projects", payload)["data"]

    def update_project(self, environment_id: str, project_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/environments/{environment_id}/projects/{project_id}", payload)["data"]

    def deploy_project(self, environment_id: str, project_id: str, redeploy: bool) -> dict:
        action = "redeploy" if redeploy else "up"
        return self._request("POST", f"/environments/{environment_id}/projects/{project_id}/{action}")["data"]

    def get_project(self, environment_id: str, project_id: str) -> dict:
        return self._request("GET", f"/environments/{environment_id}/projects/{project_id}")["data"]
