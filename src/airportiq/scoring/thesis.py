"""Turn a score into an investment thesis.

A ranking answers "who is third". An investor asks "where do I put the money, on what
building, and what would change your mind". Those are different questions, and a percentile
does not answer the second one.

This module converts a ScoreCard into a structured thesis:

  constraint type   landside / airside / regulatory  — this decides WHAT you build
  evidence for      the numbers that support acting
  evidence against  the numbers that argue the other way
  falsifier         what observation would overturn the conclusion
  recommendation    the intervention that actually follows

The evidence-against field is the important one. Anything can produce a ranked list; a system
that argues against its own recommendation is one an analyst can actually use, and it is the
difference between a dashboard and a colleague.

Still pure: same rules as engine.py, enforced by the same AST test.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Thesis:
    code: str
    name: str
    constraint: str                        # landside | airside | regulatory | mixed | unclear
    headline: str
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    falsifier: str = ""
    recommendation: str = ""
    confidence: str = "medium"             # high | medium | low


# Percentile thresholds. Named rather than inline so they are visible and arguable — a
# reviewer should be able to disagree with a number without reading the logic around it.
HIGH, LOW = 70.0, 30.0


def _ordinal(n: float) -> str:
    """87th, 81st, 83rd. '81th' in a client-facing report is small but it is the kind of
    detail that makes a reader wonder what else was not checked."""
    i = int(round(n))
    if 10 <= i % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")
    return f"{i}{suffix}"


def _pct(card, kpi: str) -> float | None:
    v = card.kpis.get(kpi)
    return v if isinstance(v, (int, float)) else None


def build(card, facts=None) -> Thesis:
    """Derive a thesis from an already-scored card. No I/O, no model."""
    k = card.kpis
    landside = [v for v in (_pct(card, "peak_pressure"), _pct(card, "gate_saturation"),
                            _pct(card, "international_intensity")) if v is not None]
    land_score = sum(landside) / len(landside) if landside else None

    # Airside is weighted toward OBSERVED delay over INFERRED runway saturation, for the
    # same reason the congestion profile is: runway COUNT is a poor proxy for runway
    # CAPACITY. SFO is the worked example - four runways looks like plenty, but the two
    # parallel pairs are ~750 ft apart, too close for simultaneous independent approaches
    # in low visibility, so the real arrival rate roughly halves when the marine layer
    # arrives. Counting runways scores SFO as uncongested; measuring delay does not.
    observed = _pct(card, "delay_congestion")
    inferred = _pct(card, "airside_saturation")
    if observed is not None and inferred is not None:
        air_score = 0.7 * observed + 0.3 * inferred
    else:
        air_score = observed if observed is not None else inferred

    regulatory = any("legally capped" in f for f in card.flags)

    # Constraint type. This is the decision that determines what gets built, so it is
    # derived explicitly rather than left implicit in a composite score.
    if regulatory:
        constraint = "regulatory"
    elif land_score is None or air_score is None:
        constraint = "unclear"
    elif air_score >= HIGH and land_score < HIGH:
        constraint = "airside"
    elif land_score >= HIGH and air_score < HIGH:
        constraint = "landside"
    elif land_score >= HIGH and air_score >= HIGH:
        constraint = "mixed"
    else:
        constraint = "unclear"

    t = Thesis(code=card.code, name=card.name, constraint=constraint, headline="")

    # --- evidence for -----------------------------------------------------------------
    for kpi, label in (
        ("peak_pressure", "terminal peak pressure"),
        ("gate_saturation", "gate saturation (carriers upgauging rather than adding flights)"),
        ("delay_congestion", "observed congestion (airspace-attributable delay and taxi-out)"),
        ("airside_saturation", "runway saturation"),
        ("demand_growth", "demand growth"),
        ("international_intensity", "international share (roughly 2x terminal area per passenger)"),
    ):
        v = _pct(card, kpi)
        if v is not None and v >= HIGH:
            t.evidence_for.append(f"{label} at the {_ordinal(v)} percentile of its hub class")

    # --- evidence against -------------------------------------------------------------
    # The part that makes this usable. A recommendation with no counter-case is a sales pitch.
    for kpi, label in (
        ("demand_growth", "demand growth"),
        ("peak_pressure", "terminal peak pressure"),
        ("delay_congestion", "observed congestion"),
        ("gate_saturation", "gate saturation"),
    ):
        v = _pct(card, kpi)
        if v is not None and v <= LOW:
            t.evidence_against.append(
                f"{label} is only at the {_ordinal(v)} percentile — weak support for acting here"
            )

    if card.missing:
        t.evidence_against.append(
            f"scored without {', '.join(card.missing)} — the picture is incomplete"
        )

    # --- headline, recommendation, falsifier ------------------------------------------
    if constraint == "regulatory":
        cap = next((f for f in card.flags if "legally capped" in f), "")
        t.headline = (f"{card.code}'s ceiling is legal, not physical. Capacity spend does not "
                      f"lift a cap.")
        t.recommendation = ("Slot or cap negotiation, or a landside retrofit that raises revenue "
                            "per passenger where passenger count is fixed. Do not fund gates that "
                            "cannot legally be used.")
        t.falsifier = "The cap being lifted or renegotiated would change this entirely."
        # Use the flag verbatim. Splitting on "(" produced fragments like
        # "FAA Level 3 slots). Flat demand here is..." which reads as a bug.
        if cap:
            t.evidence_against.append(cap)

    elif constraint == "airside":
        t.headline = (f"{card.code} is runway-constrained. A terminal will not relieve it.")
        t.recommendation = ("Airside investment — runway geometry, taxiway throughput, approach "
                            "procedures. Terminal spend here buys capacity the airfield cannot use.")
        t.falsifier = ("If delay is concentrated in weather rather than airspace volume, the "
                       "airside case weakens — check the NAS share against the weather share.")

    elif constraint == "landside":
        t.headline = f"{card.code} is landside-constrained: the terminal is the bottleneck."
        t.recommendation = ("Terminal and gate capacity — holdrooms, processing, and international "
                            "facilities if the international share is high.")
        t.falsifier = ("Published gate counts would test this directly; we infer it from upgauging "
                       "because no free structured gate data exists.")

    elif constraint == "mixed":
        t.headline = (f"{card.code} is constrained on both sides — sequencing matters more than "
                      f"selection.")
        t.recommendation = ("Airside first. Terminal capacity added ahead of runway capacity "
                            "produces gates that cannot be turned.")
        t.falsifier = "If runway headroom is larger than measured, the terminal could lead."

    else:
        t.headline = f"{card.code} shows no dominant constraint in this data."
        t.recommendation = ("Not a priority candidate on these metrics. Revisit with gate counts "
                            "and peak-hour data.")
        t.falsifier = "Peak-hour design-day data could reveal pressure that monthly data averages away."

    # --- confidence -------------------------------------------------------------------
    if card.missing or constraint == "unclear":
        t.confidence = "low"
    elif len(t.evidence_for) >= 3 and not t.evidence_against:
        t.confidence = "high"

    return t


def portfolio(cards: list, top_n: int = 5) -> list[Thesis]:
    """Theses for the highest-ranked airports, which is the shortlist a client actually wants."""
    ranked = [c for c in cards if c.composite is not None]
    ranked.sort(key=lambda c: (-(c.composite or 0), c.code))
    return [build(c) for c in ranked[:top_n]]
