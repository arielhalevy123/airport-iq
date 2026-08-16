#!/usr/bin/env python3
"""Fetch real BTS data, build facts, and produce a ranking. No LLM anywhere in this path.

This is the proof for the reviewer: run it, get a ranking, then ask the chat agent the same
question and diff. If they ever disagree, that is a bug in the narrative layer, not an opinion.

    python scripts/build_and_rank.py --profile terminal_expansion
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.data import bts                      # noqa: E402
from airportiq.scoring.engine import AirportFacts, score   # noqa: E402

# Airports capped by regulation rather than demand. Hand-maintained with the authority named,
# because inferring this at runtime would be a guess. Flat growth at these is a legal ceiling.
REGULATORY_CAPS = {
    "SNA": "noise curfew + Settlement Agreement passenger cap",
    "DCA": "FAA slots + 1,250-mile perimeter rule",
    "LGA": "FAA Level 3 slots + perimeter rule",
    "JFK": "FAA Level 3 slots",
    "EWR": "FAA Level 2 schedule facilitation",
    "LGB": "noise-ordinance slot cap",
    "BUR": "night curfew",
}

# Runway counts (>= 5,000 ft, usable by jets). In the real deliverable this comes from
# OurAirports runways.csv; hardcoded here only to keep the demo self-contained.
JET_RUNWAYS = {
    "ATL": 5, "DFW": 7, "DEN": 6, "ORD": 8, "LAX": 4, "CLT": 4, "LAS": 4, "PHX": 3,
    "MCO": 4, "SEA": 3, "MIA": 4, "IAH": 5, "JFK": 4, "EWR": 3, "SFO": 4, "DTW": 6,
    "BOS": 4, "MSP": 4, "FLL": 2, "LGA": 2, "PHL": 4, "BWI": 3, "SLC": 4, "IAD": 4,
    "DCA": 3, "SAN": 1, "TPA": 3, "PDX": 3, "STL": 4, "BNA": 4, "AUS": 2, "MDW": 4,
    "HNL": 4, "DAL": 5, "OAK": 2, "SJC": 2, "SNA": 1, "SMF": 2, "RDU": 3, "MCI": 3,
    "IND": 3, "CLE": 3, "PIT": 4, "CVG": 4, "MKE": 4, "BUF": 2, "ONT": 2, "PBI": 3,
    "ORF": 2, "RSW": 1, "ANC": 3,
    # New England, so the region question has real coverage rather than BOS alone
    "BDL": 2, "PVD": 2, "MHT": 2, "PWM": 2, "BTV": 2, "BGR": 2,
    # other regionals that appear in the sample questions
    "ALB": 2, "SYR": 2, "JAX": 2, "MEM": 4, "SAT": 2, "HOU": 3, "OMA": 2, "BOI": 2,
}


def build_facts(codes: list[str]) -> list[AirportFacts]:
    facts: list[AirportFacts] = []
    for code in codes:
        rows = bts.monthly(code, months=36)
        rows = [r for r in rows if r["passengers"] is not None]
        if len(rows) < 24:
            print(f"  skip {code}: only {len(rows)} usable months", file=sys.stderr)
            continue

        ttm, prior = rows[:12], rows[24:36]
        pax_ttm = sum(r["passengers"] for r in ttm)
        dep_ttm = sum(r["departures"] or 0 for r in ttm)
        seats_ttm = sum(r["seats"] or 0 for r in ttm)
        monthly_pax = [r["passengers"] for r in ttm]

        intl = [r["international_passengers"] for r in ttm
                if r["international_passengers"] is not None]
        intl_share = (sum(intl) / pax_ttm) if intl and pax_ttm else None

        pax_2y = sum(r["passengers"] for r in prior) if len(prior) == 12 else None
        dep_prior = sum(r["departures"] or 0 for r in prior) if len(prior) == 12 else 0
        seats_prior = sum(r["seats"] or 0 for r in prior) if len(prior) == 12 else 0

        facts.append(AirportFacts(
            code=code,
            name=ttm[0]["name"] or code,
            passengers_ttm=pax_ttm,
            departures_ttm=dep_ttm,
            load_factor_ttm=(100.0 * pax_ttm / seats_ttm) if seats_ttm else None,
            peak_month_passengers=max(monthly_pax),
            mean_month_passengers=pax_ttm / len(ttm),
            international_share=intl_share,
            passengers_2y_ago=pax_2y,
            seats_per_departure_now=(seats_ttm / dep_ttm) if dep_ttm else None,
            seats_per_departure_base=(seats_prior / dep_prior) if dep_prior else None,
            jet_runways=JET_RUNWAYS.get(code),
            peak_month_departures=max((r["departures"] or 0) for r in ttm),
            regulatory_cap=REGULATORY_CAPS.get(code),
        ))
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="terminal_expansion",
                    choices=["terminal_expansion", "congestion"])
    ap.add_argument("--hub", default="large")
    a = ap.parse_args()

    codes = sorted(JET_RUNWAYS)
    print(f"fetching {len(codes)} airports from BTS ...", file=sys.stderr)
    facts = build_facts(codes)
    print(f"built facts for {len(facts)} airports\n", file=sys.stderr)

    cards = score(facts, a.profile)
    shown = [c for c in cards if c.hub_class == a.hub and c.composite is not None]

    print(f"=== {a.profile}  |  {a.hub} hubs  |  {len(shown)} airports ===\n")
    for c in shown[:12]:
        print(f"{c.rank:>2}. {c.code}  score {c.composite:>6}   {c.name[:38]}")
        top = list(c.contributions.items())[:3]
        print("     drivers: " + ", ".join(f"{k} {v}" for k, v in top))
        for f in c.flags:
            print(f"     ! {f}")
        if c.missing:
            print(f"     missing: {', '.join(c.missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
