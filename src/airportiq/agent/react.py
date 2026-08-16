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

How to work:
- Call tools to gather what you need. Prefer several targeted calls over one vague one.
- For a region question, call list_region FIRST so the region definition is explicit.
- For "why" questions about congestion, call get_delay_breakdown — it separates airspace
  delay (a capacity ceiling) from carrier and weather delay (not the airport's constraint).
- For unmet or suppressed demand, call estimate_unmet_demand. Do not estimate it yourself.
- If a tool returns an error or no data, SAY SO. Never fill the gap with a plausible number.

How to answer:
- Quote figures returned by tools, exactly as returned. Never compute, adjust or round them.
- Percentiles are within an airport's own hub class. Say so; they are not absolute volumes.
- State assumptions the tools report — a region definition, an ambiguous name, a legal cap.
- If an airport carries a flag, lead with it. A legal cap or a runway constraint changes the
  recommendation entirely.
- Be concise. An analyst wants the finding and the reason, not a paragraph of preamble."""


def run(question: str, cards: list, facts_by_code: dict,
        max_rounds: int = MAX_ROUNDS) -> dict:
    """Answer one question with tool use. Returns the answer and a full call trace."""
    tools.bind(cards, facts_by_code)

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
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
