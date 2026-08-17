# Assignment Coverage — Airport Investment Intelligence Agent

This document maps every requirement in the brief to where it is implemented in this
repository and to the evidence that it works. All verification commands below were run on
2026-08-17 against a clean working tree: **63/63 unit tests pass** (`python3 run_tests.py`)
and **8/8 acceptance evals pass** (`python3 evals/run_evals.py`), including all four
questions from the brief.

The full design rationale (scoring methodology, tradeoffs, where AI is used) lives in
[`DESIGN.md`](../DESIGN.md). This document is the checklist view: requirement, implementation,
proof.

---

## 1. The four example questions

All four run as automated acceptance cases in `evals/questions.yaml`, executed against the
live agent by `evals/run_evals.py`. The suite asserts on the structured payload (resolved
intent, airports referenced, assumptions disclosed, and that no number appears which the
engine did not compute), not on prose wording.

| Brief question | Eval case | How it is answered |
|---|---|---|
| Which airports in New England are strong candidates for terminal expansion? | `q1_new_england` | `list_region` resolves "New England" deterministically to 6 states and their airport codes, then `rank_airports` scores them with the `terminal_expansion` profile. |
| Compare LA and Santa Ana airport congestion levels. | `q2_la_vs_santa_ana` | `get_airport_metrics` / `compare_airports` with the `congestion` profile. "LA" is ambiguous by design: the resolver asks rather than guesses (`agent/resolve.py`). |
| What is the percentage of long haul flights out of Anchorage airport? | `q3_anchorage_long_haul` | `get_stage_length_mix` buckets flight-level BTS On-Time data by distance. Both common thresholds are always returned (11.6% at 2,500+ mi, 24.4% at 1,500+ mi), and the tool itself forces the domestic-only scope caveat, since ANC's real long-haul traffic is international cargo that this source cannot see. |
| What is the unmet flight demand in SFO airport and why? | `q4_sfo_unmet_demand` | `estimate_unmet_demand` builds a proxy from three observable leaks (load factor above ~82%, NAS delay share, upgauging without frequency growth) and returns a **range** (3-9% of current traffic at SFO), never a point estimate. `get_delay_breakdown` supplies the "why": SFO's parallel runways are too close for independent approaches in low visibility. |

Verify: `python3 evals/run_evals.py` (needs one LLM key in `.env`).

## 2. "Use public APIs to gather airport/aviation data"

Two public, keyless BTS sources, both handled at the boundary in `src/airportiq/data/bts.py`
and `scripts/build_delay_snapshot.py`:

- **BTS T-100 Segment Summary** via the Socrata API
  (`https://data.bts.gov/resource/r495-tyji.json`): ~131,700 rows of monthly traffic per
  origin airport, currently through 2026-04.
- **BTS On-Time Performance** bulk files (`https://transtats.bts.gov/PREZIP/...`):
  flight-level delay causes, taxi-out times, and stage lengths.

A committed snapshot in `data/snapshots/` means the whole system also runs with **no network
and no account**, so a reviewer on a clean clone gets identical numbers.

## 3. "Rank or compare airports based on your defined logic or KPI"

Six KPIs, each computed as a percentile rank **within the airport's hub class**, combined
with fixed, documented weights (`src/airportiq/scoring/engine.py`). Two profiles:

- **terminal_expansion**: peak_pressure 0.35, gate_saturation 0.20, demand_growth 0.20,
  international_intensity 0.15, airside_headroom 0.10
- **congestion**: delay_congestion 0.35, airside_saturation 0.20, demand_growth 0.15,
  peak_pressure 0.15, gate_saturation 0.15

The same runway measure enters the terminal profile as headroom (spare runway is a
precondition) and the congestion profile as saturation (runway saturation is congestion),
which is what stops the system recommending a terminal at a runway-constrained airport. A
hand-maintained `REGULATORY_CAPS` registry flags legally capped airports (JFK, LGA, DCA,
SNA, EWR) where flat growth is a ceiling, not weakness.

Full methodology and the reasoning behind every weight: `DESIGN.md` section 4.

Verify without any LLM: `python3 scripts/build_and_rank.py --profile terminal_expansion`.

## 4. "Explain its reasoning clearly"

- Every ranked result carries **per-KPI contributions** (an additive model was chosen
  specifically because it can decompose its own score; see `DESIGN.md` section 6).
- Every answer in the UI shows the **tool-call trace** that produced it, streamed live, plus
  percentile bars drawn from the deterministic engine. The bars are built server-side from
  the tool-call arguments, never by parsing the model's prose, so they verify the answer
  rather than echo it.
- Constraint flags are written as recommendations, not just labels (for example
  "airside-first: a terminal will not relieve this").

## 5. "Support conversational follow-up questions"

`src/airportiq/agent/session.py` persists per-session state (airports, profile, disclosed
assumptions) keyed by `session_id`, and `server.py` threads conversation history through
`/v1/chat/stream`. So "Compare LAX and SNA" followed by "why?" works.

Numbers deliberately do **not** carry across turns: every answer re-reads values from the
score cards, because inheriting a figure from chat history would let the model restate a
stale number and bypass the numeric guard.

Evidence: eval case `followup_keeps_referent` passes.

## 6. Requirement: deterministic scoring (not only LLM output)

Three mechanisms, in increasing strictness (details in `DESIGN.md` section 3):

1. **A purity test that fails the build.** `tests/test_purity.py` walks the AST of every
   module under `scoring/` and rejects any import of an LLM library, a network library,
   `random`, or even `datetime`. The LLM cannot touch the arithmetic because the build
   fails if the code even imports the means to.
2. **The model cannot emit digits.** Narration references values as `{{CODE.field}}`
   placeholders; the server substitutes real numbers afterwards.
3. **A numeric tripwire.** After substitution, any numeric literal not in the allow-set of
   actually computed values causes the prose to be discarded and replaced with a
   deterministic template.

Proof for a reviewer: run `build_and_rank.py` (pure path, no model), then ask the agent the
same question. Identical numbers, because it is the same function.

## 7. Requirement: chat interface (voice is a bonus)

- `python3 -m airportiq.api.server` serves a chat UI at `http://localhost:8000`. Plain
  HTML/CSS/JS, no build step, no CDN. Tool calls stream live as the agent makes them.
- **Voice is implemented** (`src/airportiq/api/ui.py`): browser Web Speech API, with
  `SpeechRecognition` for input and `speechSynthesis` for output, each feature-detected
  separately. Speech carries the finding; caveats and evidence stay on screen, deliberately,
  so they are not lost in listening.

Any one of `OPENAI_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, or `ANTHROPIC_API_KEY` works.

## 8. Requirement: communicate assumptions, uncertainty, and scoping

- **Assumptions** are a first-class part of every answer payload, and the eval suite asserts
  they are disclosed.
- **Uncertainty**: unmet demand is returned as a range with a confidence label; long-haul
  share always returns both thresholds because the definition, not the airport, drives the
  answer.
- **Scoping**: `DESIGN.md` opens by stating what the system does *not* rank (profitability
  requires cost data that is not public), section 7 lists known limitations up front (gate
  counts absent, monthly granularity, four-month data lag, one month of delay data), and
  section 9 lists everything not modelled at all. Scope caveats that matter are emitted by
  the tools themselves (for example the Anchorage domestic-only caveat is forced by a test),
  not left to the model's discretion.

## 9. Deliverables

| Deliverable | Where |
|---|---|
| Source code | `src/airportiq/` (agent, scoring, data, API), `scripts/`, `tests/`, `evals/` |
| Design/architecture document | `DESIGN.md`: scoring methodology (section 4), key tradeoffs (section 6), where and how AI is used (sections 2 and 3) |
| This coverage map | `docs/ASSIGNMENT_COVERAGE.md` |

## 10. How to verify everything yourself

```bash
python3 run_tests.py                                              # 63 tests, stdlib only, no key
python3 scripts/build_and_rank.py --profile terminal_expansion    # pure ranking, no LLM, no network
cp .env.example .env                                              # add one LLM key
python3 -m airportiq.api.server                                   # chat UI at http://localhost:8000
python3 evals/run_evals.py                                        # 8 acceptance cases incl. the 4 brief questions
```
