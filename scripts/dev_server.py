#!/usr/bin/env python3
"""Run the API server and restart it whenever the code changes.

    python3 scripts/dev_server.py [port]      # default 8000

Same zero-dependency rule as everything else: no watchdog, no uvicorn --reload. A one-second
mtime poll over src/ and scripts/ is plenty for a single developer, and it works everywhere.

The child is the REAL server (`python -m airportiq.api.server`), not a reimplementation, so
dev and production run identical code. The UI is served with Cache-Control: no-store, so a
plain browser refresh after the restart is enough to see the change — no stale-tab mystery.

If the server crashes on boot (a syntax error mid-edit), the watcher stays alive and waits
for the next file change instead of hot-looping a broken process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCH = (ROOT / "src", ROOT / "scripts")
POLL_SEC = 1.0


def _stamps() -> dict:
    return {p: p.stat().st_mtime for d in WATCH for p in d.rglob("*.py") if p.exists()}


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    while True:
        proc = subprocess.Popen(
            [sys.executable, "-m", "airportiq.api.server", port], env=env, cwd=ROOT)
        seen = _stamps()
        print(f"[dev] server pid {proc.pid} on port {port} — watching "
              f"{len(seen)} files for changes", file=sys.stderr)
        try:
            while True:
                time.sleep(POLL_SEC)
                if _stamps() != seen:
                    print("[dev] change detected — restarting", file=sys.stderr)
                    break
                if proc.poll() is not None:
                    print(f"[dev] server exited ({proc.returncode}) — waiting for a "
                          f"file change before retrying", file=sys.stderr)
                    while _stamps() == seen:
                        time.sleep(POLL_SEC)
                    break
        except KeyboardInterrupt:
            print("\n[dev] stopping", file=sys.stderr)
            proc.terminate()
            return 0
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
