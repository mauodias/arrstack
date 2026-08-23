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
