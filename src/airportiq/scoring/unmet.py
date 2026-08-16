"""Unmet demand: a proxy, stated as one.

"What is the unmet flight demand at SFO and why?" is the hardest of the four questions in the
brief, and the one that separates a real answer from a plausible one.

THE HONEST STARTING POINT
Unmet demand is fundamentally **unobservable**. Demand that was suppressed — the passenger who
did not book because the fare was high, the airline that did not add a frequency because no slot
existed — never appears in any dataset. Every number here is therefore a proxy against a stated
counterfactual, and saying so is part of the answer rather than a hedge attached to it.

Refusing the question outright is also wrong. We can construct a defensible estimate from what
is observable, provided we say exactly what it assumes.

THE METHOD
Three observable signals, each capturing a different way constrained demand leaks into the data:

  1. Load factor above a comfort threshold. Airlines target roughly 80-85%; sustained operation
     above that means seats are scarce and marginal passengers are being priced out.
  2. Airspace-attributable delay. When NAS delay dominates the mix, the schedule already exceeds
     what the airfield reliably delivers — the airport is selling capacity it cannot fly.
  3. Upgauging without frequency growth. Carriers flying larger aircraft on a flat number of
     departures is the fingerprint of a slot or gate ceiling: they want more capacity and cannot
     get more movements.

Each is expressed as a percentage of current traffic, then combined. The output is a RANGE, not
a point estimate, because a point estimate would imply a precision the method does not have.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Industry planning comfort threshold. Above this, load factor stops being efficiency and
# starts being scarcity.
LF_COMFORT = 0.82

# Fraction of NAS-attributable delay treated as suppressed demand rather than inefficiency.
# Deliberately conservative: not all delay represents demand that would otherwise have flown.
NAS_TO_DEMAND = 0.5


@dataclass
class UnmetDemand:
    code: str
    name: str
    low_pct: float | None = None
    high_pct: float | None = None
    low_pax: float | None = None
    high_pax: float | None = None
    mechanism: list[str] = field(default_factory=list)
    method: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    confidence: str = "low"


# Structural mechanisms that explain WHY demand goes unmet at specific airports. Hand-curated
# with the physical cause named, because "high demand and limited capacity" is not an
# explanation - it restates the question.
MECHANISMS = {
    "SFO": ("The two parallel runway pairs are roughly 750 ft apart — too close for "
            "simultaneous independent approaches under instrument conditions. When the marine "
            "layer moves in, the arrival rate roughly halves and the published schedule cannot "
            "be flown. The constraint is meteorological and geometric, not terminal."),
    "LGA": ("Slot-controlled (FAA Level 3) with a perimeter rule. Demand is capped by "
            "regulation before it is capped by concrete."),
    "JFK": ("Slot-controlled (FAA Level 3). International widebody banking concentrates demand "
            "into narrow windows that the airfield cannot widen."),
    "EWR": ("Schedule-facilitated (FAA Level 2) and sharing the most congested airspace in the "
            "country with JFK and LGA. Airspace, not the airport, is the binding constraint."),
    "SNA": ("A noise curfew and a court-settlement passenger cap. The ceiling is legal: demand "
            "is turned away by ordinance, not by a shortage of gates."),
    "DCA": ("Slot controls plus the 1,250-mile perimeter rule, which suppresses long-haul demand "
            "by statute rather than by capacity."),
    "SEA": ("Three runways carrying large-hub volumes, with terrain and airspace limiting "
            "expansion options."),
    "BOS": ("A constrained site with converging runway geometry; configuration changes with wind "
            "cut usable capacity sharply."),
}


def estimate(card, facts) -> UnmetDemand:
    """Estimate suppressed demand at one airport. Pure: no I/O, no model."""
    u = UnmetDemand(code=card.code, name=card.name)

    lf = facts.load_factor_ttm
    if lf is not None and lf > 1.5:
        lf = lf / 100.0

    signals: list[float] = []

    # 1. load factor above comfort
    if lf is not None:
        if lf > LF_COMFORT:
            excess = (lf - LF_COMFORT) / LF_COMFORT
            signals.append(excess)
            u.method.append(
                f"load factor {lf:.1%} against a {LF_COMFORT:.0%} planning comfort level "
                f"→ {excess:.1%} of traffic implies seats sold beyond comfortable capacity"
            )
        else:
            u.method.append(
                f"load factor {lf:.1%} is at or below the {LF_COMFORT:.0%} comfort level — "
                f"no scarcity signal from seat occupancy"
            )

    # 2. airspace-attributable delay
    nas = facts.nas_delay_share
    if nas is not None:
        contribution = nas * NAS_TO_DEMAND
        signals.append(contribution)
        u.method.append(
            f"{nas:.1%} of delay is airspace-attributable; at a {NAS_TO_DEMAND:.0%} "
            f"pass-through this implies {contribution:.1%} of schedule not reliably deliverable"
        )

    # 3. upgauging without frequency growth
    now, base = facts.seats_per_departure_now, facts.seats_per_departure_base
    if now and base:
        upgauge = (now / base) - 1.0
        if upgauge > 0.02:
            signals.append(upgauge)
            u.method.append(
                f"average aircraft gauge up {upgauge:.1%} against the base period — carriers "
                f"adding seats per movement, the signature of a movement ceiling"
            )

    if not signals:
        u.caveats.append("No signal available: insufficient data for even a proxy estimate.")
        return u

    centre = sum(signals) / len(signals)
    # A wide band, deliberately. The inputs are proxies; a narrow range would overstate.
    u.low_pct, u.high_pct = max(centre * 0.6, 0.0), centre * 1.6

    if facts.passengers_ttm:
        u.low_pax = facts.passengers_ttm * u.low_pct
        u.high_pax = facts.passengers_ttm * u.high_pct

    mech = MECHANISMS.get(card.code)
    if mech:
        u.mechanism.append(mech)
        u.confidence = "medium"
    else:
        u.mechanism.append(
            "No airport-specific structural mechanism is on file. The estimate rests on the "
            "observable signals alone, which show that demand is constrained without "
            "identifying the physical cause."
        )

    u.caveats.append(
        "Unmet demand is not directly observable — suppressed demand never enters the data. "
        "This is a proxy against a stated counterfactual, not a measurement."
    )
    u.caveats.append(
        "The estimate assumes constrained demand persists rather than diverting permanently "
        "to another airport or mode."
    )
    if card.missing:
        u.caveats.append(f"computed without {', '.join(card.missing)}")
        u.confidence = "low"

    return u


def render(u: UnmetDemand) -> str:
    """A readable answer. Deterministic - the LLM is not needed to state a number."""
    if u.low_pct is None:
        return f"{u.code}: {u.caveats[0] if u.caveats else 'no estimate available'}"

    lines = [f"{u.code} — estimated unmet demand: {u.low_pct:.0%} to {u.high_pct:.0%} "
             f"of current traffic"]
    if u.low_pax:
        lines.append(f"  roughly {u.low_pax/1e6:.1f}M to {u.high_pax/1e6:.1f}M passengers a year, "
                     f"at {u.confidence} confidence")
    lines.append("\nWhy (mechanism):")
    lines += [f"  {m}" for m in u.mechanism]
    lines.append("\nHow the estimate is built:")
    lines += [f"  - {m}" for m in u.method]
    lines.append("\nWhat this is not:")
    lines += [f"  - {c}" for c in u.caveats]
    return "\n".join(lines)
