# Session handoff — AirportIQ

Paste the block below into a new session to pick this up cold.

---

## Context prompt (copy from here)

I'm continuing work on **AirportIQ**, a home assignment for a **Deloitte Digital Forward Deployed
Engineer** role. The brief is at
`~/Library/Mobile Documents/com~apple~CloudDocs/personal/job-applications/2026-08-16_Deloitte_FDE_home_assignment/FDE_Exam_2.pdf`.

**Repo:** `~/Desktop/software_engnearing/airport-iq` — public at
https://github.com/arielhalevy123/airport-iq
**State:** `main` at 37 commits, everything merged, working tree clean.
**Read `DESIGN.md` first** (~350 lines); it carries the reasoning, not just the what.

### What it is

An agent that ranks US airports as capacity-investment candidates from public aviation data.
The organising idea: **the model chooses which questions to ask and writes the prose; it never
produces a number.** `tests/test_purity.py` walks the AST of `src/airportiq/scoring/` and fails
the build on any import of an LLM library, a network library, `time`, `datetime`, or `random`.
That test is the project's strongest argument — do not weaken it.

### Run it

```bash
cd ~/Desktop/software_engnearing/airport-iq
python -m airportiq.api.server        # http://localhost:8000
python run_tests.py                   # 36 tests, stdlib only, no framework
python evals/run_evals.py             # 8 cases, needs an LLM key
python scripts/build_and_rank.py --profile terminal_expansion   # no key, no model, no network
```

There is **no test framework and no third-party dependency**, deliberately — the project claims
it runs on a clean clone with nothing installed, and CI has no install step so that claim stays
honest. Don't add pytest; two tests actively guard against it coming back.

### Architecture

Two paths:
- **Tool-calling agent** (`src/airportiq/agent/react.py`) — 7 read-only tools, bounded at
  4 rounds / 10 calls, repeat calls served from cache. `run()` is the reference implementation
  the evals use; `run_streaming()` is the SSE generator the UI uses. They are deliberately
  separate so the thing under test matches the thing in production.
- **Deterministic pipeline** (`src/airportiq/agent/answer.py`) — fallback when no
  OpenAI-compatible key exists, and the reference implementation.

Both call the same pure engine (`src/airportiq/scoring/engine.py`). `/v1/score` reproduces any
number the agent quotes, with **no key and no model involved**.

### Data

- **BTS T-100 Segment Summary by Origin Airport** via Socrata (`r495-tyji`) — keyless.
  Three traps documented in `src/airportiq/data/bts.py`: the filter field is
  `origin_airport_code` (`origin` 400s), every value returns as a **string**, and Socrata
  **omits null fields per row**. Schema identity verified:
  `domestic_passengers + outbound_international_1 = total_passengers`, and
  `inbound_international_*` is a **separate arrivals block not in the total**.
- **BTS On-Time Performance** bulk zip → `data/snapshots/bts_delays.json`, built by
  `scripts/build_delay_snapshot.py`. Gives `nas_delay_share` (causal congestion) and, since
  today, per-flight `Distance` bucketed into stage-length bands.

### The four brief questions — all four answered, no exemptions

1. New England terminal expansion — region resolved deterministically, never by the model.
2. LA vs Santa Ana congestion — "LA" is disambiguated out loud; SNA's legal cap is disclosed.
3. **Anchorage long haul** — 11.6% at 2,500+ statute miles, 24.4% at 1,500+. **Two thresholds
   are always returned**, because the answer roughly doubles between them and one figure would
   hide that the threshold, not the airport, produced it. Scope caveat is emitted **by the
   tool**: the source is domestic-only, and ANC's real long-haul traffic is international
   freight to Asia. `test_anchorage_cargo_caveat_is_forced` pins it.
4. SFO unmet demand — a range with a method and a physical mechanism (parallel runways ~750 ft
   apart, marine layer halves the arrival rate), stated as a proxy rather than a measurement.

### Interface

Plain HTML/CSS/JS in `src/airportiq/api/ui.py`, no build step, no CDN.
- **SSE streaming** — tool calls appear live as the agent decides to make them. The point:
  the interesting latency is the tool calls, not token generation, so a spinner would hide
  the part worth watching.
- **Scorecards** — engine percentile bars beneath the prose, built server-side from the
  **tool-call arguments**, never by parsing the model's text.
- **Voice** — browser Web Speech API, both legs feature-detected. Voice *in* is the whole
  question; voice *out* is the prose only — the trace and caveats are never spoken.
- Amber is reserved exclusively for constraint flags. Amber = that airport is capped.

### Observability

`src/airportiq/obs.py` — **optional** Langfuse, wired at the boundary. `scoring/` must never
import it (there is a test). Traces the scoring run from the caller: profile, airports scored,
how many incomplete or flagged. **Fails open** — no keys, no package, or a throw all degrade to
a no-op. Keys go in `.env`, see `.env.example`.

### Git workflow the owner asked for

`feat/*` → `staging` → PR → `main`. Many small commits with real reasoning in the message.
CI (`.github/workflows/ci.yml`) runs `run_tests.py` on Python 3.10–3.13 on every push to
`main`/`staging` and every PR, plus a hygiene job that greps for committed credentials.
**Note:** GitHub blocks approving your own PR, so PRs get merged, not approved.

### Bugs already found and fixed — don't reintroduce

- `"Atlantis"` resolved to **LAX** by substring match. Fixed with word-boundary matching plus a
  regression test. The worst class of failure this system can have.
- SSE sent `Connection: keep-alive`. An SSE body has no `Content-Length`, so only the socket
  closing ends it; the browser's reader never reported `done` and **everything after the stream
  silently never ran** — no trace, no scorecards, input stuck disabled. Presents as a missing
  feature, not an error.
- The UI was served with **no cache headers**, so a browser kept serving the old page against a
  new server. This is what made streaming look broken when it wasn't.
- `delay_congestion` was dead code — in the engine, never populated by `build_facts`.
- `hash()` is randomised per process → cache never hit once. Now sha1.
- Relative import broke `python server.py` (worked only via `-m`).

### Owner's standing preferences

- Terse; mirror his current-message language (Hebrew/English).
- Decide technical trade-offs yourself and state the reasoning; don't ask permission for
  routine calls.
- He wants many commits across feature branches.
- **No hosting, no auth, no JWT, no server deployment.** Local only.
- Secrets live in `.env` / `~/.ted/secrets/`, mode 600, never in git.

### Open items

- **Rotate the OpenAI key** — it was pasted into a chat window and is currently live in `.env`.
- Send Rotem the repo link.
- MCP wrapper for the tools was offered but never built (~30 min).
- Owner mentioned another company's home assignment due — unknown which.
