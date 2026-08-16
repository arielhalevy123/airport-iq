"""Entity resolution must be forgiving of phrasing and unforgiving of nonsense."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.agent import resolve


def _r(term):
    try:
        return resolve.resolve_airport(term, allow_primary=True)[0]
    except (ValueError, resolve.Ambiguous):
        return None


def test_phrasing_variants_resolve():
    """The model does not always hand back a clean place name."""
    assert _r("flights out of Anchorage") == "ANC"
    assert _r("the Anchorage airport") == "ANC"
    assert _r("Santa Ana airport") == "SNA"
    assert _r("departures from Boston Logan") == "BOS"


def test_nonsense_is_refused_not_invented():
    """A plain substring match resolved 'Atlantis' to LAX, because it contains 'la'.
    Inventing an airport for a nonsense query is the worst failure this system can have."""
    assert _r("Atlantis") is None
    assert _r("Atlantis International") is None
    assert _r("Blahville") is None
    assert _r("Wakanda") is None


def test_longest_alias_wins():
    """'Portland Maine' must not resolve to Portland Oregon."""
    assert _r("Portland Maine") == "PWM"
    assert _r("Portland") == "PDX"


def test_new_england_excludes_new_york():
    codes, note = resolve.resolve_region("New England")
    assert "ALB" not in codes, "Albany is New York"
    assert "BDL" in codes, "Hartford is Connecticut"
    assert "New England" in note


def test_la_is_ambiguous_without_permission():
    try:
        resolve.resolve_airport("LA")
        raise AssertionError("bare 'LA' must raise Ambiguous rather than silently pick LAX")
    except resolve.Ambiguous as e:
        assert "SNA" in e.candidates


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all resolution tests passed")
