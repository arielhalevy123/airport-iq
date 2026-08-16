"""Optional Langfuse tracing, wired at the boundary — never inside the engine.

WHY IT IS NOT IN THE SCORING PACKAGE
`tests/test_purity.py` walks the AST of `airportiq.scoring` and fails the build on any import
of a network library, `time`, or `datetime`. Langfuse needs all three. Instrumenting the engine
from the inside would therefore turn the project's strongest claim — that determinism is
mechanically enforced rather than promised — into a comment.

So the engine is traced from the OUTSIDE. The caller opens a span, calls the pure function, and
records what went in and what came out. You get exactly the observability you wanted: per-run
timings, the profile used, how many airports were scored, how many had missing inputs. The
engine stays a function of its arguments and does not know it is being watched.

That is also the more honest instrumentation. A span recorded by the caller measures the real
call; a span recorded inside the function measures the function's opinion of itself.

FAILURE POLICY: FAIL OPEN, deliberately
This is the opposite of the rule everywhere else in the project. Scoring guards fail closed —
a missing input becomes "unknown", never zero. Tracing fails OPEN: if Langfuse is missing,
misconfigured, or throws, every call here degrades to a no-op and the answer is unaffected.
Telemetry that can take down the thing it is observing is worse than no telemetry.

ZERO-DEPENDENCY IS PRESERVED
Langfuse is optional. Without it installed — and CI installs nothing — every span is a no-op
and the whole suite still passes. Enable it by installing langfuse and setting keys in .env:

    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...
    LANGFUSE_HOST=https://cloud.langfuse.com     # or your self-hosted URL
"""
from __future__ import annotations

import os
import pathlib
from contextlib import contextmanager

_client = None
_state = "uninitialised"          # uninitialised | off | on | error:<reason>


def _env() -> dict[str, str]:
    env = dict(os.environ)
    path = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def _init():
    """Resolve the client once. Any failure means tracing is off, not that the app is down."""
    global _client, _state
    if _state != "uninitialised":
        return _client

    env = _env()
    if not (env.get("LANGFUSE_PUBLIC_KEY") and env.get("LANGFUSE_SECRET_KEY")):
        _state = "off"                      # not configured: the normal case
        return None
    try:
        from langfuse import Langfuse       # noqa: PLC0415  (optional dependency)
        _client = Langfuse(
            public_key=env["LANGFUSE_PUBLIC_KEY"],
            secret_key=env["LANGFUSE_SECRET_KEY"],
            host=env.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        _state = "on"
    except ImportError:
        _state = "off"                      # keys set but package absent: still fine
    except Exception as e:                  # noqa: BLE001
        _state = f"error:{type(e).__name__}"
    return _client


def status() -> str:
    _init()
    return _state


@contextmanager
def span(name: str, **metadata):
    """Trace one operation. A no-op when tracing is unavailable.

    Yields a recorder: call `.output(...)` to attach the result. The yielded object is always
    usable, so callers never branch on whether tracing is on.
    """
    client = _init()

    class _Rec:
        def __init__(self):
            self._out = None
        def output(self, **kw):
            self._out = kw

    rec = _Rec()
    if client is None:
        yield rec
        return

    handle = None
    try:
        handle = client.start_span(name=name, input=metadata or None)
    except Exception:                        # noqa: BLE001
        yield rec
        return

    try:
        yield rec
    except Exception as e:                   # noqa: BLE001
        try:
            handle.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            handle.end()
        except Exception:                    # noqa: BLE001
            pass
        raise                                # the error is the caller's, not ours to swallow
    else:
        try:
            if rec._out is not None:
                handle.update(output=rec._out)
            handle.end()
        except Exception:                    # noqa: BLE001
            pass


def flush() -> None:
    """Push buffered events. Worth calling before a short-lived process exits."""
    client = _init()
    if client is None:
        return
    try:
        client.flush()
    except Exception:                        # noqa: BLE001
        pass
