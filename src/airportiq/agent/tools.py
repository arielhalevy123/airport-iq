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
#
# BOTH profiles are bound, because composite, rank, flags and missing[] are PROFILE-
# SPECIFIC. Binding one card set and letting rank_airports echo whatever profile name the
# caller asked for produced the worst kind of wrong answer: a congestion ranking wearing a
# terminal_expansion label, with the airside-first flag structurally unreachable.
# tests/test_tools.py::test_rank_airports_uses_the_requested_profiles_cards pins this.
_CARDS_BY_PROFILE: dict[str, dict[str, Any]] = {}
_CARDS: dict[str, Any] = {}       # the default (congestion) view, for profile-independent
_FACTS: dict[str, Any] = {}       # lookups: hub class, growth rates, facts passthrough

_DEFAULT_PROFILE = "congestion"


def bind(cards_by_profile: dict[str, list], facts_by_code: dict) -> None:
    global _CARDS_BY_PROFILE, _CARDS, _FACTS
    _CARDS_BY_PROFILE = {p: {c.code: c for c in cards}
                         for p, cards in cards_by_profile.items()}
    _CARDS = (_CARDS_BY_PROFILE.get(_DEFAULT_PROFILE)
              or next(iter(_CARDS_BY_PROFILE.values()), {}))
    _FACTS = facts_by_code


def _cards_for(profile: str) -> dict[str, Any]:
    return _CARDS_BY_PROFILE.get(profile, _CARDS)


# --------------------------------------------------------------------------- the tools

def _raw_metrics(f) -> dict:
    """Raw (un-normalised) values the caller should use whenever the user asks a "how much"
    question rather than a "how ranked" question. Percentiles are compressed views of these
    same values, so a question about growth rate must get the growth rate, not its rank.

    Every entry is either a real number or None. None means the input is missing; the caller
    must say so rather than substitute a plausible figure.
    """
    pax_growth = None
    if f.passengers_ttm and f.passengers_2y_ago and f.passengers_2y_ago > 0:
        pax_growth = (f.passengers_ttm / f.passengers_2y_ago) - 1.0
    flight_growth = None
    if f.departures_ttm and f.departures_2y_ago and f.departures_2y_ago > 0:
        flight_growth = (f.departures_ttm / f.departures_2y_ago) - 1.0
    upgauge = None
    if f.seats_per_departure_now and f.seats_per_departure_base:
        upgauge = (f.seats_per_departure_now / f.seats_per_departure_base) - 1.0
    return {
        "passengers_ttm": f.passengers_ttm,
        "departures_ttm": f.departures_ttm,
        "load_factor_pct_ttm": round(f.load_factor_ttm, 2)
                               if f.load_factor_ttm is not None else None,
        "international_share_ttm": (round(f.international_share, 4)
                                    if f.international_share is not None else None),
        "passenger_growth_2y": round(pax_growth, 4) if pax_growth is not None else None,
        "flight_growth_2y": (round(flight_growth, 4)
                             if flight_growth is not None else None),
        "seat_upgauge_2y": round(upgauge, 4) if upgauge is not None else None,
        "nas_delay_share": (round(f.nas_delay_share, 4)
                            if f.nas_delay_share is not None else None),
        "mean_taxi_out_min": f.mean_taxi_out_min,
        "freight_lbs_ttm": f.freight_lbs_ttm,
        "jet_runways": f.jet_runways,
    }


# Which KPIs are direct measurements and which are proxies. Surfaced in every tool response
# so a reader is not left inferring whether "gate saturation 87th percentile" is a physical
# gate count. It is not — it is a seat-upgauging ratio, a fingerprint of a gate/slot
# constraint rather than a measure of one. Same for airside_saturation, which is a runway
# COUNT proxy — not a runway CAPACITY measurement.
# One wording, shared with the UI tooltips (scoring/explain.py), so the two cannot drift.
from ..scoring.explain import PROXY_LABELS as _PROXY_LABELS  # noqa: E402


def _scope_notes(card, f) -> dict:
    """Provenance and coverage disclosures every tool result should carry."""
    notes = {
        "data_period_delays": f.delay_period,
        "delay_scope": ("DOMESTIC flights by reporting US carriers only. International "
                        "departures are not counted in nas_delay_share, taxi_out or "
                        "stage_length."),
        "delay_temporal_scope": ("Delay figures reflect ONE month only. Seasonal "
                                 "congestion is not visible; do not present them as "
                                 "stable annual behaviour."),
        "percentile_scope": ("Percentiles are within an airport's own hub class. Not "
                             "comparable across hub classes as if they were on one scale."),
        "financial_scope": ("This system has no cost or IRR data. Do not present cost, "
                            "profit, ROI or terminal-construction estimates as sourced."),
    }
    return notes


def get_airport_metrics(airport: str, profile: str = _DEFAULT_PROFILE) -> dict:
    """Every computed KPI for one airport, with its rank and any flags.

    Returns raw values AND percentiles AND composite side by side, plus proxy labels for
    the KPIs that are inferred rather than measured. A caller asking for a growth rate must
    quote the raw growth rate, not its percentile — the percentile is a rank aid, not a rate.

    Composite, rank, flags and missing[] are PROFILE-SPECIFIC; the profile used is named in
    the response so it can never be silently mislabelled.
    """
    try:
        code, note = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous) as e:
        return {"error": f"could not resolve {airport!r}", "detail": str(e)}
    card = _cards_for(profile).get(code)
    if not card:
        return {"error": f"no data for {code}"}
    fact = _FACTS.get(code)
    out = {
        "code": code, "name": card.name, "hub_class": card.hub_class,
        "profile": profile,
        "rank_in_hub_class": card.rank, "composite": card.composite,
        "percentiles_within_hub_class": {k: v for k, v in card.kpis.items()
                                         if isinstance(v, (int, float))},
        "proxy_labels": {k: lbl for k, lbl in _PROXY_LABELS.items()
                         if k in card.kpis},
        "flags": card.flags,
        "missing": card.missing,
        "note": note or None,
    }
    if fact is not None:
        out["raw"] = _raw_metrics(fact)
        out["confidence"] = _confidence(card, fact)
        out["scope"] = _scope_notes(card, fact)
    return out


def _confidence(card, f) -> dict:
    """Data-coverage confidence signal. Missing KPIs and missing delay data lower it.

    Deliberately coarse (high/medium/low) rather than a bogus decimal — the underlying
    coverage is discrete, and a false-precision number here would look sourced when it is not.
    """
    total_kpi_slots = 5      # weight-carrying KPIs per profile
    missing_kpis = len(card.missing)
    has_delays = f.nas_delay_share is not None or f.mean_taxi_out_min is not None
    if missing_kpis == 0 and has_delays:
        level = "high"
    elif missing_kpis >= 2 or not has_delays:
        level = "low"
    else:
        level = "medium"
    return {
        "level": level,
        "missing_kpis": card.missing,
        "has_delay_data": has_delays,
        "note": ("Airports below the delay snapshot's 500-flight floor lose NAS-share and "
                 "taxi-out entirely; their composite is renormalised across whatever KPIs "
                 "remain and should not be compared to a full-data airport without saying so."),
    }


def compare_airports(airports: list[str], profile: str = _DEFAULT_PROFILE) -> dict:
    """Side-by-side percentiles for several airports."""
    out, notes = [], []
    for a in airports:
        m = get_airport_metrics(a, profile=profile)
        if m.get("note"):
            notes.append(m.pop("note"))
        out.append(m)
    return {"airports": out, "notes": notes,
            "caveat": ("Percentiles are within each airport's own hub class, so a medium hub "
                       "at the 90th percentile is not busier than a large hub at the 50th — "
                       "it is more constrained relative to its peers.")}


def list_region(region: str) -> dict:
    """Which airports a region name covers, and the definition used.

    Accepts US state names (spelled out) and multi-state groupings. State abbreviations are
    deliberately not accepted here to avoid collisions with metro shorthand (LA, OR, IN);
    'Louisiana' resolves, 'LA' resolves to the Los Angeles metro via resolve_airport.
    """
    try:
        codes, note = resolve.resolve_region(region)
    except ValueError as e:
        return {"error": f"no definition on file for region {region!r}",
                "detail": str(e),
                "supported": ["individual US state names (Florida, Texas, New York, ...)",
                              "New England"],
                "hint": ("Spell out the state name. Two-letter abbreviations are ambiguous "
                         "in a system that also handles metro areas.")}
    available = [c for c in codes if c in _CARDS]
    excluded = [c for c in codes if c not in _CARDS]
    return {"region": region, "definition": note,
            "airports": available,
            "excluded_no_data": excluded,
            "coverage_note": (
                "Membership is deterministic from the AIRPORT_STATE table. Airports in this "
                "state that are not in the scoring universe are listed under excluded_no_data "
                f"({len(excluded)} shown); do not treat their absence as evidence they do not "
                "exist."
                if excluded else
                "Membership is deterministic from the AIRPORT_STATE table."
            )}


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


def get_stage_length_mix(airport: str) -> dict:
    """How far this airport's departures actually fly, and what share are long haul.

    Returns TWO long-haul thresholds on purpose. There is no legal definition of "long
    haul", and for many airports the answer roughly doubles between the two — ANC is 11.6%
    at 2,500 sm and 24.4% at 1,500 sm. Handing back one number would hide that the
    threshold, not the airport, was doing the work.
    """
    try:
        code, note = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous) as e:
        return {"error": f"could not resolve {airport!r}", "detail": str(e)}
    f = _FACTS.get(code)
    s = getattr(f, "stage_length", None) if f else None
    if not s:
        return {"error": f"no stage-length data for {code}"}

    out = {
        "code": code,
        "departures_measured": s["departures_with_distance"],
        "mean_stage_length_statute_miles": s["mean_stage_length_sm"],
        "share_by_band": s["bands_share"],
        "long_haul_share": {
            "at_2500_statute_miles": s["long_haul_share_2500sm"],
            "at_1500_statute_miles": s["long_haul_share_1500sm"],
        },
        "definition_note": ("There is no single definition of long haul. 2,500 sm is roughly "
                            "the 4,000 km ICAO convention; 1,500 sm is the looser commercial "
                            "usage. Report which threshold you used."),
        "scope_warning": ("DOMESTIC flights by reporting US carriers only. International "
                          "departures are NOT counted."),
    }
    if code == "ANC":
        out["airport_specific_warning"] = (
            "Anchorage is one of the world's largest CARGO hubs, and its genuinely long-haul "
            "traffic is international freight to Asia — none of which is in this data. This "
            "figure describes ANC's domestic passenger operation only. Do not present it as "
            "the share of all flying at Anchorage.")
    if note:
        out["note"] = note
    return out


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

    Profiles are COMPOSITE scores. For a plain "which airports are growing fastest" question,
    use rank_by_passenger_growth or rank_by_flight_growth instead — growth is a raw rate and
    a composite that only weights growth at ~0.15-0.20 will not answer the question asked.

    `airports` exists because the natural region flow is list_region -> rank those codes.
    Without it the model could only rank a hub class, so a New England query returned BOS
    alone and reported the others as absent from the rankings - technically true, and a
    worse answer than the one it replaced.
    """
    if profile not in ("congestion", "terminal_expansion"):
        return {"error": f"unknown profile {profile!r}",
                "hint": ("For growth or size questions, use rank_by_passenger_growth or "
                         "rank_by_flight_growth. Composite profiles weight growth at "
                         "~0.15-0.20 and do not answer growth questions directly.")}

    pool = _cards_for(profile)     # the requested profile's cards, not whatever was bound
    if airports:
        wanted = set()
        for a in airports:
            try:
                wanted.add(resolve.resolve_airport(a, allow_primary=True)[0])
            except (ValueError, resolve.Ambiguous):
                continue
        rows = [c for c in pool.values() if c.code in wanted and c.composite is not None]
    else:
        rows = [c for c in pool.values()
                if (hub_class is None or c.hub_class == hub_class)
                and c.composite is not None]
    rows.sort(key=lambda c: (-(c.composite or 0), c.code))
    return {
        "profile": profile, "hub_class": hub_class or "all",
        "methodology_note": (
            "Composite is a weighted blend of percentiles WITHIN each airport's hub class. "
            "That is fine for ranking WITHIN a class, and fine for the specific-set case where "
            "the set is roughly like-for-like. Do NOT rank across hub classes with a single "
            "composite — a large-hub composite is not on the same scale as a medium-hub "
            "composite. If you must, name every airport's class and treat the ordering as "
            "class-aware rather than absolute."
        ),
        "cross_class_warning": (hub_class is None and not airports),
        "explanation_hint": ("Quote the top_drivers of the winner. Do not describe a ranking "
                             "with generic language ('driven by congestion and growth') if the "
                             "drivers below say otherwise."),
        "results": [{"rank_in_hub_class": c.rank, "code": c.code, "name": c.name,
                     "hub_class": c.hub_class, "composite": c.composite,
                     "top_drivers": dict(list(c.contributions.items())[:3]),
                     "flags": c.flags, "missing": c.missing}
                    for c in rows[:limit]]}


def _hub_class_filter(hub_class: str | None):
    """Return a predicate over facts."""
    if not hub_class:
        return lambda f: True
    return lambda f: (_CARDS.get(f.code) and _CARDS[f.code].hub_class == hub_class)


def rank_by_passenger_growth(hub_class: str | None = None, top_n: int = 10) -> dict:
    """Rank airports by RAW 2-year passenger growth. Not a percentile, not a composite.

    Growth is a rate, so it does not have the size-proxy problem that raw passenger counts do.
    Ranking directly on the rate is honest across hub classes, but a hub_class filter is still
    offered because 'medium hubs growing fastest' is a common shape of question.
    """
    keep = _hub_class_filter(hub_class)
    rows = []
    for code, f in _FACTS.items():
        if not keep(f):
            continue
        if not f.passengers_ttm or not f.passengers_2y_ago or f.passengers_2y_ago <= 0:
            continue
        g = (f.passengers_ttm / f.passengers_2y_ago) - 1.0
        rows.append((code, f.name, g))
    rows.sort(key=lambda r: (-r[2], r[0]))
    return {
        "metric": "raw two-year passenger growth (ttm vs. two years prior)",
        "hub_class": hub_class or "all",
        "unit": "fractional change (0.10 = +10%)",
        "note": ("This is a RAW growth rate, not a percentile or composite. Quote the "
                 "fraction as returned. Growth here is passenger-based; if the user asked "
                 "about FLIGHT growth, use rank_by_flight_growth instead."),
        "results": [{"code": c, "name": n, "passenger_growth_2y": round(g, 4),
                     "hub_class": (_CARDS.get(c).hub_class if _CARDS.get(c) else "unknown")}
                    for c, n, g in rows[:top_n]],
    }


def rank_by_flight_growth(hub_class: str | None = None, top_n: int = 10) -> dict:
    """Rank airports by RAW 2-year departure (flight) growth.

    Distinct from passenger growth: an airport whose passenger count is rising while
    departures are flat is upgauging (bigger planes, same slots) rather than adding flights,
    which is a different capacity story. Never present flight growth as passenger growth.
    """
    keep = _hub_class_filter(hub_class)
    rows = []
    for code, f in _FACTS.items():
        if not keep(f):
            continue
        if not f.departures_ttm or not f.departures_2y_ago or f.departures_2y_ago <= 0:
            continue
        g = (f.departures_ttm / f.departures_2y_ago) - 1.0
        rows.append((code, f.name, g))
    rows.sort(key=lambda r: (-r[2], r[0]))
    return {
        "metric": "raw two-year departure growth (ttm vs. two years prior)",
        "hub_class": hub_class or "all",
        "unit": "fractional change (0.10 = +10%)",
        "note": ("RAW departure growth. Not a percentile, not gate_saturation or "
                 "airside_saturation — those are congestion proxies, not flight-count "
                 "growth. Do not substitute."),
        "results": [{"code": c, "name": n, "flight_growth_2y": round(g, 4),
                     "hub_class": (_CARDS.get(c).hub_class if _CARDS.get(c) else "unknown")}
                    for c, n, g in rows[:top_n]],
    }


def compare_growth(hub_class: str | None = None, top_n: int = 15) -> dict:
    """Passenger growth vs. flight growth, side by side.

    Serves the exact question 'high passenger growth but relatively low flight growth': the
    two numbers are what the question is asking about, so returning both prevents the model
    from folding them into a gate_saturation percentile that has a different meaning.
    """
    keep = _hub_class_filter(hub_class)
    rows = []
    for code, f in _FACTS.items():
        if not keep(f):
            continue
        pg = None
        if f.passengers_ttm and f.passengers_2y_ago and f.passengers_2y_ago > 0:
            pg = (f.passengers_ttm / f.passengers_2y_ago) - 1.0
        fg = None
        if f.departures_ttm and f.departures_2y_ago and f.departures_2y_ago > 0:
            fg = (f.departures_ttm / f.departures_2y_ago) - 1.0
        if pg is None and fg is None:
            continue
        gap = (pg - fg) if (pg is not None and fg is not None) else None
        rows.append((code, f.name, pg, fg, gap))
    # Rank by (pax growth - flight growth) descending, so airports absorbing traffic via
    # upgauging rather than more flights surface first.
    rows.sort(key=lambda r: (-(r[4] if r[4] is not None else -1), r[0]))
    return {
        "metric": "passenger_growth_2y and flight_growth_2y, both RAW",
        "hub_class": hub_class or "all",
        "note": ("A positive gap means passenger growth outpaces flight growth — carriers "
                 "are moving more people without adding flights (upgauging), often the "
                 "fingerprint of a gate or slot constraint. Say 'upgauging' rather than "
                 "'high gate_saturation', which is a proxy percentile."),
        "results": [{"code": c, "name": n,
                     "passenger_growth_2y": (round(pg, 4) if pg is not None else None),
                     "flight_growth_2y": (round(fg, 4) if fg is not None else None),
                     "gap_pax_minus_flight": (round(gap, 4) if gap is not None else None),
                     "hub_class": (_CARDS.get(c).hub_class if _CARDS.get(c) else "unknown")}
                    for c, n, pg, fg, gap in rows[:top_n]],
    }


def get_delay_per_passenger(airport: str) -> dict:
    """Total delay minutes per 1,000 passengers, from BTS On-Time Performance.

    Serves the 'delay relative to passenger volume' question directly rather than pointing
    at NAS delay share, which is a CAUSE mix (airspace vs carrier vs weather) and does not
    normalise for size. Returns raw minutes and the derived per-1k rate, so the caller can
    quote whichever the user actually asked for.

    Missing data: BGR and BTV fall below the 500-flight monthly floor and have no delay row.
    They get an explicit refusal rather than a silent zero.
    """
    try:
        code, _ = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous):
        return {"error": f"could not resolve {airport!r}"}
    f = _FACTS.get(code)
    if not f:
        return {"error": f"no data for {code}"}
    if f.total_delay_minutes is None:
        return {"error": (f"no delay data for {code}. The BTS On-Time snapshot requires 500 "
                          f"flights in the month; airports below that threshold (e.g. BGR, "
                          f"BTV) do not have delay figures. Confidence for this airport is "
                          f"low on any delay-based question."),
                "code": code,
                "data_period_delays": f.delay_period}
    if not f.passengers_ttm or f.passengers_ttm <= 0:
        return {"error": f"no passenger volume for {code}"}
    per_1k = f.total_delay_minutes / (f.passengers_ttm / 1000.0)
    return {
        "code": code,
        "total_delay_minutes": f.total_delay_minutes,
        "passengers_ttm": f.passengers_ttm,
        "delay_minutes_per_1k_passengers": round(per_1k, 3),
        "data_period_delays": f.delay_period,
        "scope": ("Delay figures cover ONE month and DOMESTIC flights only. Passenger "
                  "volume is trailing-twelve-month total. The ratio therefore mixes a "
                  "one-month delay figure with a twelve-month volume figure; treat the "
                  "figure as a coarse per-passenger rate for that one month, not an "
                  "annualised number."),
    }


def get_cargo_intensity(airport: str) -> dict:
    """Freight lbs and freight lbs per passenger, from BTS T-100.

    Purpose: distinguish an airport's cargo footprint from its passenger footprint, so ANC
    and similar cargo-heavy airports are not evaluated as if they were purely passenger hubs.
    Cargo intensity is descriptive only — it is deliberately NOT scored, because a
    passenger-terminal recommendation should not be distorted by cargo volume.
    """
    try:
        code, _ = resolve.resolve_airport(airport, allow_primary=True)
    except (ValueError, resolve.Ambiguous):
        return {"error": f"could not resolve {airport!r}"}
    f = _FACTS.get(code)
    if not f:
        return {"error": f"no data for {code}"}
    if f.freight_lbs_ttm is None:
        return {"code": code, "freight_lbs_ttm": None,
                "note": ("No freight_lbs reported in the T-100 snapshot for this airport. "
                         "Do not substitute zero.")}
    per_pax = (f.freight_lbs_ttm / f.passengers_ttm) if f.passengers_ttm else None
    return {
        "code": code,
        "freight_lbs_ttm": int(f.freight_lbs_ttm),
        "passengers_ttm": f.passengers_ttm,
        "freight_lbs_per_passenger": round(per_pax, 2) if per_pax is not None else None,
        "note": ("Descriptive metric. Cargo intensity is not part of the terminal or "
                 "congestion score — a cargo hub does not thereby need a passenger "
                 "terminal. Use this to contextualise passenger-only figures at cargo-"
                 "heavy airports (ANC in particular)."),
    }


REGISTRY: dict[str, Callable] = {
    "get_airport_metrics": get_airport_metrics,
    "compare_airports": compare_airports,
    "list_region": list_region,
    "get_delay_breakdown": get_delay_breakdown,
    "estimate_unmet_demand": estimate_unmet_demand,
    "get_stage_length_mix": get_stage_length_mix,
    "rank_airports": rank_airports,
    "rank_by_passenger_growth": rank_by_passenger_growth,
    "rank_by_flight_growth": rank_by_flight_growth,
    "compare_growth": compare_growth,
    "get_delay_per_passenger": get_delay_per_passenger,
    "get_cargo_intensity": get_cargo_intensity,
}

# OpenAI-style schemas. Kept next to the implementations so the two cannot drift.
SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_airport_metrics",
        "description": ("All computed KPI percentiles, rank and flags for one airport. "
                        "Composite and rank are profile-specific: pass profile="
                        "'terminal_expansion' for terminal/gate questions (default is "
                        "'congestion')."),
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string", "description": "IATA code or city name, e.g. SFO or Santa Ana"},
            "profile": {"type": "string", "enum": ["congestion", "terminal_expansion"]}},
            "required": ["airport"]}}},
    {"type": "function", "function": {
        "name": "compare_airports",
        "description": ("Side-by-side metrics for two or more airports. Pass profile="
                        "'terminal_expansion' for terminal/gate comparisons (default "
                        "'congestion')."),
        "parameters": {"type": "object", "properties": {
            "airports": {"type": "array", "items": {"type": "string"}},
            "profile": {"type": "string", "enum": ["congestion", "terminal_expansion"]}},
            "required": ["airports"]}}},
    {"type": "function", "function": {
        "name": "list_region",
        "description": ("Which airports a region covers, and the definition used. Handles "
                        "US STATES (spell out the state name: 'Florida', 'Texas', 'New York') "
                        "and multi-state groupings ('New England'). MUST be called before "
                        "ranking a named region so the membership is explicit — never let "
                        "the model itself decide which airports are in a state."),
        "parameters": {"type": "object", "properties": {
            "region": {"type": "string",
                       "description": "e.g. 'Florida', 'Texas', 'California', 'New England'"}},
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
        "name": "get_stage_length_mix",
        "description": ("How far an airport's departures fly, and the long-haul share at two "
                        "thresholds. USE THIS for any question about long haul, stage length, "
                        "sector length or flight distance. Do not estimate these yourself."),
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string"}}, "required": ["airport"]}}},
    {"type": "function", "function": {
        "name": "rank_airports",
        "description": ("Ranked shortlist by a COMPOSITE profile. Use for 'expansion "
                        "candidates', 'most capacity-constrained', 'best modernisation "
                        "targets'. Do NOT use for growth questions — see rank_by_passenger_"
                        "growth and rank_by_flight_growth. Pass `airports` to rank a specific "
                        "set (use after list_region), or `hub_class` for a whole class."),
        "parameters": {"type": "object", "properties": {
            "profile": {"type": "string", "enum": ["congestion", "terminal_expansion"]},
            "hub_class": {"type": "string", "enum": ["large", "medium", "small", "nonhub"],
                          "description": "Omit to rank across all classes."},
            "airports": {"type": "array", "items": {"type": "string"},
                         "description": "Specific airports to rank. Use this after list_region."},
            "limit": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "rank_by_passenger_growth",
        "description": ("Airports ranked by RAW two-year passenger growth. USE THIS for "
                        "'which airports are growing fastest', 'top growing airports', "
                        "'fastest growing medium hubs'. Not a percentile, not a composite."),
        "parameters": {"type": "object", "properties": {
            "hub_class": {"type": "string", "enum": ["large", "medium", "small", "nonhub"]},
            "top_n": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "rank_by_flight_growth",
        "description": ("Airports ranked by RAW two-year DEPARTURE growth. USE THIS whenever "
                        "the user says 'flight growth', 'departure growth', or 'operations "
                        "growth'. Distinct from passenger growth."),
        "parameters": {"type": "object", "properties": {
            "hub_class": {"type": "string", "enum": ["large", "medium", "small", "nonhub"]},
            "top_n": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "compare_growth",
        "description": ("Passenger growth AND flight growth side by side, sorted by the gap. "
                        "USE THIS for 'high passenger growth but low flight growth' or any "
                        "wording that contrasts the two rates."),
        "parameters": {"type": "object", "properties": {
            "hub_class": {"type": "string", "enum": ["large", "medium", "small", "nonhub"]},
            "top_n": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_delay_per_passenger",
        "description": ("Total delay minutes per 1,000 passengers for one airport. USE THIS "
                        "for 'delay rate relative to passenger volume' or similar. Do NOT "
                        "substitute NAS delay share, which is a cause mix, not a per-"
                        "passenger rate. Returns an error if the airport is below the "
                        "delay-snapshot flight floor."),
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string"}}, "required": ["airport"]}}},
    {"type": "function", "function": {
        "name": "get_cargo_intensity",
        "description": ("Freight lbs and freight lbs per passenger for one airport. Use for "
                        "cargo hubs (ANC especially) to distinguish cargo footprint from "
                        "passenger footprint. Descriptive only — not part of the score."),
        "parameters": {"type": "object", "properties": {
            "airport": {"type": "string"}}, "required": ["airport"]}}},
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
