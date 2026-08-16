#!/usr/bin/env python3
"""Run every unit test. No dependencies, no test framework.

    python run_tests.py

Discovers tests/test_*.py, runs every test_* function, and reports pass/fail per test with a
non-zero exit code on failure so CI can gate on it.

Why not pytest: the repo's claim is that it runs on a clean clone with nothing installed, and
that claim should survive contact with its own test suite. A runner is about forty lines.

The EVALS are deliberately not run here. They call a live model, which costs money and needs a
key, so they cannot run in CI on every push. Run them manually with `python evals/run_evals.py`.
Conflating "unit tests pass" with "the model still behaves" would make a green build mean less
than it does.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    files = sorted((ROOT / "tests").glob("test_*.py"))
    if not files:
        print("no test files found")
        return 2

    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for path in files:
        print(f"\n{path.name}")
        try:
            mod = load(path)
        except Exception:
            failed += 1
            failures.append((path.name, traceback.format_exc()))
            print("  ERROR importing module")
            continue

        for name in sorted(n for n in dir(mod) if n.startswith("test_")):
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"  ok    {name}")
            except Exception:
                failed += 1
                failures.append((f"{path.name}::{name}", traceback.format_exc()))
                print(f"  FAIL  {name}")

    print(f"\n{'='*60}\n{passed} passed, {failed} failed")
    for name, tb in failures:
        print(f"\n--- {name}\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
