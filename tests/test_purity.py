"""The scoring package must never be able to reach a model or the network.

This is the mechanical answer to the brief's requirement for "deterministic scoring or
ranking logic (not only LLM output)". The separation is not a convention or a comment —
it is a failing build. Walk the AST of every module under scoring/ and reject any import
that could introduce nondeterminism, a network call, or an LLM.

Run: python -m pytest tests/ -q     (or just: python tests/test_purity.py)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SCORING = Path(__file__).resolve().parents[1] / "src" / "airportiq" / "scoring"

FORBIDDEN = {
    # LLM / agent frameworks
    "openai", "anthropic", "langchain", "langgraph", "groq", "google",
    "transformers", "litellm",
    # network
    "httpx", "requests", "urllib", "urllib3", "aiohttp", "socket", "boto3",
    # nondeterminism
    "random", "secrets", "datetime", "time",
    # sibling packages that do I/O
    "airportiq.data", "airportiq.agent",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
    return found


def test_scoring_is_pure() -> None:
    violations: list[str] = []
    modules = sorted(SCORING.rglob("*.py"))
    assert modules, f"no modules found under {SCORING}"

    for path in modules:
        for imported in _imports(path):
            root = imported.split(".")[0]
            if root in FORBIDDEN or imported in FORBIDDEN:
                violations.append(f"{path.name} imports {imported!r}")

    assert not violations, (
        "The scoring engine must stay pure — no network, no LLM, no clock, no randomness.\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


def test_scoring_is_deterministic() -> None:
    """Same input twice must give byte-identical output, and input order must not matter."""
    sys.path.insert(0, str(SCORING.parents[1]))
    from airportiq.scoring.engine import AirportFacts, score

    facts = [
        AirportFacts(code="AAA", name="A", passengers_ttm=30e6, departures_ttm=300e3,
                     load_factor_ttm=84.0, peak_month_passengers=3.2e6,
                     mean_month_passengers=2.5e6, international_share=0.30,
                     passengers_2y_ago=27e6, seats_per_departure_now=150,
                     seats_per_departure_base=140, jet_runways=3,
                     peak_month_departures=28e3),
        AirportFacts(code="BBB", name="B", passengers_ttm=25e6, departures_ttm=280e3,
                     load_factor_ttm=79.0, peak_month_passengers=2.4e6,
                     mean_month_passengers=2.1e6, international_share=0.10,
                     passengers_2y_ago=24e6, seats_per_departure_now=138,
                     seats_per_departure_base=136, jet_runways=4,
                     peak_month_departures=24e3),
        AirportFacts(code="CCC", name="C", passengers_ttm=28e6, departures_ttm=250e3,
                     load_factor_ttm=88.0, peak_month_passengers=3.0e6,
                     mean_month_passengers=2.3e6, international_share=0.45,
                     passengers_2y_ago=23e6, seats_per_departure_now=160,
                     seats_per_departure_base=142, jet_runways=2,
                     peak_month_departures=26e3),
    ]

    a = score(facts, "terminal_expansion")
    b = score(facts, "terminal_expansion")
    assert [(c.code, c.composite, c.rank) for c in a] == \
           [(c.code, c.composite, c.rank) for c in b], "not deterministic across runs"

    shuffled = score(list(reversed(facts)), "terminal_expansion")
    assert {c.code: c.composite for c in a} == {c.code: c.composite for c in shuffled}, \
        "score depends on input ordering"


def test_missing_data_is_not_zero() -> None:
    """A missing KPI must not be scored as zero, which would rank a data gap as ideal."""
    sys.path.insert(0, str(SCORING.parents[1]))
    from airportiq.scoring.engine import AirportFacts, score

    blank = AirportFacts(code="ZZZ", name="no data")
    cards = score([blank], "terminal_expansion")
    assert cards[0].composite is None, "an airport with no data must not receive a score"
    assert cards[0].missing, "missing KPIs must be reported"


if __name__ == "__main__":
    test_scoring_is_pure()
    test_scoring_is_deterministic()
    test_missing_data_is_not_zero()
    print("all purity/determinism tests passed")
