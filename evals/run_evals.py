#!/usr/bin/env python3
"""Run the acceptance suite against the live agent.

You cannot evaluate an LLM system by reading a few answers and deciding they look fine. The
failures that matter are the ones that appear on the fifth question, or after a prompt tweak
three days later. So the four brief questions, plus the refusal and ambiguity cases, run as a
suite with pass/fail per assertion.

WHAT IS ASSERTED
    the resolved intent, the airports actually referenced, the assumptions disclosed,
    and that no number appears which the engine did not compute.

WHAT IS NOT ASSERTED
    prose quality. A suite that fails when a sentence is reworded gets ignored within a week,
    and an ignored suite is worse than none because it looks like coverage.

    python evals/run_evals.py            # all
    python evals/run_evals.py q4         # one, by id prefix
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def _strip_comment(val: str) -> str:
    """Remove a trailing # comment that is not inside quotes or brackets.

    The first version of this parser did not, so expected values arrived carrying their own
    explanatory comments and every assertion failed for the wrong reason. Worth noting: the
    eval harness needed debugging before its verdicts could be trusted, which is exactly why
    a suite that has never failed is not evidence of anything.
    """
    out, in_q, depth = [], False, 0
    for ch in val:
        if ch == '"':
            in_q = not in_q
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "#" and not in_q and depth == 0:
            break
        out.append(ch)
    return "".join(out)


def load_cases(path: Path) -> list[dict]:
    """Minimal YAML subset parser — avoids a dependency for a 60-line config file."""
    cases: list[dict] = []
    cur: dict | None = None
    section: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("- id:"):
            cur = {"id": line.split(":", 1)[1].strip(), "expect": {}}
            cases.append(cur)
            section = None
        elif cur is None:
            continue
        elif line.startswith("  expect:"):
            section = "expect"
        elif line.startswith("  ") and not line.startswith("    "):
            key, _, val = line.strip().partition(":")
            cur[key] = _strip_comment(val).strip().strip('"')
            section = None
        elif line.startswith("    ") and section == "expect":
            key, _, val = line.strip().partition(":")
            val = _strip_comment(val).strip()
            if val.startswith("["):
                cur["expect"][key] = [v.strip().strip('"')
                                      for v in val.strip("[]").split(",") if v.strip()]
            elif val in ("true", "false"):
                cur["expect"][key] = val == "true"
            elif val.isdigit():
                cur["expect"][key] = int(val)
            else:
                cur["expect"][key] = val.strip('"')
    return cases


def run_case(case: dict, cards, facts_by_code) -> tuple[bool, list[str]]:
    from airportiq.agent.answer import answer
    from airportiq.agent.session import Session

    exp = case["expect"]
    session = Session(sid=case["id"])
    failures: list[str] = []

    if case.get("after"):
        answer(case["after"], cards, facts_by_code, session=session)

    res = answer(case["question"], cards, facts_by_code, session=session)
    text = res.text
    blob = text + " " + " ".join(res.assumptions)

    if "intent" in exp and res.intent != exp["intent"]:
        if not (exp.get("allow_unsupported") and res.intent == "unsupported"):
            failures.append(f"intent was {res.intent!r}, expected {exp['intent']!r}")

    for code in exp.get("must_mention", []):
        if code not in blob:
            failures.append(f"never mentioned {code}")

    for code in exp.get("must_not_mention", []):
        if re.search(rf"\b{code}\b", blob):
            failures.append(f"mentioned {code}, which should be excluded")

    if "must_state_assumption" in exp:
        needle = exp["must_state_assumption"].lower()
        if needle not in " ".join(res.assumptions).lower():
            failures.append(f"did not disclose the assumption about {exp['must_state_assumption']!r}")

    if "must_disclose_flag" in exp:
        if exp["must_disclose_flag"].lower() not in blob.lower():
            failures.append(f"did not disclose the {exp['must_disclose_flag']!r} flag")

    if "must_contain_any" in exp:
        if not any(w.lower() in text.lower() for w in exp["must_contain_any"]):
            failures.append(f"none of {exp['must_contain_any']} appeared — the causal side was skipped")

    if "min_airports" in exp:
        found = len(set(re.findall(r"\b[A-Z]{3}\b", text)))
        if found < exp["min_airports"]:
            failures.append(f"only {found} airports referenced, expected at least {exp['min_airports']}")

    if exp.get("must_not_invent"):
        if res.intent != "unsupported" and not any(
            w in blob.lower() for w in ("not an airport", "no data", "cannot", "excluded")
        ):
            failures.append("did not refuse an airport it has no data for")

    return (not failures), failures


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = load_cases(Path(__file__).parent / "questions.yaml")
    if only:
        cases = [c for c in cases if c["id"].startswith(only)]
    if not cases:
        print("no matching cases"); return 2

    print("loading data ...", file=sys.stderr)
    from build_and_rank import JET_RUNWAYS, build_facts
    from airportiq.scoring.engine import score
    facts = build_facts(sorted(JET_RUNWAYS))
    cards = score(facts, "congestion")
    by_code = {f.code: f for f in facts}

    passed = 0
    for case in cases:
        try:
            ok, failures = run_case(case, cards, by_code)
        except Exception as e:                      # noqa: BLE001
            ok, failures = False, [f"raised {type(e).__name__}: {e}"]
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']}")
        for f in failures:
            print(f"        {f}")

    print(f"\n{passed}/{len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
