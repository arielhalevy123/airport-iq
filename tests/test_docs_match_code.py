"""The design doc must state the same weights the code uses.

A doc that drifts from the code is worse than no doc: a reviewer who spots the mismatch
stops trusting everything else in it. This test makes the drift a build failure.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.scoring.engine import PROFILES

DESIGN = Path(__file__).resolve().parents[1] / "DESIGN.md"


def test_design_doc_states_the_real_weights():
    doc = DESIGN.read_text()
    missing = [f"{profile}: {kpi} {w:g}"
               for profile, weights in PROFILES.items()
               for kpi, w in weights.items()
               if f"{kpi} {w:g}" not in doc]
    assert not missing, (
        "DESIGN.md does not state these weights as implemented:\n  "
        + "\n  ".join(missing)
    )


def test_weights_sum_to_one():
    for profile, weights in PROFILES.items():
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-9, f"{profile} weights sum to {total}, not 1.0"


def test_no_weight_dominates():
    """Stated design principle: above 0.40 the ranking is a one-metric sort in disguise."""
    for profile, weights in PROFILES.items():
        worst = max(weights.items(), key=lambda kv: kv[1])
        assert worst[1] <= 0.40, f"{profile}: {worst[0]} at {worst[1]} exceeds the 0.40 ceiling"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("docs match code")
