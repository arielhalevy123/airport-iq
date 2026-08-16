"""Deterministic entity resolution.

This is deliberately NOT the LLM's job. The model extracts the surface string the user typed
("New England", "LA", "Santa Ana"); this module maps it to airport codes. A model guessing here
produces a silently wrong answer — quietly including Albany in New England, or resolving "LA"
to LAX alone while the user meant the basin — and a silently wrong answer is worse than a
refusal, because nobody checks it.

Every mapping below is a stated assumption that surfaces in the response.
"""
from __future__ import annotations

NEW_ENGLAND = {"CT", "ME", "MA", "NH", "RI", "VT"}

# Airport -> state. In the full build this comes from OurAirports iso_region; kept explicit
# here so the demo is self-contained and the assumption is auditable.
AIRPORT_STATE = {
    "BOS": "MA", "BDL": "CT", "PVD": "RI", "MHT": "NH", "PWM": "ME", "BTV": "VT",
    "BGR": "ME", "ORH": "MA", "HYA": "MA", "ACK": "MA", "MVY": "MA",
    "JFK": "NY", "LGA": "NY", "EWR": "NJ", "ALB": "NY", "BUF": "NY", "SYR": "NY",
    "LAX": "CA", "BUR": "CA", "LGB": "CA", "ONT": "CA", "SNA": "CA", "SAN": "CA",
    "SFO": "CA", "OAK": "CA", "SJC": "CA", "SMF": "CA", "FAT": "CA",
    "SEA": "WA", "PDX": "OR", "DEN": "CO", "PHX": "AZ", "LAS": "NV", "SLC": "UT",
    "ORD": "IL", "MDW": "IL", "DTW": "MI", "MSP": "MN", "STL": "MO", "MCI": "MO",
    "IND": "IN", "CVG": "KY", "CLE": "OH", "PIT": "PA", "PHL": "PA", "MKE": "WI",
    "ATL": "GA", "MIA": "FL", "FLL": "FL", "MCO": "FL", "TPA": "FL", "RSW": "FL",
    "PBI": "FL", "JAX": "FL", "CLT": "NC", "RDU": "NC", "BNA": "TN", "MEM": "TN",
    "DFW": "TX", "DAL": "TX", "IAH": "TX", "HOU": "TX", "AUS": "TX", "SAT": "TX",
    "IAD": "VA", "DCA": "VA", "ORF": "VA", "BWI": "MD", "ANC": "AK", "HNL": "HI",
}

# Metro groupings. "LA" is genuinely ambiguous, so we say so rather than pick silently.
METRO = {
    "la": ["LAX", "BUR", "LGB", "ONT", "SNA"],
    "los angeles": ["LAX", "BUR", "LGB", "ONT", "SNA"],
    "bay area": ["SFO", "OAK", "SJC"],
    "new york": ["JFK", "LGA", "EWR"],
    "nyc": ["JFK", "LGA", "EWR"],
    "washington": ["DCA", "IAD", "BWI"],
    "chicago": ["ORD", "MDW"],
    "dallas": ["DFW", "DAL"],
    "houston": ["IAH", "HOU"],
    "miami": ["MIA", "FLL", "PBI"],
}

# Primary airport of a metro, used when the user clearly means one airport.
METRO_PRIMARY = {"la": "LAX", "los angeles": "LAX", "new york": "JFK", "nyc": "JFK",
                 "bay area": "SFO", "washington": "DCA", "chicago": "ORD",
                 "dallas": "DFW", "houston": "IAH", "miami": "MIA"}

CITY_ALIAS = {
    "santa ana": "SNA", "orange county": "SNA", "john wayne": "SNA",
    "burbank": "BUR", "long beach": "LGB", "ontario": "ONT",
    "san francisco": "SFO", "oakland": "OAK", "san jose": "SJC",
    "boston": "BOS", "logan": "BOS", "hartford": "BDL", "providence": "PVD",
    "manchester": "MHT", "portland maine": "PWM", "burlington": "BTV", "bangor": "BGR",
    "portland": "PDX", "seattle": "SEA", "newark": "EWR", "dulles": "IAD",
    "reagan": "DCA", "national": "DCA", "anchorage": "ANC", "atlanta": "ATL",
    "denver": "DEN", "phoenix": "PHX", "vegas": "LAS", "las vegas": "LAS",
    "boston logan": "BOS", "bradley": "BDL", "t f green": "BDL", "tf green": "PVD",
    "green": "PVD", "jetport": "PWM", "portland jetport": "PWM",
    "kennedy": "JFK", "jfk": "JFK", "laguardia": "LGA", "o'hare": "ORD", "ohare": "ORD",
    "midway": "MDW", "sea-tac": "SEA", "seatac": "SEA", "sky harbor": "PHX",
    "hartsfield": "ATL", "hartsfield-jackson": "ATL", "dulles": "IAD",
    "fort lauderdale": "FLL", "ft lauderdale": "FLL", "west palm beach": "PBI",
}


class Ambiguous(Exception):
    """Raised when a surface string maps to several airports and the caller must choose."""

    def __init__(self, term: str, candidates: list[str]):
        self.term, self.candidates = term, candidates
        super().__init__(f"{term!r} could mean any of {candidates}")


def resolve_region(term: str) -> tuple[list[str], str]:
    """Region name -> airport codes, plus the assumption to show the user."""
    t = " ".join(term.strip().lower().split())
    if t in {"new england", "ניו אינגלנד"}:
        codes = sorted(c for c, s in AIRPORT_STATE.items() if s in NEW_ENGLAND)
        return codes, ("New England = CT, ME, MA, NH, RI, VT. "
                       "Albany NY is excluded — it is not a New England state.")
    raise ValueError(f"unknown region {term!r}")


# Words users attach to airport names that carry no identifying information. Stripping them
# is deterministic normalisation, not guessing — "Santa Ana airport" and "Santa Ana" are the
# same request, and failing to match the first is a bug that reads as missing data.
_NOISE = ("international airport", "regional airport", "airport", "international",
          "regional", "intl", "field", "the ")


def _normalise(term: str) -> str:
    t = " ".join(term.strip().lower().split())
    for w in _NOISE:
        t = t.replace(w, " ")
    return " ".join(t.split())


def resolve_airport(term: str, *, allow_primary: bool = False) -> tuple[str, str]:
    """Surface string -> a single airport code, plus the assumption made.

    Raises Ambiguous rather than guessing when the term covers a metro area.
    """
    t = _normalise(term)

    if t.upper() in AIRPORT_STATE:
        return t.upper(), ""

    if t in CITY_ALIAS:
        return CITY_ALIAS[t], ""

    if t in METRO:
        if not allow_primary:
            raise Ambiguous(term, METRO[t])
        primary = METRO_PRIMARY[t]
        return primary, (f"'{term}' is ambiguous — the metro area includes "
                         f"{', '.join(METRO[t])}. Interpreting as {primary}. "
                         f"Ask to compare the full metroplex if that is what you meant.")

    raise ValueError(f"could not resolve {term!r} to an airport")


def resolve_many(terms: list[str]) -> tuple[list[str], list[str]]:
    """Resolve several terms, collecting assumptions. Unknown terms are refused, not invented."""
    codes: list[str] = []
    notes: list[str] = []
    for t in terms:
        try:
            code, note = resolve_airport(t, allow_primary=True)
            codes.append(code)
            if note:
                notes.append(note)
        except ValueError:
            notes.append(f"'{t}' is not an airport I have data for — excluded rather than guessed.")
    return codes, notes
