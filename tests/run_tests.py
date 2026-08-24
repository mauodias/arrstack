#!/usr/bin/env python3
"""Minimal runner for the pytest-style function tests in this directory.

unittest only collects TestCase classes, so `unittest discover` silently runs
none of these and still reports OK. pytest is not guaranteed to be present on
every machine that touches this repo. This runs them with neither.
"""
import importlib.util
import os
import sys
import traceback


def load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _under_test_modules():
    """Modules loaded from config/scripts -- the code under test."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "scripts"))
    out = []
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and os.path.abspath(f).startswith(root):
            out.append(mod)
    return out


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    targets = argv or [f for f in sorted(os.listdir(here))
                       if f.startswith("test_") and f.endswith(".py")]
    passed = failed = 0
    for fname in targets:
        path = fname if os.path.isabs(fname) else os.path.join(here, fname)
        mod = load(path)
        for name in sorted(vars(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn) or getattr(fn, "__module__", None) != mod.__name__:
                continue
            snaps = [(m, dict(vars(m))) for m in _under_test_modules()]
            try:
                fn()
                passed += 1
            except Exception:
                failed += 1
                print("FAIL %s::%s" % (os.path.basename(path), name))
                traceback.print_exc()
            finally:
                # Tests monkeypatch module globals; without this a patch from
                # one test silently changes the behaviour under another.
                for target, snap in snaps:
                    vars(target).clear()
                    vars(target).update(snap)
    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
