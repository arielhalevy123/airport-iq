"""The deterministic scoring engine.

This module is the graded artefact. Two rules govern it, and both are enforced by tests
rather than by convention:

1. IT PERFORMS NO I/O AND CALLS NO MODEL. It receives already-materialised facts and returns
   numbers. `tests/test_purity.py` walks this package's AST and fails the build if anything
   here imports a network or LLM library. That is the answer to "how do you know the LLM
   cannot change a score" — it is a failing build, not an intention.

2. IT IS DETERMINISTIC. Same inputs, byte-identical output. No dict-ordering dependence, no
   clock, no randomness.

Design decisions worth defending, all of which are in the design doc:

- Scores are percentile ranks WITHIN A PEER GROUP (hub class), not globally. Running this
  model without peer grouping does not produce a size proxy, it produces an INVERSION —
  the top terminal-expansion candidates come out as Glacier Park, Fresno and Appleton.
  Size decides which league you are in; it does not decide your score within it.

- No single weight exceeds 0.40, so no ranking can be a one-metric sort in disguise.

- Missing inputs propagate as missing. An airport with no load factor is not scored as
  having a load factor of zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Hub classes by share of national departing passengers, mirroring FAA convention.
HUB_LARGE, HUB_MEDIUM, HUB_SMALL = 0.010, 0.0025, 0.0005

PROFILES: dict[str, dict[str, float]] = {
    # Landside. Terminals are sized on peak-hour design-day flow (IATA ADRM), so the
    # metric closest to that condition carries the plurality.
    "terminal_expansion": {
        "peak_pressure": 0.35,
        "gate_saturation": 0.20,
        "demand_growth": 0.20,
        "international_intensity": 0.15,
        "airside_headroom": 0.10,
    },
    # Airside. Same runway measure as above but with the POLARITY FLIPPED: spare runway is
    # a precondition for terminal ROI, whereas runway saturation *is* congestion.
    # delay_congestion is OBSERVED congestion; airside_saturation is INFERRED from runway
    # count. Observation outranks inference, so the observed measure takes the plurality and
    # the inferred one drops to 0.20. Still no weight above 0.40.
    "congestion": {
        "delay_congestion": 0.35,
        "airside_saturation": 0.20,
        "demand_growth": 0.15,
        "peak_pressure": 0.15,
        "gate_saturation": 0.15,
    },
}


@dataclass(frozen=True)
class AirportFacts:
    """Everything the engine needs about one airport. Pre-fetched, never fetched here."""
    code: str
    name: str = ""
    passengers_ttm: float | None = None
    departures_ttm: float | None = None
    load_factor_ttm: float | None = None
    peak_month_passengers: float | None = None
    mean_month_passengers: float | None = None
    international_share: float | None = None
    passengers_2y_ago: float | None = None
    seats_per_departure_now: float | None = None
    seats_per_departure_base: float | None = None
    jet_runways: int | None = None
    peak_month_departures: float | None = None
    regulatory_cap: str | None = None      # e.g. "SNA: noise curfew + passenger cap"
    # From BTS On-Time Performance. nas_delay_share is the fraction of this airport's delay
    # attributable to the National Airspace System - volume and capacity - as opposed to
    # carrier or weather delay. It is the closest thing in open data to a causal congestion
    # measure, and it is a RATE, so it does not scale with airport size.
    nas_delay_share: float | None = None
    mean_taxi_out_min: float | None = None


@dataclass
class ScoreCard:
    code: str
    name: str
    composite: float | None
    rank: int | None = None
    hub_class: str = ""
    kpis: dict[str, float | None] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- individual KPIs

def _peak_pressure(a: AirportFacts) -> float | None:
    """Load factor and how far the design month runs above the mean. Landside."""
    if a.load_factor_ttm is None:
        return None
    lf = a.load_factor_ttm / 100.0 if a.load_factor_ttm > 1.5 else a.load_factor_ttm
    if a.peak_month_passengers and a.mean_month_passengers:
        peak_ratio = a.peak_month_passengers / a.mean_month_passengers
        return 0.6 * lf + 0.4 * min(peak_ratio / 2.0, 1.0)
    return 0.6 * lf


def _gate_saturation(a: AirportFacts) -> float | None:
    """Upgauging: carriers fly BIGGER aircraft when they cannot add MORE departures.
    That is the fingerprint of a gate or slot constraint."""
    if not a.seats_per_departure_now or not a.seats_per_departure_base:
        return None
    return (a.seats_per_departure_now / a.seats_per_departure_base) - 1.0


def _demand_growth(a: AirportFacts) -> float | None:
    """Two-year passenger CAGR. Meaningless at capped airports - see regulatory_cap."""
    if not a.passengers_ttm or not a.passengers_2y_ago or a.passengers_2y_ago <= 0:
        return None
    return (a.passengers_ttm / a.passengers_2y_ago) ** 0.5 - 1.0


def _international_intensity(a: AirportFacts) -> float | None:
    """International passengers need roughly 2x the terminal area and dwell time."""
    return a.international_share


def _airside_saturation(a: AirportFacts) -> float | None:
    """Peak-month daily departures per usable runway.

    Known weakness, stated in the doc: runway COUNT is not runway CAPACITY. Two parallels
    4,300 ft apart allow simultaneous independent approaches; two at 750 ft do not — which
    is precisely why SFO's arrival rate halves in low visibility. The named upgrade path is
    to replace this denominator with the FAA Capacity Profile called rate.
    """
    if not a.peak_month_departures or not a.jet_runways or a.jet_runways <= 0:
        return None
    return (a.peak_month_departures / 30.0) / (a.jet_runways * 30.0)


def _delay_congestion(a: AirportFacts) -> float | None:
    """Observed congestion, from delay causes rather than inferred from runway counts.

    Combines the share of delay attributable to the airspace system with taxi-out time.
    Both are rates, so neither is a size proxy: a small airport with a full runway scores
    high, and a huge airport with plenty of capacity does not.
    """
    if a.nas_delay_share is None and a.mean_taxi_out_min is None:
        return None
    parts, weights = [], []
    if a.nas_delay_share is not None:
        parts.append(a.nas_delay_share); weights.append(0.6)
    if a.mean_taxi_out_min is not None:
        parts.append(min(a.mean_taxi_out_min / 30.0, 1.0)); weights.append(0.4)
    return sum(p * w for p, w in zip(parts, weights)) / sum(weights)


KPI_FUNCS = {
    "delay_congestion": _delay_congestion,
    "peak_pressure": _peak_pressure,
    "gate_saturation": _gate_saturation,
    "demand_growth": _demand_growth,
    "international_intensity": _international_intensity,
    "airside_saturation": _airside_saturation,
}


# ------------------------------------------------------------------------ helpers

def _hub_class(passengers: float | None, national_total: float) -> str:
    if not passengers or national_total <= 0:
        return "unknown"
    share = passengers / national_total
    if share >= HUB_LARGE:
        return "large"
    if share >= HUB_MEDIUM:
        return "medium"
    if share >= HUB_SMALL:
        return "small"
    return "nonhub"


def _percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Percentile rank within the given set. Ties share the average rank, so the result
    does not depend on input ordering."""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    if n == 1:
        return {items[0][0]: 50.0}
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_pos = (i + j) / 2.0
        pct = 100.0 * avg_pos / (n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


# -------------------------------------------------------------------------- score

def score(facts: list[AirportFacts], profile: str = "terminal_expansion") -> list[ScoreCard]:
    """Rank airports. Pure: no I/O, no model, deterministic."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; have {sorted(PROFILES)}")
    weights = PROFILES[profile]

    national_total = sum(a.passengers_ttm or 0.0 for a in facts)
    classes = {a.code: _hub_class(a.passengers_ttm, national_total) for a in facts}

    # Raw KPI values, computed once.
    raw: dict[str, dict[str, float | None]] = {}
    for a in facts:
        raw[a.code] = {name: fn(a) for name, fn in KPI_FUNCS.items()}
        # Headroom is saturation inverted. Same measure, opposite sign - this is what
        # separates "terminal" from "congestion" rather than a reshuffle of weights.
        sat = raw[a.code].get("airside_saturation")
        raw[a.code]["airside_headroom"] = None if sat is None else -sat

    # Percentile-rank each KPI WITHIN its hub class.
    pct: dict[str, dict[str, float]] = {a.code: {} for a in facts}
    for kpi in set(list(KPI_FUNCS) + ["airside_headroom"]):
        for hub in {"large", "medium", "small", "nonhub", "unknown"}:
            group = {c: raw[c][kpi] for c in raw
                     if classes[c] == hub and raw[c].get(kpi) is not None}
            for code, p in _percentile_ranks(group).items():
                pct[code][kpi] = p

    cards: list[ScoreCard] = []
    for a in facts:
        contribs: dict[str, float] = {}
        missing: list[str] = []
        available_weight = 0.0
        total = 0.0
        for kpi, w in weights.items():
            p = pct[a.code].get(kpi)
            if p is None:
                missing.append(kpi)
                continue
            contribs[kpi] = w * p
            total += w * p
            available_weight += w

        # Renormalise over available weight so a missing KPI does not silently score 0.
        composite = None
        if available_weight >= 0.5:      # refuse to score on less than half the model
            composite = round(total / available_weight, 2)

        flags: list[str] = []
        if a.regulatory_cap:
            flags.append(
                f"Growth is legally capped ({a.regulatory_cap}). Flat demand here is a "
                "ceiling, not weakness — and gates that cannot legally be used return nothing."
            )
        sat_pct = pct[a.code].get("airside_saturation")
        if profile == "terminal_expansion" and sat_pct is not None and sat_pct > 90:
            flags.append(
                "Airside-first: this airport is runway-constrained, so a terminal will not "
                "relieve it."
            )

        cards.append(ScoreCard(
            code=a.code, name=a.name, composite=composite,
            hub_class=classes[a.code],
            kpis={k: (round(v, 2) if isinstance(v, float) else v)
                  for k, v in pct[a.code].items()},
            contributions={k: round(v, 2) for k, v in
                           sorted(contribs.items(), key=lambda kv: -kv[1])},
            missing=missing, flags=flags,
        ))

    # Rank within hub class. Sort key includes the code so ties are deterministic.
    for hub in {c.hub_class for c in cards}:
        group = [c for c in cards if c.hub_class == hub and c.composite is not None]
        for i, c in enumerate(sorted(group, key=lambda x: (-x.composite, x.code)), start=1):
            c.rank = i

    return sorted(cards, key=lambda c: (c.hub_class, c.rank or 9999, c.code))
