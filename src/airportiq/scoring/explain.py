"""Per-stat derivations: what each score means, and how THIS airport's number was built.

The UI shows percentile bars. A percentile with no derivation is a number the reader must
take on faith, which is the exact failure this project exists to avoid. This module turns a
ScoreCard + AirportFacts into, for every stat shown, three parts:

    what     one sentence: what the stat measures and why it matters
    how      the airport's REAL inputs plugged into the REAL formula, ending with where
             that lands in the peer group
    caveat   the honesty label: proxy status, scope limits, or None

Same rules as the rest of this package, enforced by the same AST test: no I/O, no model,
no clock. Everything here is a function of the card, the facts and the peer set — so the
tooltip can never say something the engine did not compute.

PROXY_LABELS lives here (not in agent/tools.py) because both the tools and the tooltips
must quote ONE wording. Scoring cannot import from agent; agent already imports scoring.
"""
from __future__ import annotations

from .engine import PROFILES
from .thesis import _ordinal

# Which KPIs are direct measurements and which are proxies. Quoted verbatim by the agent
# tools AND by the UI tooltips, so the two cannot drift apart.
PROXY_LABELS = {
    "gate_saturation": ("PROXY: seats-per-departure change over two years, "
                        "not a physical gate count. Rising means carriers are upgauging, "
                        "which is the fingerprint of a gate or slot constraint."),
    "airside_saturation": ("PROXY: peak-month departures per usable runway. Runway COUNT, "
                           "not runway CAPACITY. Two closely spaced parallels do not equal "
                           "two independent runways in low visibility."),
    "airside_headroom": ("PROXY: inverse of airside_saturation. Same caveats apply."),
    "peak_pressure": ("PROXY: load-factor and peak-vs-mean month ratio. Not a facility-level "
                      "measurement of terminal crowding."),
}

_WHAT = {
    "delay_congestion": ("Observed congestion: the share of this airport's delay caused by "
                         "the airspace system (volume against capacity), blended with "
                         "taxi-out time. Measured from real flights, not inferred."),
    "peak_pressure": ("Terminal peak pressure: how full the seats are, plus how far the "
                      "busiest month runs above the average month. Terminals are sized on "
                      "peaks, not annual totals."),
    "gate_saturation": ("Gate/slot constraint signal: carriers flying bigger aircraft "
                        "without adding flights (upgauging) means they want more capacity "
                        "and cannot get more movements."),
    "demand_growth": "Two-year passenger growth, annualised.",
    "international_intensity": ("Share of passengers flying internationally. They need "
                                "roughly twice the terminal area and dwell time."),
    "airside_saturation": ("How hard the runways work: peak-month departures per "
                           "jet-capable runway."),
    "airside_headroom": ("Spare runway capacity, the inverse of airside saturation. Spare "
                         "runway is a precondition for a terminal investment to pay off."),
}

_HUB_NOUN = {"large": "large hubs", "medium": "medium hubs", "small": "small hubs",
             "nonhub": "non-hub airports", "unknown": "unclassified airports"}

# Plain-words summaries: one or two sentences a non-specialist can read without any of the
# project's vocabulary. No "percentile", no "composite", no "proxy" — the jargon-free door
# into the same number, never a different claim about it.
_SIMPLE = {
    "delay_congestion": ("Of all the minutes planes spent delayed here, we count how many "
                         "were caused by crowded skies and runways — not by weather and not "
                         "by the airline. Lots of crowding delay means the airport is "
                         "running out of room."),
    "peak_pressure": ("How full the planes are, and how much busier the busiest month is "
                      "than an ordinary month. Full planes in a very busy month mean a "
                      "crowded terminal."),
    "gate_saturation": ("Airlines here started using bigger planes instead of adding more "
                        "flights. That is what airlines do when there is no room for more "
                        "flights."),
    "demand_growth": "How much more passenger traffic there is now than two years ago.",
    "international_intensity": ("What share of the passengers are flying to or from other "
                                "countries. Those passengers need about twice as much "
                                "terminal space."),
    "airside_saturation": ("How many take-offs each runway handles in the busiest month. "
                           "More take-offs per runway means busier runways."),
    "airside_headroom": ("How much spare room the runways still have. It is runway "
                         "busyness turned upside down."),
    "composite": ("All the scores above mixed into one number, with the most important "
                  "ones counting more. Higher means a stronger case for investment."),
    "rank": ("This airport's place in line among airports of a similar size — first place "
             "means the strongest overall score in its group."),
}

# Where each number's raw data comes from. The click-through window leads with this,
# because "where did you get that" is the first honest question about any figure.
_T100_SOURCE = ("US Bureau of Transportation Statistics, T-100 reports — the traffic "
                "figures every airline is required to file monthly. We use the last 12 "
                "months, compared with the same 12 months two years earlier.")


def _source(kpi: str, facts) -> str:
    if kpi == "delay_congestion":
        period = facts.delay_period or "one recent month"
        return (f"US Bureau of Transportation Statistics, On-Time Performance — every "
                f"domestic flight's delay minutes and their causes, for {period}.")
    if kpi in ("airside_saturation", "airside_headroom"):
        return (_T100_SOURCE + " Runway counts come from a hand-checked table of "
                "jet-capable runways (5,000 ft or longer).")
    return _T100_SOURCE


def _peer_count(card, all_cards, key: str | None) -> int:
    """Peers in the card's hub class carrying this KPI (or, for key=None, a composite).
    The card itself always counts once, whether or not it appears in all_cards."""
    n = 0
    for c in all_cards:
        if c.hub_class != card.hub_class or c.code == card.code:
            continue
        if key is None:
            n += c.composite is not None
        else:
            n += isinstance(c.kpis.get(key), (int, float))
    return n + 1


def _pct_line(card, all_cards, kpi: str) -> str:
    p = card.kpis[kpi]
    n = _peer_count(card, all_cards, kpi)
    noun = _HUB_NOUN.get(card.hub_class, f"{card.hub_class} hubs")
    return f"→ {_ordinal(p)} percentile of {n} {noun}"


def _lf(facts) -> float | None:
    """Load factor normalised to a fraction, exactly as the engine does it."""
    lf = facts.load_factor_ttm
    if lf is None:
        return None
    return lf / 100.0 if lf > 1.5 else lf


def _kpi_how(kpi: str, card, facts, all_cards) -> list[str]:
    """The derivation lines for one KPI: real inputs, real formula, peer placement.
    Mirrors the formulas in engine.py; if a formula changes there, the test suite pins
    these lines to the same inputs, so drift shows up as a failure rather than a lie."""
    f = facts
    lines: list[str] = []

    if kpi == "delay_congestion":
        if f.nas_delay_share is not None:
            lines.append(f"airspace-system share of delay: {f.nas_delay_share:.1%}")
        if f.mean_taxi_out_min is not None:
            lines.append(f"mean taxi-out: {f.mean_taxi_out_min:g} min "
                         f"(scaled against a 30 min ceiling)")
        lines.append("raw score = 0.6 × airspace share + 0.4 × scaled taxi-out"
                     if f.nas_delay_share is not None and f.mean_taxi_out_min is not None
                     else "raw score uses the one signal available, at full weight")
        if f.delay_period:
            lines.append(f"delay data period: {f.delay_period} (one month)")

    elif kpi == "peak_pressure":
        lf = _lf(f)
        if lf is not None:
            lines.append(f"trailing-12-month load factor: {lf:.1%}")
        if f.peak_month_passengers and f.mean_month_passengers:
            ratio = f.peak_month_passengers / f.mean_month_passengers
            lines.append(f"peak month runs {ratio:.2f}× the mean month")
            lines.append("raw score = 0.6 × load factor + 0.4 × min(peak ratio ÷ 2, 1)")
        else:
            lines.append("no peak/mean month data — raw score = 0.6 × load factor")

    elif kpi == "gate_saturation":
        now, base = f.seats_per_departure_now, f.seats_per_departure_base
        if now and base:
            u = now / base - 1.0
            lines.append(f"seats per departure now: {now:.1f}")
            lines.append(f"seats per departure two years ago: {base:.1f}")
            lines.append(f"upgauging = {now:.1f} ÷ {base:.1f} − 1 = {u:+.1%}")

    elif kpi == "demand_growth":
        if f.passengers_ttm and f.passengers_2y_ago:
            g = (f.passengers_ttm / f.passengers_2y_ago) ** 0.5 - 1.0
            lines.append(f"passengers, trailing 12 months: {f.passengers_ttm:,.0f}")
            lines.append(f"passengers, two years earlier: {f.passengers_2y_ago:,.0f}")
            lines.append(f"annualised growth = √(ratio) − 1 = {g:+.1%} a year")

    elif kpi == "international_intensity":
        if f.international_share is not None:
            lines.append(f"international share of passengers: {f.international_share:.1%}")

    elif kpi in ("airside_saturation", "airside_headroom"):
        if f.peak_month_departures and f.jet_runways:
            daily = f.peak_month_departures / 30.0
            lines.append(f"peak-month departures: {f.peak_month_departures:,.0f} "
                         f"≈ {daily:,.0f} a day")
            lines.append(f"across {f.jet_runways} jet-capable runways "
                         f"≈ {daily / f.jet_runways:,.1f} daily departures per runway")
        if kpi == "airside_headroom":
            lines.append("headroom is that same measure inverted: low saturation = high headroom")

    lines.append(_pct_line(card, all_cards, kpi))
    return lines


def explain(card, facts, all_cards: list, profile: str) -> dict:
    """Every stat the scorecard shows, with its meaning and its derivation. Pure."""
    weights = PROFILES.get(profile, {})
    out: dict = {"kpis": {}, "composite": None, "rank": None}

    for kpi, v in card.kpis.items():
        if not isinstance(v, (int, float)):
            continue
        caveat = PROXY_LABELS.get(kpi)
        if kpi == "delay_congestion":
            caveat = ("Covers DOMESTIC flights by reporting US carriers, for one month "
                      "only. Not stable annual behaviour.")
        out["kpis"][kpi] = {
            "what": _WHAT.get(kpi, kpi.replace("_", " ")),
            "simple": _SIMPLE.get(kpi, _WHAT.get(kpi, kpi.replace("_", " "))),
            "source": _source(kpi, facts),
            "how": _kpi_how(kpi, card, facts, all_cards),
            "caveat": caveat,
        }

    if card.composite is not None:
        how = []
        available = 0.0
        for kpi, w in weights.items():
            if kpi in card.contributions:
                how.append(f"{kpi}: weight {w:g} × {card.kpis.get(kpi):g} pct "
                           f"= {card.contributions[kpi]:g}")
                available += w
            elif kpi in card.missing:
                how.append(f"{kpi}: missing — its {w:g} weight is excluded and the "
                           f"blend renormalised over the rest")
        total = sum(card.contributions.values())
        # ≈, not =: contributions are rounded to 2 dp on the card, so their sum can
        # differ from the composite in the last digit. Showing = would look like an error.
        how.append(f"composite = {total:g} (sum) ÷ {available:g} (weight with data) "
                   f"≈ {card.composite:g}")
        out["composite"] = {
            "what": (f"Weighted blend of the KPI percentiles under the '{profile}' "
                     f"profile. Comparable within one hub class only."),
            "simple": _SIMPLE["composite"],
            "source": ("Computed from the scores above — no extra data. The weights are "
                       "fixed in the scoring engine and published in the design document."),
            "how": how,
            "caveat": ("Percentiles are within this airport's hub class; do not compare "
                       "composites across hub classes."),
        }

    if card.rank is not None:
        n = _peer_count(card, all_cards, None)
        noun = _HUB_NOUN.get(card.hub_class, f"{card.hub_class} hubs")
        out["rank"] = {
            "what": "Position by composite score among peers in the same hub class.",
            "simple": _SIMPLE["rank"],
            "source": "Computed by sorting the composite scores — no extra data.",
            "how": [f"#{card.rank} of {n} {noun} by the '{profile}' composite"],
            "caveat": None,
        }

    return out
