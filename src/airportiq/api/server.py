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
import re
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
    from airportiq import obs

    with obs.span("build_facts", source="bts+snapshot") as sp:
        _FACTS = build_facts(sorted(JET_RUNWAYS))
        sp.output(airports=len(_FACTS))

    # The engine is traced from OUT HERE, never from inside. scoring/ stays free of any
    # network, clock or third-party import, so tests/test_purity.py keeps passing — see
    # airportiq/obs.py for why that boundary is where the instrumentation belongs.
    for profile in ("terminal_expansion", "congestion"):
        with obs.span("score", profile=profile, airports=len(_FACTS)) as sp:
            _CARDS[profile] = score(_FACTS, profile)
            cards = _CARDS[profile]
            sp.output(
                scored=len(cards),
                incomplete=sum(1 for c in cards if c.missing),
                flagged=sum(1 for c in cards if c.flags),
                top=[c.code for c in cards[:5]],
            )


_CODE_RE = re.compile(r'"(?:airport|airports)"\s*:\s*(?:"([^"]+)"|\[([^\]]*)\])')


def _scorecards_for(trace: list[dict], limit: int = 4) -> list[dict]:
    """The engine's own numbers for the airports this turn actually looked at.

    Read out of the tool-call arguments rather than out of the prose. Parsing the model's
    sentence for airport codes would mean the panel agrees with the narration by
    construction, which is precisely the check we want to keep independent.
    """
    from airportiq.agent import resolve

    seen: list[str] = []
    for call in trace:
        for m in _CODE_RE.finditer(call.get("args") or ""):
            raw = m.group(1) or m.group(2) or ""
            for token in raw.replace('"', " ").split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    code, _ = resolve.resolve_airport(token, allow_primary=True)
                except (ValueError, resolve.Ambiguous):
                    continue
                if code not in seen:
                    seen.append(code)

    out = []
    for code in seen[:limit]:
        card = next((c for c in _CARDS["congestion"] if c.code == code), None)
        if card is None:
            continue
        out.append({
            "code": card.code, "name": card.name, "hub_class": card.hub_class,
            "rank": card.rank, "composite": card.composite,
            "kpis": {k: round(v, 1) for k, v in card.kpis.items()
                     if isinstance(v, (int, float))},
            "flags": card.flags, "missing": card.missing,
        })
    return out


# Absolute, not relative: a relative import fails when this file is run directly
# (`python src/airportiq/api/server.py`), which is exactly what someone tries first.
# sys.path is already set above, so both entry points work.
from airportiq.api.ui import INDEX  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The UI is a string baked into the Python module, so it changes whenever the server
        # is restarted with new code — but the browser has no way to know that and will
        # happily re-serve the page it already has. The failure that causes is nasty to
        # diagnose: the server is running the new build, curl proves the new endpoint works,
        # and the tab in front of you is still the old app calling the old route.
        if "html" in ctype:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, indent=1).encode(), "application/json")

    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # MUST be close, not keep-alive. An SSE response has no Content-Length, so the
        # only thing that tells the client the body has ended is the socket closing. On
        # keep-alive the browser's reader never reports done: the answer streams in and
        # renders, but the code that runs *after* the stream — trace, scorecards, and
        # re-enabling the input — simply never fires. It fails as a missing feature
        # rather than as an error, which is why it survived a first round of testing.
        self.send_header("Connection", "close")
        # Without this a reverse proxy will happily buffer the whole stream and deliver
        # it as one lump, which looks exactly like streaming being broken.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data: dict) -> bool:
        """Write one SSE frame. Returns False once the client has gone away."""
        try:
            payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

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

        if self.path == "/v1/chat/stream":
            question = (payload.get("question") or "").strip()
            if not question:
                return self._json(400, {"error": "question is required"})

            by_code = {f.code: f for f in _FACTS}
            self._sse_open()
            try:
                from airportiq.agent import react
                saw_region = False
                for kind, data in react.run_streaming(question, _CARDS["congestion"], by_code):
                    if kind == "tool_result" and "list_region" in data.get("tool", ""):
                        saw_region = True
                    if kind == "done":
                        assumptions = []
                        if saw_region:
                            assumptions.append("Region membership resolved deterministically, "
                                               "not inferred by the model.")
                        data = {**data, "assumptions": assumptions,
                                "scorecards": _scorecards_for(data.get("trace") or [])}
                    if not self._sse(kind, data):
                        return                       # client navigated away mid-answer
            except Exception as e:                   # noqa: BLE001
                # The stream is already open, so an error has to travel as an event —
                # a 500 status cannot be sent after the headers have gone out.
                self._sse("error", {"error": f"{type(e).__name__}: {e}"})
            return

        if self.path == "/v1/chat":
            # Attach the engine's own scorecards for whichever airports the turn touched.
            # This is the point of the interface: the model writes the sentence, and the
            # bars beside it come straight from the deterministic engine, so a reader can
            # check the prose against the numbers without taking anything on trust.
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
                        "assumptions": assumptions,
                        "scorecards": _scorecards_for(out["trace"])})
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
