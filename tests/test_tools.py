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


# --------------------------------------------------------------- stage length

class _Facts:
    """Minimal stand-in for AirportFacts; the tool only reads .stage_length."""
    def __init__(self, stage_length=None):
        self.stage_length = stage_length


_ANC_STAGE = {
    "departures_with_distance": 1333,
    "mean_stage_length_sm": 1232.3,
    "bands_share": {"lt_500": 0.2386, "500_999": 0.1718, "1000_1499": 0.3458,
                    "1500_2499": 0.1275, "ge_2500": 0.1163},
    "long_haul_share_2500sm": 0.1163,
    "long_haul_share_1500sm": 0.2440,
    "scope": "domestic flights by reporting US carriers only",
}


def test_stage_length_returns_both_thresholds():
    """One long-haul number would hide that the threshold is doing the work: for ANC the
    answer roughly doubles between the two, so both must always be returned."""
    _bind()
    tools.bind(tools._CARDS.values(), {"ANC": _Facts(_ANC_STAGE)})
    out = json.loads(tools.call("get_stage_length_mix", '{"airport": "Anchorage"}'))
    lh = out["long_haul_share"]
    assert lh["at_2500_statute_miles"] == 0.1163
    assert lh["at_1500_statute_miles"] == 0.2440
    assert lh["at_1500_statute_miles"] > lh["at_2500_statute_miles"]


def test_stage_length_bands_sum_to_one():
    """A band set that does not sum to 1 means departures were dropped or double counted."""
    _bind()
    tools.bind(tools._CARDS.values(), {"ANC": _Facts(_ANC_STAGE)})
    out = json.loads(tools.call("get_stage_length_mix", '{"airport": "ANC"}'))
    assert abs(sum(out["share_by_band"].values()) - 1.0) < 0.001


def test_anchorage_cargo_caveat_is_forced():
    """ANC's real long-haul flying is international freight, which is absent from this
    source. Answering the brief's Anchorage question without that caveat is misleading,
    so the tool emits it rather than hoping the model remembers."""
    _bind()
    tools.bind(tools._CARDS.values(), {"ANC": _Facts(_ANC_STAGE)})
    out = json.loads(tools.call("get_stage_length_mix", '{"airport": "ANC"}'))
    assert "cargo" in out["airport_specific_warning"].lower()
    assert "DOMESTIC" in out["scope_warning"]


def test_stage_length_missing_data_is_refused_not_guessed():
    _bind()
    tools.bind(tools._CARDS.values(), {"SFO": _Facts(None)})
    out = json.loads(tools.call("get_stage_length_mix", '{"airport": "SFO"}'))
    assert "error" in out


# ------------------------------------------------------- growth vs expansion routing

class _GrowthFacts:
    """Only the attributes read by rank_by_*_growth / compare_growth. Keeping this a plain
    class rather than the real dataclass keeps the test focused on the tool contract."""
    def __init__(self, code, name, pax_ttm, pax_2y, dep_ttm=None, dep_2y=None,
                 total_delay=None, freight=None):
        self.code = code
        self.name = name
        self.passengers_ttm = pax_ttm
        self.passengers_2y_ago = pax_2y
        self.departures_ttm = dep_ttm
        self.departures_2y_ago = dep_2y
        self.load_factor_ttm = None
        self.international_share = None
        self.seats_per_departure_now = None
        self.seats_per_departure_base = None
        self.nas_delay_share = None
        self.mean_taxi_out_min = None
        self.freight_lbs_ttm = freight
        self.total_delay_minutes = total_delay
        self.jet_runways = None
        self.delay_period = "2026-04"


def _bind_growth():
    # Real IATA codes so resolve_airport does not refuse them. The scenario mapping:
    #   SFO  +20% pax, +5%  flights → upgauging (pax growth outpaces flights)
    #   BOS  +10% pax, +12% flights → adding flights
    #   PDX  -5%  pax, -3%  flights → declining
    cards = [
        ScoreCard(code="SFO", name="San Francisco", composite=70.0, rank=1, hub_class="medium",
                  kpis={}, contributions={}, missing=[], flags=[]),
        ScoreCard(code="BOS", name="Boston Logan", composite=60.0, rank=2, hub_class="medium",
                  kpis={}, contributions={}, missing=[], flags=[]),
        ScoreCard(code="PDX", name="Portland OR", composite=50.0, rank=3, hub_class="large",
                  kpis={}, contributions={}, missing=[], flags=[]),
    ]
    facts = {
        "SFO": _GrowthFacts("SFO", "San Francisco", 1200, 1000, 1050, 1000, total_delay=50_000),
        "BOS": _GrowthFacts("BOS", "Boston Logan", 1100, 1000, 1120, 1000, total_delay=None),
        "PDX": _GrowthFacts("PDX", "Portland OR", 950, 1000, 970, 1000, total_delay=200_000),
    }
    tools.bind(cards, facts)


def test_passenger_growth_tool_returns_raw_rate_not_percentile():
    """The user asking 'which airports are growing fastest' must get the growth rate itself,
    not its percentile rank. A percentile is a compressed view and cannot answer 'how much'."""
    _bind_growth()
    out = json.loads(tools.call("rank_by_passenger_growth", '{"hub_class": "medium"}'))
    codes = [r["code"] for r in out["results"]]
    assert codes[0] == "SFO" and codes[1] == "BOS"
    # Growth is a fraction — not a percentile in the 0-100 range.
    assert out["results"][0]["passenger_growth_2y"] == 0.2
    assert "raw" in out["metric"].lower()
    assert "percentile" not in json.dumps(out["results"]).lower()


def test_flight_growth_is_distinct_from_passenger_growth():
    """Passenger growth and flight growth are separate quantities. An airport upgauging
    (more people, same flights) must not be misreported as 'flight growth'."""
    _bind_growth()
    fg = json.loads(tools.call("rank_by_flight_growth", '{"hub_class": "medium"}'))
    codes = [r["code"] for r in fg["results"]]
    assert codes[0] == "BOS" and codes[1] == "SFO"
    assert fg["results"][0]["flight_growth_2y"] == 0.12
    assert fg["results"][1]["flight_growth_2y"] == 0.05


def test_compare_growth_surfaces_the_gap():
    """The exact query the brief flagged: 'high passenger growth but low flight growth'.
    SFO must top the list because its gap (20% - 5% = 15 pts) is the largest."""
    _bind_growth()
    out = json.loads(tools.call("compare_growth", "{}"))
    top = out["results"][0]
    assert top["code"] == "SFO"
    assert top["passenger_growth_2y"] == 0.2
    assert top["flight_growth_2y"] == 0.05
    assert top["gap_pax_minus_flight"] == 0.15


def test_rank_airports_refuses_growth_profile_with_a_hint():
    """A growth question routed to rank_airports is a semantic mismatch. The tool must
    refuse rather than run a composite that only weights growth at ~15-20% — that would
    quietly answer a different question."""
    _bind_growth()
    out = json.loads(tools.call("rank_airports", '{"profile": "growth"}'))
    assert "error" in out
    assert "growth" in out.get("hint", "").lower()


# ----------------------------------------------------------- delay per passenger

def test_delay_per_passenger_returns_the_actual_ratio():
    """The 'delay rate relative to passenger volume' question must be answered with the
    ratio itself, not with a generic congestion composite or the NAS cause-mix."""
    _bind_growth()
    out = json.loads(tools.call("get_delay_per_passenger", '{"airport": "SFO"}'))
    # 50_000 minutes / (1200 pax / 1000) = 41,666.67 per 1k pax at the fixture's scale
    assert abs(out["delay_minutes_per_1k_passengers"] - 41666.67) < 0.1
    assert out["total_delay_minutes"] == 50_000
    assert out["passengers_ttm"] == 1200


def test_delay_per_passenger_refuses_when_the_airport_has_no_delay_row():
    """Airports below the 500-flight monthly floor (BGR, BTV) have no delay data. The tool
    must say so rather than substitute zero, which would rank them as the least-delayed."""
    _bind_growth()
    out = json.loads(tools.call("get_delay_per_passenger", '{"airport": "BOS"}'))
    assert "error" in out
    assert "500" in out["error"] or "delay" in out["error"].lower()


# --------------------------------------------------------------- confidence / missing data

def test_missing_delay_data_lowers_confidence():
    """An airport without delay data must not be presented as equally confident to a
    complete-data airport. Anything else silently equates BGR with SFO."""
    cards = [
        ScoreCard(code="SFO", name="San Francisco", composite=70.0, rank=1, hub_class="large",
                  kpis={"delay_congestion": 87.0, "peak_pressure": 77.0}, contributions={},
                  missing=[], flags=[]),
        ScoreCard(code="BGR", name="Bangor", composite=55.0, rank=2, hub_class="nonhub",
                  kpis={"peak_pressure": 60.0}, contributions={},
                  missing=["delay_congestion", "airside_saturation"], flags=[]),
    ]
    facts = {
        "SFO": _GrowthFacts("SFO", "San Francisco", 1000, 900, 800, 780, total_delay=10_000),
        "BGR": _GrowthFacts("BGR", "Bangor", 500, 480, 400, 400, total_delay=None),
    }
    facts["SFO"].nas_delay_share = 0.42
    facts["BGR"].nas_delay_share = None
    tools.bind(cards, facts)
    full = json.loads(tools.call("get_airport_metrics", '{"airport": "SFO"}'))
    lean = json.loads(tools.call("get_airport_metrics", '{"airport": "BGR"}'))
    assert full["confidence"]["level"] == "high"
    assert lean["confidence"]["level"] == "low"
    assert lean["confidence"]["has_delay_data"] is False


# --------------------------------------------------------------- freight passthrough

def test_freight_is_available_at_the_facts_layer():
    """Freight was collected in T-100 and dropped before the model could ever see it. It
    must reach the facts and be exposed through a tool, so cargo hubs like ANC can be
    characterised as such."""
    _bind_growth()
    tools._FACTS["SFO"].freight_lbs_ttm = 5_000_000
    out = json.loads(tools.call("get_cargo_intensity", '{"airport": "SFO"}'))
    assert out["freight_lbs_ttm"] == 5_000_000
    assert out["freight_lbs_per_passenger"] is not None


# --------------------------------------------------------------- cross-hub-class warning

def test_rank_airports_flags_cross_class_ranking():
    """Ranking across hub classes with within-class percentiles is mathematically misleading.
    The tool must emit an explicit warning so the model does not present the ordering as a
    single scale."""
    _bind_growth()
    out = json.loads(tools.call("rank_airports", "{}"))
    assert out["cross_class_warning"] is True
    assert "hub" in out["methodology_note"].lower()


# --------------------------------------------------------------- state region

def test_list_region_handles_us_states():
    """State-based region queries must resolve to the state's airports, deterministically."""
    _bind_growth()
    out = json.loads(tools.call("list_region", '{"region": "Florida"}'))
    # Growth fixtures only bind A/B/C; the resolver still names the FL airports and marks
    # them as excluded (no scoring data), so the query is answered honestly rather than
    # falling through as an unknown region.
    assert "error" not in out
    assert "excluded_no_data" in out
    assert "MCO" in out["excluded_no_data"]
