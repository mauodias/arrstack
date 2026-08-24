"""Isolate the module under test between test functions.

Several tests monkeypatch library_seed globals (missing_videos, wait_complete)
to drive the procedure without a live qBittorrent. Without this, a patch from
one test silently changes the behaviour of the next -- which is exactly what
happened: the wait_complete tests passed alone and failed after the repoint
tests had run.

tests/run_tests.py performs the same snapshot, so both runners agree.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config", "scripts"))


@pytest.fixture(autouse=True)
def _isolate_module_under_test():
    import library_seed

    snapshot = dict(vars(library_seed))
    try:
        yield
    finally:
        vars(library_seed).clear()
        vars(library_seed).update(snapshot)
