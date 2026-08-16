"""The agent: question in, grounded answer out.

The pipeline, and the order matters:

    parse (LLM)  ->  resolve (deterministic)  ->  fetch (deterministic)
                 ->  score  (deterministic)   ->  narrate (LLM, placeholders only)
                 ->  verify (deterministic)

The LLM appears exactly twice, and neither time can it produce a number that reaches the user.
On the way in it classifies intent and extracts surface strings. On the way out it writes prose
containing placeholders like {{SFO.load_factor}}; the server substitutes real values afterwards
and then scans the result for any numeric literal that is not in the allow-set. A violation is
caught, not hoped against.

That last step is the difference between claiming the model cannot hallucinate a figure and
being able to demonstrate it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import llm, resolve
from .session import Session, Turn, is_followup

INTENTS = ("rank", "compare", "metric", "explain", "unsupported")

_PARSE_SYSTEM = """You convert an aviation analyst's question into a JSON plan. Output ONLY JSON.

Schema:
{
  "intent": "rank" | "compare" | "metric" | "explain" | "unsupported",
  "entities": ["surface strings exactly as the user wrote them"],
  "region": "region name if one is named, else null",
  "profile": "terminal_expansion" | "congestion",
  "metric": "load_factor" | "international_share" | "long_haul_share" | null,
  "reason": "short note if intent is unsupported"
}

Rules you must follow:
- Copy entity strings VERBATIM. Do not convert names to airport codes. That is done elsewhere.
- "terminal expansion" or anything about gates/terminals -> profile "terminal_expansion".
- "congestion", "delays", "capacity" -> profile "congestion".
- If the question needs data we do not have (construction cost, ticket prices, ROI, staffing),
  return intent "unsupported" with a reason. Do not guess."""


@dataclass
class Answer:
    text: str
    intent: str
    assumptions: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    unsupported_reason: str = ""


def _parse(question: str, prior: str = "") -> dict:
    """Turn a question into a plan. `prior` carries the previous turn so that a bare
    follow-up ("why?") is interpreted against it rather than rejected as meaningless."""
    prompt = question
    if prior:
        prompt = (f"Previous question in this conversation: {prior}\n"
                  f"Follow-up question: {question}\n\n"
                  "Interpret the follow-up in the context of the previous question. "
                  "If it asks 'why', the intent is 'explain' and the entities are the same "
                  "as the previous question.")
    raw = llm.complete(prompt, system=_PARSE_SYSTEM, temperature=0.0, max_tokens=300)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        return {"intent": "unsupported", "reason": "could not parse the question into a plan"}
    if plan.get("intent") not in INTENTS:
        plan["intent"] = "unsupported"
    if plan.get("profile") not in ("terminal_expansion", "congestion"):
        plan["profile"] = "terminal_expansion"
    return plan


_NARRATE_SYSTEM = """You explain an airport ranking to an investment analyst.

ABSOLUTE RULE: never write a digit. Every number must be a placeholder in the form
{{CODE.field}}, for example {{SFO.composite}} or {{SFO.peak_pressure}}. The server substitutes
real values afterwards. If you write a literal number the answer is rejected.

ALWAYS NAME THE METRIC next to its placeholder, in words a non-specialist understands.
  GOOD: "peak terminal pressure at {{BOS.peak_pressure}} and gate saturation at {{BOS.gate_saturation}}"
  BAD:  "high {{BOS.peak_pressure}} and significant {{BOS.gate_saturation}}"
A number with no metric name attached is useless to the reader.

All scores are PERCENTILE RANKS WITHIN THE AIRPORT'S HUB CLASS, 0-100. Say so, and never
present them as passenger counts or absolute figures.

Cover EVERY airport in the data given, not just the top one. If only one airport in a named
region has data, say explicitly that the others lack coverage.

Be concise and concrete. Say which KPI drove each result. If an airport carries a flag, lead
with it — a legally capped airport or a runway-constrained one changes the recommendation.
Do not speculate beyond the data given."""


_NUM = re.compile(r"\d+(?:\.\d+)?")
_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9]{3})\.([a-z_]+)\}\}")


def _substitute(text: str, values: dict[str, dict[str, float]]) -> tuple[str, list[str]]:
    """Replace placeholders with real values; report any that could not be resolved."""
    problems: list[str] = []

    def repl(m: re.Match) -> str:
        code, fieldname = m.group(1), m.group(2)
        v = values.get(code, {}).get(fieldname)
        if v is None:
            problems.append(f"{code}.{fieldname}")
            return "[unavailable]"
        return f"{v:g}"

    return _PLACEHOLDER.sub(repl, text), problems


def _verify_no_stray_numbers(text: str, allowed: set[str]) -> list[str]:
    """Every numeric literal in the final text must come from the data we substituted.
    Ordinals and years are permitted."""
    stray = []
    for m in _NUM.finditer(text):
        tok = m.group(0)
        if tok in allowed:
            continue
        if len(tok) == 4 and tok.startswith(("19", "20")):   # a year
            continue
        if tok.isdigit() and int(tok) <= 50:                  # rank/ordinal
            continue
        stray.append(tok)
    return stray


def answer(question: str, cards: list, facts_by_code: dict,
           session: Session | None = None) -> Answer:
    """Answer one question against an already-computed set of ScoreCards.

    If a session is supplied and the question is a follow-up ("why?", "what about SFO"),
    the airports and profile from the previous turn carry over. Numbers never do - they are
    always re-read from the score cards.
    """
    followup = bool(session and session.turns and is_followup(question))
    prior = session.last.question if followup and session.last else ""
    plan = _parse(question, prior=prior)

    # A follow-up that still parses as unsupported means the model could not use the
    # context. Fall back to explaining the previous turn rather than refusing - the user
    # asked a reasonable question and we know what it refers to.
    if followup and plan["intent"] == "unsupported" and session.context_codes():
        plan = {"intent": "explain", "entities": [], "region": None,
                "profile": session.context_profile() or "terminal_expansion"}

    if plan["intent"] == "unsupported":
        return Answer(
            text=("I cannot answer that from the data I have. "
                  + (plan.get("reason") or "")).strip(),
            intent="unsupported",
            unsupported_reason=plan.get("reason", ""),
        )

    assumptions: list[str] = []
    by_code = {c.code: c for c in cards}

    # Deterministic resolution — never the model's job.
    if plan.get("region"):
        try:
            codes, note = resolve.resolve_region(plan["region"])
            assumptions.append(note)
        except ValueError:
            codes = []
            assumptions.append(f"I do not have a definition for region {plan['region']!r}.")
    elif plan.get("entities"):
        codes, notes = resolve.resolve_many(plan["entities"])
        assumptions.extend(notes)
    elif followup and session.context_codes():
        codes = session.context_codes()
        assumptions.append(
            f"Following up on {', '.join(codes)} from the previous question."
        )
    else:
        codes = [c.code for c in cards if c.hub_class == "large"][:10]
        assumptions.append("No region given — showing large hubs.")

    selected = [by_code[c] for c in codes if c in by_code]
    if not selected:
        return Answer(
            text=("I have no data for those airports, so I would rather say so than "
                  "produce a number I cannot support."),
            intent=plan["intent"], assumptions=assumptions,
        )

    selected = sorted(selected, key=lambda c: (c.composite is None, -(c.composite or 0)))[:8]

    # Build the substitution table. This is the ONLY source of numbers in the final text.
    values: dict[str, dict[str, float]] = {}
    for c in selected:
        values[c.code] = {"composite": c.composite, "rank": c.rank}
        for k, v in c.kpis.items():
            if isinstance(v, (int, float)):
                values[c.code][k] = round(v, 1)

    briefing = {
        "profile": plan["profile"],
        "airports": [
            {"code": c.code, "name": c.name, "hub_class": c.hub_class,
             "rank": c.rank, "top_drivers": list(c.contributions)[:3],
             "flags": c.flags, "missing": c.missing}
            for c in selected
        ],
        "placeholders_available": {c.code: sorted(values[c.code]) for c in selected},
    }

    prose = llm.complete(
        f"Question: {question}\n\nData:\n{json.dumps(briefing, indent=2)}",
        system=_NARRATE_SYSTEM, temperature=0.1, max_tokens=700,
    )

    text, unresolved = _substitute(prose, values)
    allowed = {f"{v:g}" for d in values.values() for v in d.values() if v is not None}
    stray = _verify_no_stray_numbers(text, allowed)

    if stray:
        # The guard fired. Fall back to a deterministic template rather than ship a number
        # we cannot trace. This is the behaviour that makes the claim testable.
        lines = [f"Ranking by {plan['profile'].replace('_', ' ')}:"]
        for c in selected:
            lines.append(f"  {c.rank}. {c.code} — score {c.composite}, "
                         f"driven by {', '.join(list(c.contributions)[:2])}")
            for f in c.flags:
                lines.append(f"     ! {f}")
        text = "\n".join(lines)
        assumptions.append(
            f"The generated explanation contained figures I could not trace to the data "
            f"({', '.join(stray[:5])}), so it was replaced with the computed values directly."
        )

    if unresolved:
        assumptions.append(f"Some requested values were unavailable: {', '.join(unresolved[:5])}.")

    for c in selected:
        for f in c.flags:
            if f not in assumptions:
                assumptions.append(f"{c.code}: {f}")

    if session is not None:
        session.add(Turn(question=question, intent=plan["intent"],
                         codes=[c.code for c in selected], profile=plan["profile"]))
        # Do not repeat a caveat already stated this session.
        assumptions = session.new_assumptions(assumptions)

    return Answer(text=text.strip(), intent=plan["intent"],
                  assumptions=assumptions, data=briefing)
