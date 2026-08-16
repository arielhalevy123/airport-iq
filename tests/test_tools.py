"""Tools must return engine values, refuse cleanly, and never invent."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.agent import tools
from airportiq.scoring.engine import ScoreCard


def _bind():
    cards = [
        ScoreCard(code="SFO", name="San Francisco", composite=75.0, rank=4, hub_class="large",
                  kpis={"delay_congestion": 87.0, "peak_pressure": 77.0}, contributions={},
                  missing=[], flags=[]),
        ScoreCard(code="SNA", name="John Wayne", composite=60.0, rank=8, hub_class="medium",
                  kpis={"delay_congestion": 80.0}, contributions={}, missing=[],
                  flags=["Growth is legally capped (noise curfew)."]),
    ]
    tools.bind(cards, {})
    return cards


def test_schemas_match_the_registry():
    """A schema advertising a tool that does not exist is a runtime failure waiting."""
    _bind()
    advertised = {s["function"]["name"] for s in tools.SCHEMAS}
    assert advertised == set(tools.REGISTRY), (
        f"schema/registry mismatch: {advertised ^ set(tools.REGISTRY)}")


def test_unknown_tool_returns_an_error_not_an_exception():
    out = json.loads(tools.call("no_such_tool", "{}"))
    assert "error" in out


def test_bad_arguments_are_handled():
    _bind()
    out = json.loads(tools.call("get_airport_metrics", '{"wrong_key": "SFO"}'))
    assert "error" in out, "a bad argument must return an error, not raise"


def test_unknown_airport_is_refused():
    _bind()
    out = json.loads(tools.call("get_airport_metrics", '{"airport": "Atlantis"}'))
    assert "error" in out, "must refuse rather than invent metrics"


def test_flags_are_surfaced_to_the_model():
    """If the cap is not in the tool output, the model cannot mention it."""
    _bind()
    out = json.loads(tools.call("get_airport_metrics", '{"airport": "SNA"}'))
    assert any("capped" in f for f in out["flags"])


def test_ranking_a_specific_set_works():
    """The region flow is list_region -> rank those codes."""
    _bind()
    out = json.loads(tools.call("rank_airports", '{"airports": ["SFO", "SNA"]}'))
    assert {r["code"] for r in out["results"]} == {"SFO", "SNA"}
    assert all("hub_class" in r for r in out["results"]), \
        "hub class must be present or cross-class ranks get compared wrongly"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all tool tests passed")
