#!/usr/bin/env python3
"""Ask the agent a question. Uses cached BTS data so it runs fast."""
import sys, pathlib, argparse
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from build_and_rank import build_facts, JET_RUNWAYS
from airportiq.scoring.engine import score
from airportiq.agent.answer import answer

ap = argparse.ArgumentParser(); ap.add_argument("question", nargs="+")
a = ap.parse_args(); q = " ".join(a.question)

facts = build_facts(sorted(JET_RUNWAYS))
cards = score(facts, "terminal_expansion")
res = answer(q, cards, {f.code: f for f in facts})

print(f"Q: {q}\n")
print(f"[intent: {res.intent}]\n")
print(res.text)
if res.assumptions:
    print("\nAssumptions and caveats:")
    for x in res.assumptions:
        print(f"  - {x}")
