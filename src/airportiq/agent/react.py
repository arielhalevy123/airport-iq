"""The tool-calling loop.

The model is given the tools in `tools.py` and decides which to call, in what order, and when
it has enough to answer. That is the agentic part — and it is what lets an open-ended question
be answered without the pipeline having anticipated its shape.

WHAT IS BOUNDED, AND WHY
A loop that can call tools forever is a loop that will, eventually, on someone's bill. Three
limits, all explicit:

  MAX_ROUNDS      how many times the model may go back for more data
  MAX_CALLS       total tool calls across the whole turn
  seen_calls      identical repeated calls are answered from cache, not re-run

The third matters more than it looks: the common failure of a tool loop is not infinite
recursion, it is the model calling `get_airport_metrics("SFO")` four times because it forgot
it already had the answer. Caching makes that free instead of expensive.

WHAT DOES NOT CHANGE
Tools return values from the pure engine. The model chooses questions; it never computes.
`/v1/score` still reproduces every number, and the purity test still passes.
"""
from __future__ import annotations

import json

from . import llm, tools

MAX_ROUNDS = 4
MAX_CALLS = 10

SYSTEM = """You are an aviation capacity analyst. Answer using ONLY the tools provided.

TOOL ROUTING (match the wording of the question, not a plausible-sounding neighbour):

- Region or US state ("Florida", "Texas", "New England"): call list_region FIRST. Never
  decide state membership yourself. Then rank the returned airports with rank_airports
  (composite) OR with a growth tool, depending on the question.
- "Expansion", "modernisation", "investment candidates", "most capacity-constrained":
  rank_airports with the appropriate composite profile.
- "Growing fastest", "top growing", "highest growth": use rank_by_passenger_growth or
  rank_by_flight_growth. Do NOT use rank_airports — its composite weights growth at ~0.15,
  which is not what "growing fastest" means.
- "Passenger growth vs flight growth", "high pax growth but low flight growth": use
  compare_growth. Never substitute gate_saturation for flight growth — gate_saturation is a
  seat-upgauging proxy, not a departure-count rate.
- "Delay rate relative to passenger volume", "delay per passenger": use
  get_delay_per_passenger. Do NOT substitute NAS delay share; that is a cause mix, not a
  per-passenger rate.
- "Why is X congested": get_delay_breakdown.
- "Long haul", "stage length", "sector length": get_stage_length_mix. Two thresholds, both
  reported; name the one you use.
- "Cargo", "freight", or contextualising ANC-style hubs: get_cargo_intensity.
- Unmet or suppressed demand: estimate_unmet_demand.

STRUCTURAL RULES (violating any of these is a wrong answer even if the number is right):

- Raw ≠ percentile ≠ composite. If the user asks a "how much" question (growth rate, load
  factor, delay per passenger, freight per pax), quote the RAW value from the tool. Percentiles
  are ranking aids; do not quote a percentile in place of a raw rate.
- Percentiles are computed WITHIN each airport's hub class. Do not compare a medium-hub
  percentile to a large-hub percentile as if they were on one scale. If you must rank across
  classes, use raw rates (growth) or explicit composites, and name each airport's class.
- gate_saturation, airside_saturation, airside_headroom and peak_pressure are PROXIES, not
  physical measurements. If you cite one, label it as an inference. Never imply a physical
  gate count or a facility-level throughput measurement.
- Delay data covers ONE month (see data_period_delays from the tools). Do not present it as
  stable annual congestion.
- Delay and stage-length data are DOMESTIC flights only. For airports with heavy international
  activity (ANC especially, but also JFK, MIA, LAX, SFO), say so.
- Missing data lowers confidence. If a tool returns missing[] or an error, say what is missing
  and name the airports affected (BGR and BTV commonly lack delay data).
- Freight, cargo and passenger metrics are separate. A cargo-heavy airport does not thereby
  need a passenger terminal.

EXPLANATIONS MUST MATCH THE SCORE:

- When you cite a ranking, quote the actual top_drivers the tool returned. Do not say a
  ranking is "driven by congestion and growth" if the drivers list says peak_pressure and
  international_intensity. If the drivers do not fit the story, correct the story.
- For A vs B comparisons, if one is worse on delay but the other is worse on saturation or
  peak pressure, name BOTH. Do not oversimplify into a single "more constrained" verdict
  when the metrics disagree.

SCOPE OF CLAIMS:

- There is no cost, IRR or profitability data in this system. Cost, "will it be profitable",
  and "what will a new terminal cost" questions must state that limitation and offer the
  capacity-pressure signal instead. Do not present general knowledge as project-specific.
- Frame outputs as SCREENING signals for further due diligence, not as investment
  underwriting.

STYLE:

- Quote tool figures exactly as returned. Never compute, adjust or round in prose.
- If a flag is present (legal cap, runway constraint) it leads. It changes the answer.
- Be concise. Finding, then reason. No paragraphs of preamble."""


def run(question: str, cards: list, facts_by_code: dict,
        max_rounds: int = MAX_ROUNDS,
        history: list[dict] | None = None) -> dict:
    """Answer one question with tool use. Returns the answer and a full call trace."""
    tools.bind(cards, facts_by_code)

    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})
    trace: list[dict] = []
    seen: dict[str, str] = {}
    total_calls = 0

    for round_no in range(max_rounds):
        reply = llm.complete_with_tools(messages, tools.SCHEMAS, temperature=0.0)
        calls = reply.get("tool_calls") or []

        if not calls:
            return {"answer": reply.get("content", "").strip(),
                    "trace": trace, "rounds": round_no + 1,
                    "tool_calls": total_calls}

        messages.append({"role": "assistant", "content": reply.get("content") or None,
                         "tool_calls": calls})

        for c in calls:
            if total_calls >= MAX_CALLS:
                result = json.dumps({"error": "tool budget exhausted for this turn"})
            else:
                fn = c["function"]["name"]
                args = c["function"].get("arguments") or "{}"
                key = f"{fn}:{args}"
                if key in seen:
                    result = seen[key]              # repeated call, answered from cache
                    trace.append({"tool": fn, "args": args, "cached": True})
                else:
                    result = tools.call(fn, args)
                    seen[key] = result
                    total_calls += 1
                    trace.append({"tool": fn, "args": args,
                                  "result_preview": result[:200]})
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})

    # Out of rounds. Ask once for a final answer from what was gathered, rather than
    # returning nothing — the data is already in the message history.
    messages.append({"role": "user",
                     "content": "Answer now from the data already gathered. "
                                "Say plainly if it is insufficient."})
    final = llm.complete_with_tools(messages, tools=None, temperature=0.0)
    return {"answer": (final.get("content") or "").strip(), "trace": trace,
            "rounds": max_rounds, "tool_calls": total_calls,
            "note": "reached the round limit; answered from data already gathered"}


def run_streaming(question: str, cards: list, facts_by_code: dict,
                  max_rounds: int = MAX_ROUNDS,
                  history: list[dict] | None = None):
    """The same loop, yielding events as they happen instead of one dict at the end.

    Yields (kind, payload):
        tool_call    a tool is about to run, or was served from cache
        tool_result  that tool returned; carries a short preview
        round        a new round of model reasoning began
        delta        a fragment of the final prose answer
        done         the full answer plus the complete trace

    WHY THIS IS WORTH THE DUPLICATION
    The interesting latency in this system is not token generation, it is the tool calls:
    the model thinks, queries, thinks again. A spinner hides exactly the part a reviewer
    most wants to see. Streaming the TOOL EVENTS, not just the text, turns the wait into
    the demonstration — you watch it decide to call list_region before ranking, which is
    the agentic behaviour the brief is asking about.

    Kept as a separate function rather than folded into run() with a callback: run() is the
    reference implementation used by the evals, and threading optional emit-hooks through
    it would make the thing under test differ from the thing in production.
    """
    tools.bind(cards, facts_by_code)

    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})
    trace: list[dict] = []
    seen: dict[str, str] = {}
    total_calls = 0

    for round_no in range(max_rounds):
        yield ("round", {"round": round_no + 1})

        answer_parts: list[str] = []
        reply = None
        for kind, payload in llm.stream_with_tools(messages, tools.SCHEMAS, temperature=0.0):
            if kind == "delta":
                # Forward prose live rather than buffering to the end of the round. Buffering
                # meant "streaming" showed nothing until the answer was already complete.
                # Rounds with tool calls occasionally emit a short "let me check…" preamble
                # before the tool_call arrives — the client resets the answer buffer on
                # tool_call so that reasoning text does not pollute the final prose.
                answer_parts.append(payload)
                yield ("delta", payload)
            else:
                reply = payload

        calls = (reply or {}).get("tool_calls") or []

        if not calls:
            text = "".join(answer_parts).strip() or (reply or {}).get("content") or ""
            yield ("done", {"answer": text, "trace": trace,
                            "rounds": round_no + 1, "tool_calls": total_calls})
            return

        messages.append({"role": "assistant", "content": (reply or {}).get("content") or None,
                         "tool_calls": calls})

        for c in calls:
            fn = c["function"]["name"]
            args = c["function"].get("arguments") or "{}"
            if total_calls >= MAX_CALLS:
                result = json.dumps({"error": "tool budget exhausted for this turn"})
                entry = {"tool": fn, "args": args, "budget_exhausted": True}
            else:
                key = f"{fn}:{args}"
                if key in seen:
                    result = seen[key]
                    entry = {"tool": fn, "args": args, "cached": True}
                else:
                    yield ("tool_call", {"tool": fn, "args": args})
                    result = tools.call(fn, args)
                    seen[key] = result
                    total_calls += 1
                    entry = {"tool": fn, "args": args, "result_preview": result[:200]}
            trace.append(entry)
            yield ("tool_result", entry)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})

    messages.append({"role": "user",
                     "content": "Answer now from the data already gathered. "
                                "Say plainly if it is insufficient."})
    parts: list[str] = []
    for kind, payload in llm.stream_with_tools(messages, tools=None, temperature=0.0):
        if kind == "delta":
            parts.append(payload)
            yield ("delta", payload)
    yield ("done", {"answer": "".join(parts).strip(), "trace": trace,
                    "rounds": max_rounds, "tool_calls": total_calls,
                    "note": "reached the round limit; answered from data already gathered"})
