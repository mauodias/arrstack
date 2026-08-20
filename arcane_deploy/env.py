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
