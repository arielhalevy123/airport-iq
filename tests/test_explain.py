"""Every score the UI shows must be explainable: what it is, and how this number was built.

The explain layer turns a ScoreCard + AirportFacts into per-stat derivations with the
airport's REAL inputs plugged into the REAL formula. If the tooltip and the engine can
disagree, the tooltip is worse than nothing, so these tests pin the derivations to the
same inputs the engine used.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.scoring.engine import AirportFacts, ScoreCard
from airportiq.scoring import explain as ex


def _facts(**kw):
    base = dict(
        code="TST", name="Test Intl",
        passengers_ttm=10_000_000.0, departures_ttm=80_000.0,
        load_factor_ttm=87.3,
        peak_month_passengers=1_100_000.0, mean_month_passengers=833_333.0,
        international_share=0.21,
        passengers_2y_ago=9_000_000.0, departures_2y_ago=78_000.0,
        seats_per_departure_now=148.2, seats_per_departure_base=141.1,
        jet_runways=4, peak_month_departures=7_500.0,
        nas_delay_share=0.325, mean_taxi_out_min=25.8,
        total_delay_minutes=500_000, delay_period="2026-04",
    )
    base.update(kw)
    return AirportFacts(**base)


def _card(**kw):
    base = dict(
        code="TST", name="Test Intl", composite=75.2, rank=2, hub_class="large",
        kpis={"delay_congestion": 82.0, "peak_pressure": 79.0, "gate_saturation": 87.0,
              "demand_growth": 55.0, "international_intensity": 60.0,
              "airside_saturation": 71.0, "airside_headroom": 29.0},
        contributions={"delay_congestion": 28.7, "airside_saturation": 14.2,
                       "peak_pressure": 11.9},
        missing=[], flags=[],
    )
    base.update(kw)
    return ScoreCard(**base)


def _peers(n=28, hub_class="large"):
    """n peer cards in the same hub class, all carrying every KPI and a composite."""
    kpis = {"delay_congestion": 50.0, "peak_pressure": 50.0, "gate_saturation": 50.0,
            "demand_growth": 50.0, "international_intensity": 50.0,
            "airside_saturation": 50.0, "airside_headroom": 50.0}
    return [ScoreCard(code=f"P{i:02d}", name=f"Peer {i}", composite=50.0, rank=i + 1,
                      hub_class=hub_class, kpis=dict(kpis)) for i in range(n)]


def test_every_percentiled_kpi_gets_an_explanation():
    card = _card()
    out = ex.explain(card, _facts(), _peers(), profile="congestion")
    assert set(out["kpis"]) == set(card.kpis), \
        "each KPI the card carries must be explained; none invented, none skipped"
    for name, e in out["kpis"].items():
        assert e["what"], f"{name} has no 'what'"
        assert e["how"], f"{name} has no derivation lines"


def test_gate_saturation_derivation_uses_the_real_inputs():
    out = ex.explain(_card(), _facts(), _peers(), profile="congestion")
    how = " ".join(out["kpis"]["gate_saturation"]["how"])
    assert "148.2" in how and "141.1" in how, "must show the actual seats/departure inputs"
    assert "+5.0%" in how, "must show the derived upgauge rate"
    assert "87th percentile" in how, "must show where that lands in the peer group"


def test_percentile_line_names_the_peer_group_and_its_size():
    # The card itself plus 28 peers = 29 large hubs carrying gate_saturation.
    out = ex.explain(_card(), _facts(), _peers(28), profile="congestion")
    how = " ".join(out["kpis"]["gate_saturation"]["how"])
    assert "29 large hubs" in how


def test_peer_count_ignores_other_hub_classes_and_missing_kpis():
    peers = _peers(10, "large") + _peers(5, "medium")
    del peers[0].kpis["gate_saturation"]      # a large hub scored without this KPI
    out = ex.explain(_card(), _facts(), peers, profile="congestion")
    how = " ".join(out["kpis"]["gate_saturation"]["how"])
    assert "10 large hubs" in how, "9 peers with the KPI + the card itself = 10"


def test_proxy_kpis_carry_the_shared_proxy_caveat():
    from airportiq.agent import tools
    out = ex.explain(_card(), _facts(), _peers(), profile="congestion")
    for kpi in ("gate_saturation", "airside_saturation", "airside_headroom", "peak_pressure"):
        assert out["kpis"][kpi]["caveat"] == ex.PROXY_LABELS[kpi]
    assert tools._PROXY_LABELS is ex.PROXY_LABELS, \
        "tools and tooltips must share ONE proxy wording so they cannot drift"


def test_delay_congestion_states_its_scope():
    out = ex.explain(_card(), _facts(), _peers(), profile="congestion")
    e = out["kpis"]["delay_congestion"]
    how = " ".join(e["how"])
    assert "32.5%" in how and "25.8" in how, "must show NAS share and taxi-out inputs"
    text = (e["caveat"] or "") + how
    assert "2026-04" in text, "must disclose the one-month period the delay data covers"
    assert "domestic" in text.lower(), "must disclose the domestic-only scope"


def test_composite_explains_weights_and_renormalisation_when_kpis_are_missing():
    card = _card(missing=["gate_saturation"])
    out = ex.explain(card, _facts(), _peers(), profile="congestion")
    comp = out["composite"]
    assert "congestion" in comp["what"], "must name the profile the blend belongs to"
    how = " ".join(comp["how"])
    assert "delay_congestion" in how and "0.35" in how, \
        "must show the actual weight x percentile contributions"
    assert "gate_saturation" in how, "a missing KPI must be named, not silently absent"


def test_rank_explains_the_peer_group():
    out = ex.explain(_card(), _facts(), _peers(28), profile="congestion")
    how = " ".join(out["rank"]["how"])
    assert "#2" in how and "29 large hubs" in how


def test_missing_raw_inputs_never_become_fake_numbers():
    """An airport with no gauge data cannot show a gauge derivation. The KPI is absent
    from the card in that case, so no entry may appear for it at all."""
    card = _card()
    del card.kpis["gate_saturation"]
    facts = _facts(seats_per_departure_now=None, seats_per_departure_base=None)
    out = ex.explain(card, facts, _peers(), profile="congestion")
    assert "gate_saturation" not in out["kpis"]




def test_every_stat_has_a_plain_words_summary():
    """The click-through window opens with a sentence a non-specialist (or a kid) can
    read. Jargon-only explanations fail the person the tooltip exists for."""
    out = ex.explain(_card(), _facts(), _peers(), profile="congestion")
    for name, e in list(out["kpis"].items()) + [("composite", out["composite"]),
                                                ("rank", out["rank"])]:
        assert e.get("simple"), f"{name} has no plain-words summary"
        assert "percentile" not in e["simple"].lower(), \
            f"{name}'s plain-words summary leans on jargon"


def test_every_stat_names_its_data_source():
    """'Where did this number come from' must be answerable without reading the code."""
    out = ex.explain(_card(), _facts(), _peers(), profile="congestion")
    for kpi in ("peak_pressure", "gate_saturation", "demand_growth"):
        assert "T-100" in out["kpis"][kpi]["source"], \
            f"{kpi} must name the BTS T-100 dataset"
    assert "On-Time Performance" in out["kpis"]["delay_congestion"]["source"]
    assert "2026-04" in out["kpis"]["delay_congestion"]["source"], \
        "the delay source must name the month it covers"
    assert out["composite"]["source"], "the composite must say what it is computed from"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all explain tests passed")
