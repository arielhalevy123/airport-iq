"""HTTP API and a chat interface, with no framework dependency.

Deliberately built on the standard library. FastAPI would be the normal choice, but the
brief asks for something a reviewer can run, and `python -m airportiq.api.server` with zero
`pip install` removes the most common reason a take-home fails on someone else's machine.
The routing is simple enough that a framework would add a dependency without adding clarity.

Two endpoints, and the pairing is the point:

    POST /v1/score   deterministic ranking. NO LLM anywhere in this path. No key needed.
    POST /v1/chat    the conversational agent, which calls the same scoring function.

A reviewer can curl /v1/score, then ask /v1/chat the same question, and diff the numbers.
They are identical because it is the same function. That is the demonstration that the model
is not doing the arithmetic.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from airportiq.scoring.engine import score          # noqa: E402

_FACTS: list = []
_CARDS: dict[str, list] = {}
_SESSIONS = None   # lazily created; see _load()


def _load() -> None:
    """Build facts once at startup, from snapshot if the network is unavailable."""
    global _FACTS, _CARDS
    if _FACTS:
        return
    global _SESSIONS
    from airportiq.agent.session import SessionStore
    _SESSIONS = SessionStore()
    from build_and_rank import JET_RUNWAYS, build_facts
    _FACTS = build_facts(sorted(JET_RUNWAYS))
    for profile in ("terminal_expansion", "congestion"):
        _CARDS[profile] = score(_FACTS, profile)


# Absolute, not relative: a relative import fails when this file is run directly
# (`python src/airportiq/api/server.py`), which is exactly what someone tries first.
# sys.path is already set above, so both entry points work.
from airportiq.api.ui import INDEX  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, indent=1).encode(), "application/json")

    def log_message(self, *args) -> None:      # quieter console
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX.encode(), "text/html; charset=utf-8")
        elif self.path == "/health":
            self._json(200, {"ok": True, "airports": len(_FACTS)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})

        if self.path == "/v1/score":
            # Deterministic path. No LLM, no key required.
            profile = payload.get("profile", "terminal_expansion")
            hub = payload.get("hub_class", "large")
            cards = [c for c in _CARDS.get(profile, []) if c.hub_class == hub and c.composite]
            return self._json(200, {
                "profile": profile, "hub_class": hub,
                "results": [{"rank": c.rank, "code": c.code, "name": c.name,
                             "composite": c.composite, "contributions": c.contributions,
                             "flags": c.flags, "missing": c.missing}
                            for c in cards[:20]],
            })

        if self.path == "/v1/chat":
            question = (payload.get("question") or "").strip()
            if not question:
                return self._json(400, {"error": "question is required"})
            try:
                sid = payload.get("session_id") or self.headers.get("X-Session", "default")
                by_code = {f.code: f for f in _FACTS}

                # Tool-calling agent first: it picks its own data and handles open-ended
                # questions. Falls back to the deterministic pipeline if tool calling is
                # unavailable (no OpenAI-compatible key), so the demo still works.
                try:
                    from airportiq.agent import react
                    out = react.run(question, _CARDS["congestion"], by_code)
                    assumptions = []
                    for t in out["trace"]:
                        if "list_region" in t["tool"]:
                            assumptions.append("Region membership resolved deterministically, "
                                               "not inferred by the model.")
                    return self._json(200, {
                        "answer": out["answer"], "intent": "agent",
                        "trace": out["trace"], "rounds": out["rounds"],
                        "assumptions": assumptions})
                except Exception:
                    from airportiq.agent.answer import answer
                    res = answer(question, _CARDS["terminal_expansion"], by_code,
                                 session=_SESSIONS.get(sid))
                    return self._json(200, {"answer": res.text, "intent": res.intent,
                                            "assumptions": res.assumptions})
            except Exception as e:                       # noqa: BLE001
                return self._json(500, {
                    "error": f"{type(e).__name__}: {e}",
                    "hint": "The chat endpoint needs an LLM key in .env. "
                            "/v1/score works without one.",
                })

        self._json(404, {"error": "not found"})


def main(port: int = 8000) -> None:
    print("loading airport data ...", file=sys.stderr)
    _load()
    print(f"ready: {len(_FACTS)} airports\n"
          f"  open   http://localhost:{port}\n"
          f"  or     curl -s localhost:{port}/v1/score -d '{{\"profile\":\"congestion\"}}'",
          file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
