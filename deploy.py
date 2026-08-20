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
