"""The thesis layer must argue against itself, and must not misread a legal cap."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.scoring.engine import ScoreCard
from airportiq.scoring.thesis import build, _ordinal


def _card(**kpis):
    flags = kpis.pop("_flags", [])
    return ScoreCard(code="TST", name="Test", composite=70.0, rank=1,
                     hub_class="large", kpis=kpis, contributions={}, missing=[], flags=flags)


def test_ordinals_are_correct():
    assert _ordinal(81) == "81st"
    assert _ordinal(82) == "82nd"
    assert _ordinal(83) == "83rd"
    assert _ordinal(87) == "87th"
    assert _ordinal(11) == "11th"   # not 11st
    assert _ordinal(12) == "12th"
    assert _ordinal(13) == "13th"
    assert _ordinal(100) == "100th"


def test_regulatory_cap_dominates_and_changes_the_recommendation():
    """A capped airport must never be told to build gates it cannot legally use."""
    t = build(_card(peak_pressure=95.0, gate_saturation=95.0, delay_congestion=95.0,
                    _flags=["Growth is legally capped (FAA Level 3 slots)."]))
    assert t.constraint == "regulatory"
    assert "cannot legally be used" in t.recommendation


def test_weak_metrics_appear_as_evidence_against():
    """A recommendation with no counter-case is a sales pitch."""
    t = build(_card(peak_pressure=85.0, gate_saturation=80.0,
                    international_intensity=75.0, demand_growth=10.0))
    assert any("demand growth" in e for e in t.evidence_against), \
        "a 10th-percentile metric must be surfaced against the thesis"


def test_missing_data_lowers_confidence():
    c = _card(peak_pressure=90.0, gate_saturation=90.0, international_intensity=90.0)
    c.missing = ["delay_congestion"]
    t = build(c)
    assert t.confidence == "low"
    assert any("incomplete" in e for e in t.evidence_against)


def test_observed_delay_outweighs_inferred_runway_count():
    """Runway COUNT underestimates constraint where runways are too close to use
    independently - SFO's parallels are ~750 ft apart. Observed delay must dominate."""
    high_delay_few_runways = build(_card(delay_congestion=95.0, airside_saturation=20.0,
                                         peak_pressure=40.0, gate_saturation=40.0,
                                         international_intensity=40.0))
    assert high_delay_few_runways.constraint == "airside", \
        "observed congestion must outweigh a low inferred runway-saturation figure"


def test_every_thesis_states_a_falsifier():
    for kpis in ({"peak_pressure": 90.0, "gate_saturation": 90.0, "international_intensity": 90.0},
                 {"delay_congestion": 90.0, "airside_saturation": 90.0},
                 {"peak_pressure": 10.0}):
        assert build(_card(**kpis)).falsifier, "a claim with no falsifier is not an analysis"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all thesis tests passed")
