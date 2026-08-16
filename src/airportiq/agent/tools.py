"""Tools the model may call.

This is where the system becomes an agent rather than a pipeline: the model decides WHICH
data to look at and in WHAT ORDER, then reasons over what comes back. That is real agency,
and it is what makes open-ended questions ("why is SFO constrained?") answerable — a fixed
pipeline can only answer questions whose shape it anticipated.

THE BOUNDARY, WHICH DOES NOT MOVE
Every tool is READ-ONLY and returns values computed by the pure scoring engine. The model
chooses the questions; it never computes an answer. So the purity test still passes, the
numeric guard still fires, and `/v1/score` still reproduces any figure the agent quotes.

Giving a model tools is often where determinism quietly dies — the model starts "computing"
in prose over tool output. The defence here is that every tool returns STRUCTURED values with
their provenance, and the narration layer downstream still cannot emit a digit of its own.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import resolve

# Populated by bind() so the tool functions can reach the current data without globals
# leaking across requests.
_CARDS: dict[str, Any] = {}
_FACTS: dict[str, Any] = {}


def bind(cards: list, facts_by_code: dict) -> None:
    global _CARDS, _FACTS
    _CARDS = {c.code: c for c in cards}
    _FACTS = facts_by_code


# --------------------------------------------------------------------------- the tools

def get_airport_metrics(airport: str) -> dict:
    """Every computed KPI for one airport, with its rank and any flags."""
    try:
        code, note = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous) as e:
        return {"error": f"could not resolve {airport!r}", "detail": str(e)}
    card = _CARDS.get(code)
    if not card:
        return {"error": f"no data for {code}"}
    return {
        "code": code, "name": card.name, "hub_class": card.hub_class,
        "rank_in_hub_class": card.rank, "composite": card.composite,
        "percentiles": {k: v for k, v in card.kpis.items() if isinstance(v, (int, float))},
        "flags": card.flags, "missing": card.missing,
        "note": note or None,
    }


def compare_airports(airports: list[str]) -> dict:
    """Side-by-side percentiles for several airports."""
    out, notes = [], []
    for a in airports:
        m = get_airport_metrics(a)
        if m.get("note"):
            notes.append(m.pop("note"))
        out.append(m)
    return {"airports": out, "notes": notes,
            "caveat": ("Percentiles are within each airport's own hub class, so a medium hub "
                       "at the 90th percentile is not busier than a large hub at the 50th — "
                       "it is more constrained relative to its peers.")}


def list_region(region: str) -> dict:
    """Which airports a region name covers, and the definition used."""
    try:
        codes, note = resolve.resolve_region(region)
    except ValueError:
        return {"error": f"no definition on file for region {region!r}",
                "known": ["New England"]}
    available = [c for c in codes if c in _CARDS]
    return {"region": region, "definition": note,
            "airports": available,
            "excluded_no_data": [c for c in codes if c not in _CARDS]}


def get_delay_breakdown(airport: str) -> dict:
    """Why an airport is delayed: airspace versus carrier versus weather."""
    try:
        code, _ = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous):
        return {"error": f"could not resolve {airport!r}"}
    f = _FACTS.get(code)
    if not f or f.nas_delay_share is None:
        return {"error": f"no delay data for {code}"}
    return {
        "code": code,
        "nas_delay_share": round(f.nas_delay_share, 4),
        "mean_taxi_out_min": f.mean_taxi_out_min,
        "interpretation": ("NAS delay is airspace-system delay — volume, capacity, flow "
                           "control. A high share means the airport is hitting a capacity "
                           "ceiling rather than suffering airline or weather problems."),
    }


def estimate_unmet_demand(airport: str) -> dict:
    """Suppressed demand at one airport, as a range with its method and mechanism."""
    from ..scoring.unmet import estimate
    try:
        code, _ = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous):
        return {"error": f"could not resolve {airport!r}"}
    card, f = _CARDS.get(code), _FACTS.get(code)
    if not card or not f:
        return {"error": f"no data for {code}"}
    u = estimate(card, f)
    return {"code": code,
            "low_pct": round(u.low_pct, 4) if u.low_pct is not None else None,
            "high_pct": round(u.high_pct, 4) if u.high_pct is not None else None,
            "low_passengers": int(u.low_pax) if u.low_pax else None,
            "high_passengers": int(u.high_pax) if u.high_pax else None,
            "mechanism": u.mechanism, "method": u.method,
            "caveats": u.caveats, "confidence": u.confidence}


def rank_airports(profile: str = "congestion", hub_class: str | None = None,
                  airports: list[str] | None = None, limit: int = 10) -> dict:
    """Rank a specific set of airports, or a whole hub class.

    `airports` exists because the natural region flow is list_region -> rank those codes.
    Without it the model could only rank a hub class, so a New England query returned BOS
    alone and reported the others as absent from the rankings - technically true, and a
    worse answer than the one it replaced.
    """
    if profile not in ("congestion", "terminal_expansion"):
        return {"error": f"unknown profile {profile!r}"}

    if airports:
        wanted = set()
        for a in airports:
            try:
                wanted.add(resolve.resolve_airport(a, allow_primary=True)[0])
            except (ValueError, resolve.Ambiguous):
                continue
        rows = [c for c in _CARDS.values() if c.code in wanted and c.composite is not None]
    else:
        rows = [c for c in _CARDS.values()
                if (hub_class is None or c.hub_class == hub_class)
                and c.composite is not None]
    rows.sort(key=lambda c: (-(c.composite or 0), c.code))
    return {"profile": profile, "hub_class": hub_class or "all",
            "note": ("Ranks and percentiles are within each airport's own hub class, so "
                     "comparing a small hub's rank to a large hub's is meaningless. Compare "
                     "the composite, and say which class each is in."),
            "results": [{"rank_in_hub_class": c.rank, "code": c.code, "name": c.name,
                         "hub_class": c.hub_class, "composite": c.composite,
                         "top_drivers": list(c.contributions)[:3], "flags": c.flags}
                        for c in rows[:limit]]}


REGISTRY: dict[str, Callable] = {
    "get_airport_metrics": get_airport_metrics,
    "compare_airports": compare_airports,
    "list_region": list_region,
    "get_delay_breakdown": get_delay_breakdown,
    "estimate_unmet_demand": estimate_unmet_demand,
    "rank_airports": rank_airports,
}

# OpenAI-style schemas. Kept next to the implementations so the two cannot drift.
SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_airport_metrics",
        "description": "All computed KPI percentiles, rank and flags for one airport.",
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string", "description": "IATA code or city name, e.g. SFO or Santa Ana"}},
            "required": ["airport"]}}},
    {"type": "function", "function": {
        "name": "compare_airports",
        "description": "Side-by-side metrics for two or more airports.",
        "parameters": {"type": "object", "properties": {
            "airports": {"type": "array", "items": {"type": "string"}}},
            "required": ["airports"]}}},
    {"type": "function", "function": {
        "name": "list_region",
        "description": "Which airports a region covers, and the definition used. Use this before ranking a named region.",
        "parameters": {"type": "object", "properties": {
            "region": {"type": "string", "description": "e.g. New England"}},
            "required": ["region"]}}},
    {"type": "function", "function": {
        "name": "get_delay_breakdown",
        "description": "Why an airport is delayed: airspace vs carrier vs weather. Use for 'why' questions about congestion.",
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string"}}, "required": ["airport"]}}},
    {"type": "function", "function": {
        "name": "estimate_unmet_demand",
        "description": "Suppressed demand as a range, with method and physical mechanism.",
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string"}}, "required": ["airport"]}}},
    {"type": "function", "function": {
        "name": "rank_airports",
        "description": ("Ranked shortlist. Pass `airports` to rank a specific set (use after "
                        "list_region), or `hub_class` for a whole class, or neither for all."),
        "parameters": {"type": "object", "properties": {
            "profile": {"type": "string", "enum": ["congestion", "terminal_expansion"]},
            "hub_class": {"type": "string", "enum": ["large", "medium", "small", "nonhub"],
                          "description": "Omit to rank across all classes."},
            "airports": {"type": "array", "items": {"type": "string"},
                         "description": "Specific airports to rank. Use this after list_region."},
            "limit": {"type": "integer"}}, "required": []}}},
]


def call(name: str, arguments: str | dict) -> str:
    """Dispatch one tool call. Returns JSON, always — an exception here would otherwise
    surface to the user as a broken answer rather than a handled gap."""
    fn = REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name!r}"})
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        return json.dumps(fn(**args), ensure_ascii=False, default=str)
    except Exception as e:                       # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
