"""Conversation state, so follow-up questions work.

The brief requires "support conversational follow-up questions". Without state, "compare LAX
and SNA" followed by "why?" has no referent and the agent either guesses or fails.

WHY THIS IS NOT LANGGRAPH
-------------------------
LangGraph's checkpointer is the natural production home for this, and this module is
deliberately shaped like one: a thread id, a persisted state object, a reducer that merges a
new turn into it. But the pipeline here is strictly linear — parse, resolve, fetch, score,
narrate, verify — with no branching, no cycles and no agent handoff. LangGraph earns its place
when routing is dynamic; here it would add a dependency and no capability, and the repo would
stop running on `git clone` alone.

So: same pattern, implemented directly, and the mapping to LangGraph is one paragraph in the
design doc. Adding the framework when there is a reason is a defensible decision. Adding it
because it looks good is the kind of scope sprawl this role is screening against.

WHAT CARRIES OVER, AND WHAT DELIBERATELY DOES NOT
-------------------------------------------------
Carried: the airports last discussed, the profile, and the assumptions already stated (so the
same caveat is not repeated every turn).

NOT carried: any NUMBER from a previous answer. A follow-up always re-reads values from the
score cards. If a figure could be inherited from conversation history, the model could restate
a stale or misremembered number — which is exactly the failure the numeric guard exists to
prevent, reintroduced through the back door.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# Phrases that only make sense against a previous turn.
_FOLLOWUP = re.compile(
    r"^\s*(why|why\?|and why|how come|what about|and\b|explain|elaborate|which one|"
    r"compare them|the first|the second|that one|those|it|them)\b",
    re.I,
)

_MAX_TURNS = 12
_TTL_SEC = 60 * 60


@dataclass
class Turn:
    question: str
    intent: str
    codes: list[str]
    profile: str
    at: float = field(default_factory=time.time)


@dataclass
class Session:
    sid: str
    turns: list[Turn] = field(default_factory=list)
    stated_assumptions: set[str] = field(default_factory=set)

    def add(self, turn: Turn) -> None:
        self.turns.append(turn)
        del self.turns[:-_MAX_TURNS]

    @property
    def last(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    def context_codes(self) -> list[str]:
        """Airports from the most recent turn that had any."""
        for t in reversed(self.turns):
            if t.codes:
                return t.codes
        return []

    def context_profile(self) -> str | None:
        return self.turns[-1].profile if self.turns else None

    def new_assumptions(self, assumptions: list[str]) -> list[str]:
        """Filter out caveats already said this session — repeating them every turn trains
        the reader to skip them, which defeats the point of stating them."""
        fresh = [a for a in assumptions if a not in self.stated_assumptions]
        self.stated_assumptions.update(fresh)
        return fresh


class SessionStore:
    """In-memory sessions with a TTL. A real deployment swaps this for the LangGraph
    checkpointer or any KV store; the interface is the same three methods."""

    def __init__(self) -> None:
        self._s: dict[str, Session] = {}

    def get(self, sid: str) -> Session:
        self._evict()
        if sid not in self._s:
            self._s[sid] = Session(sid=sid)
        return self._s[sid]

    def _evict(self) -> None:
        now = time.time()
        for sid, sess in list(self._s.items()):
            if sess.turns and now - sess.turns[-1].at > _TTL_SEC:
                del self._s[sid]

    def __len__(self) -> int:
        return len(self._s)


def is_followup(question: str) -> bool:
    """Does this question depend on the previous turn?

    Deliberately a regex over opening phrases rather than an LLM call: it is deterministic,
    instant, free, and independently testable. Getting it wrong is cheap in one direction
    (we pass extra context the model ignores) and expensive in the other (we lose the referent),
    so it is tuned to over-detect.
    """
    q = question.strip()
    if len(q.split()) <= 3 and not q.endswith("?"):
        return True                      # "why", "and SFO", "the second one"
    return bool(_FOLLOWUP.match(q))
