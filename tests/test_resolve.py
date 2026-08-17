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


def test_us_states_resolve_as_regions():
    """State-based queries must work as regions. Membership is deterministic (AIRPORT_STATE),
    never guessed. A user asking 'airports in Florida' should get every FL airport in our
    universe, not just BOS-because-New-England-is-the-only-known-region."""
    fl_codes, fl_note = resolve.resolve_region("Florida")
    assert "MCO" in fl_codes and "MIA" in fl_codes and "FLL" in fl_codes and "TPA" in fl_codes
    assert "ATL" not in fl_codes, "Georgia is not Florida"
    assert "Florida" in fl_note

    tx_codes, _ = resolve.resolve_region("Texas")
    assert "DFW" in tx_codes and "IAH" in tx_codes and "AUS" in tx_codes
    assert "MCO" not in tx_codes

    # Wrapping words that a user or model might use verbatim.
    assert resolve.resolve_region("state of Florida")[0] == fl_codes
    assert resolve.resolve_region("the Florida")[0] == fl_codes


def test_state_abbreviation_is_not_a_region():
    """Two-letter abbreviations collide with metro shorthand and English. 'LA' is a metro
    in this codebase and must not silently become Louisiana here. Users spell out the state."""
    try:
        resolve.resolve_region("LA")
    except ValueError:
        pass                                          # good — refused rather than guessed
    else:
        raise AssertionError("bare 'LA' region must raise, not resolve to Louisiana")


def test_state_with_no_airports_refuses_rather_than_returns_empty():
    """A state with no airport in AIRPORT_STATE is a coverage gap, not a query with an
    empty answer. Silence would look like 'no Alabama airports have expansion potential',
    which is a wrong answer to a right question."""
    try:
        resolve.resolve_region("Wyoming")
    except ValueError as e:
        assert "Wyoming" in str(e)
    else:
        raise AssertionError("a state we do not cover must refuse rather than return empty")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all resolution tests passed")
