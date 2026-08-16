#!/usr/bin/env python3
"""Build the delay snapshot from BTS On-Time Performance.

Why this exists: FAA ASPM — the natural source for congestion — is login-gated with no API.
But BTS publishes flight-level On-Time Performance at a stable bulk URL, and that carries the
one metric that actually attributes congestion to the airport rather than to airlines or
weather:

    NAS delay share = sum(NASDelay) / sum(total delay minutes)

"NAS" is National Airspace System delay: volume and capacity constraints, holding, flow control.
As distinct from CarrierDelay (the airline's own problem) and WeatherDelay (nobody's fault).
An airport where NAS delay dominates the delay mix is an airport hitting its capacity ceiling —
which is exactly the investment signal, and it is causal rather than correlational.

We download one month (~30 MB), aggregate to a few KB per airport, and commit that. The reviewer
gets real delay data without a 30 MB download or a BTS account.

    python scripts/build_delay_snapshot.py 2026 4
"""
from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

URL = ("https://transtats.bts.gov/PREZIP/"
       "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip")
OUT = Path(__file__).resolve().parents[1] / "data" / "snapshots" / "bts_delays.json"


def build(year: int, month: int) -> Path:
    url = URL.format(year=year, month=month)
    print(f"downloading {url} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "airport-iq/0.1"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        blob = resp.read()
    print(f"  {len(blob)/1e6:.1f} MB", file=sys.stderr)

    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        print(f"  parsing {name}", file=sys.stderr)
        with z.open(name) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
            for i, row in enumerate(reader):
                origin = row.get("Origin")
                if not origin:
                    continue
                a = agg[origin]
                a["flights"] += 1

                def f(key: str) -> float:
                    v = row.get(key, "")
                    try:
                        return float(v) if v not in ("", None) else 0.0
                    except ValueError:
                        return 0.0

                a["taxi_out_sum"] += f("TaxiOut")
                a["taxi_out_n"] += 1 if row.get("TaxiOut") else 0
                a["arr_delay_min"] += f("ArrDelayMinutes")
                a["carrier_delay"] += f("CarrierDelay")
                a["weather_delay"] += f("WeatherDelay")
                a["nas_delay"] += f("NASDelay")
                a["late_aircraft_delay"] += f("LateAircraftDelay")
                a["security_delay"] += f("SecurityDelay")
                a["del15"] += f("DepDel15")
                a["cancelled"] += f("Cancelled")
                if i and i % 250_000 == 0:
                    print(f"    {i:,} rows", file=sys.stderr)

    out: dict[str, dict] = {}
    for code, a in agg.items():
        if a["flights"] < 500:          # too few flights for a stable rate
            continue
        cause_total = (a["carrier_delay"] + a["weather_delay"] + a["nas_delay"]
                       + a["late_aircraft_delay"] + a["security_delay"])
        out[code] = {
            "flights": int(a["flights"]),
            "mean_taxi_out_min": round(a["taxi_out_sum"] / a["taxi_out_n"], 2)
                                 if a["taxi_out_n"] else None,
            # The headline metric: how much of this airport's delay is the airspace system
            # (i.e. volume against capacity) rather than airline or weather problems.
            "nas_delay_share": round(a["nas_delay"] / cause_total, 4) if cause_total else None,
            "pct_delayed_15": round(a["del15"] / a["flights"], 4),
            "cancel_rate": round(a["cancelled"] / a["flights"], 4),
            "total_delay_minutes": int(a["arr_delay_min"]),
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"period": f"{year}-{month:02d}", "airports": out}, indent=1))
    print(f"wrote {OUT} — {len(out)} airports, {OUT.stat().st_size/1024:.0f} KB", file=sys.stderr)
    return OUT


if __name__ == "__main__":
    y = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    m = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    build(y, m)
