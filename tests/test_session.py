"""Conversation state: follow-ups keep the referent, numbers never carry over."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.agent.session import Session, SessionStore, Turn, is_followup


def test_followup_detection():
    for q in ["why?", "Why is that?", "and SFO", "what about Boston", "explain", "the second one"]:
        assert is_followup(q), f"{q!r} should be a follow-up"
    for q in ["Compare LAX and SNA congestion levels",
              "Which airports in New England need terminal expansion?",
              "What is the unmet flight demand at SFO?"]:
        assert not is_followup(q), f"{q!r} should NOT be a follow-up"


def test_context_carries_airports():
    s = Session(sid="t")
    s.add(Turn(question="Compare LAX and SNA", intent="compare",
               codes=["LAX", "SNA"], profile="congestion"))
    assert s.context_codes() == ["LAX", "SNA"]
    assert s.context_profile() == "congestion"


def test_assumptions_are_not_repeated():
    """Restating the same caveat every turn trains the reader to skip it."""
    s = Session(sid="t")
    first = s.new_assumptions(["New England = CT, ME, MA, NH, RI, VT.", "LA is ambiguous."])
    assert len(first) == 2
    again = s.new_assumptions(["New England = CT, ME, MA, NH, RI, VT.", "SNA is capped."])
    assert again == ["SNA is capped."], "already-stated assumptions must be filtered"


def test_history_is_bounded():
    s = Session(sid="t")
    for i in range(40):
        s.add(Turn(question=f"q{i}", intent="rank", codes=["SFO"], profile="congestion"))
    assert len(s.turns) <= 12, "unbounded history would grow without limit"


def test_sessions_are_isolated():
    store = SessionStore()
    a, b = store.get("a"), store.get("b")
    a.add(Turn(question="q", intent="rank", codes=["JFK"], profile="congestion"))
    assert b.context_codes() == [], "one user's context must not leak into another's"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all session tests passed")
