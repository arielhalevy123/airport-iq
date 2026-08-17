# AirportIQ — Architecture

How the system is built, layer by layer, and exactly what happens to a chat message from the
moment the user presses Enter to the moment the answer, its tool trace, and its scorecards
appear on screen.

This document explains the *structure*. The *rationale* (why each design decision was made,
scoring methodology, tradeoffs) lives in [`DESIGN.md`](../DESIGN.md).

---

## 1. System at a glance

```
 ┌────────────────────────────────────────────────────────────────────────┐
 │  BROWSER  (plain HTML/CSS/JS, no build step)                           │
 │  chat box · live tool trace · percentile bars · voice in/out           │
 └───────────────┬────────────────────────────────────────────────────────┘
                 │  POST /v1/chat/stream  (SSE)          POST /v1/score
                 ▼                                            │
 ┌────────────────────────────────────────────────────────────┼───────────┐
 │  HTTP SERVER  api/server.py  (stdlib http.server, no framework)        │
 │  routes · session lookup · SSE writer · scorecard panel builder        │
 └───────┬──────────────────────────────┬─────────────────────┼───────────┘
         │ default                      │ fallback            │ no LLM at all
         ▼                              ▼                     │
 ┌──────────────────────┐   ┌──────────────────────┐          │
 │  AGENT (tool loop)   │   │  DETERMINISTIC       │          │
 │  agent/react.py      │   │  PIPELINE            │          │
 │  model picks tools,  │   │  agent/answer.py     │          │
 │  ≤4 rounds ≤10 calls │   │  parse→resolve→fetch │          │
 └──────┬───────┬───────┘   │  →score→narrate      │          │
        │       │           │  →verify             │          │
        │       │           └──────────┬───────────┘          │
        ▼       ▼                      ▼                      ▼
 ┌───────────┐ ┌────────────────────────────────────────────────────────┐
 │ LLM       │ │  SCORING ENGINE  scoring/   ← PURE: no I/O, no model,  │
 │ provider  │ │  engine.py · unmet.py · explain.py · thesis.py         │
 │ factory   │ │  (enforced by tests/test_purity.py, fails the build)   │
 │ agent/    │ └───────────────────────────┬────────────────────────────┘
 │ llm.py    │                             │ reads AirportFacts
 └───────────┘                             ▼
               ┌─────────────────────────────────────────────────────────┐
               │  DATA LAYER  data/bts.py · scripts/build_delay_snapshot │
               │  BTS T-100 (Socrata, keyless) · BTS On-Time (bulk)      │
               │  → local cache → committed snapshot (offline fallback)  │
               └─────────────────────────────────────────────────────────┘

   sidecars: agent/session.py (conversation state, keyed by session_id)
             obs.py (optional Langfuse tracing, wired at the boundary only)
```

Three properties of this picture are load-bearing:

1. **Both answer paths converge on the same engine.** The agent's tools and the fallback
   pipeline both read the same precomputed score cards, so `/v1/score` (no LLM) reproduces
   any number the chat ever shows.
2. **The LLM sits beside the flow, not inside the arithmetic.** It chooses which tools to
   call and phrases the answer. Every number comes out of `scoring/`, which cannot even
   import a model or network library without failing the build.
3. **The data layer degrades gracefully**: live Socrata API → local cache → committed
   snapshot, so a clean clone with no network still produces identical numbers.

---

## 2. Module map

| Module | Role |
|---|---|
| `api/server.py` | stdlib HTTP server. Routes, SSE streaming, session lookup, builds the scorecard panel from tool-call arguments |
| `api/ui.py` | The single-page chat UI, served inline. Web Speech API voice in/out |
| `agent/react.py` | The tool-calling loop: bounded at 4 rounds / 10 tool calls, repeated calls served from a per-turn cache |
| `agent/tools.py` | The 12 read-only tools the model may call. Every tool returns engine-computed values with provenance |
| `agent/llm.py` | Provider factory: OpenAI, Groq, Gemini (OpenAI wire style) and Anthropic behind one interface, no SDK dependency |
| `agent/session.py` | Conversation state per `session_id`, so follow-ups have a referent |
| `agent/answer.py` | The deterministic fallback pipeline (parse → resolve → fetch → score → narrate → verify) |
| `agent/resolve.py` | Deterministic entity resolution: "New England" → 6 states → codes; "LA" raises Ambiguous rather than guessing |
| `scoring/engine.py` | The pure scoring engine: KPIs, hub-class percentiles, weighted composites, flags |
| `scoring/unmet.py` | Unmet-demand proxy, returned as a range with stated confidence |
| `scoring/explain.py` | Per-stat derivations behind the UI's percentile bars |
| `scoring/thesis.py` | Converts a score card into a structured investment thesis |
| `data/bts.py` | BTS T-100 boundary: Socrata quirks handled here, cache, snapshot |
| `obs.py` | Optional Langfuse tracing, at the boundary only (the purity test forbids it inside `scoring/`) |

---

## 3. Boot sequence

Everything heavy happens once, at startup, so per-message latency is the LLM plus memory reads:

```
 python -m airportiq.api.server
        │
        ▼
 _load()  ──▶  fetch T-100 for 65 airports (API → cache → snapshot)
        │
        ▼
 build_facts()      one AirportFacts per airport (traffic, delays, stage lengths)
        │
        ▼
 score() × 2        full score cards for both profiles
        │            (terminal_expansion, congestion), all hub classes
        ▼
 in-memory state:   _FACTS · _CARDS · SessionStore
        ▼
 "ready: 65 airports"  →  serves http://localhost:8000
```

The consequence: **tools never do I/O at question time.** They read `_CARDS` and `_FACTS`
from memory, which is what makes a 10-tool-call turn fast and makes every answer consistent
with every other answer in the same server run.

---

## 4. The life of a message

The exact path of one chat turn through `/v1/chat/stream`, step by step. Numbers match the
sequence diagram below.

```
 BROWSER              SERVER               AGENT LOOP            LLM          TOOLS/ENGINE
    │  (1) POST          │                  (react.py)            │           (in-memory)
    │  question+session  │                      │                 │                │
    ├───────────────────▶│  (2) load history    │                 │                │
    │                    ├─────────────────────▶│                 │                │
    │   event: round     │                      │ (3) round 1     │                │
    │◀───────────────────┤                      ├────────────────▶│                │
    │                    │                      │  question+hist  │                │
    │                    │                      │  +12 tool defs  │                │
    │                    │                      │◀────────────────┤                │
    │   event: tool_call │                      │ (4) tool calls  │                │
    │◀───────────────────┤◀─────────────────────┤ (5) execute ───────────────────▶ │
    │   event: tool_result                      │◀───────────────────────────────── │
    │◀───────────────────┤◀─────────────────────┤  engine values, │                │
    │                    │                      │  with provenance│                │
    │                    │                      │ (6) round 2..4: back to (3)      │
    │                    │                      │     until the model answers      │
    │   event: delta ×N  │                      ├────────────────▶│                │
    │◀───────────────────┤◀─────────────────────┤ (7) prose tokens│                │
    │   event: done      │ (8) attach           │                 │                │
    │◀───────────────────┤  assumptions +       │                 │                │
    │                    │  scorecards from     │                 │                │
    │  (9) render trace, │  TOOL-CALL ARGS      │                 │                │
    │  prose, bars,      │  save turn to session│                 │                │
    │  flags; speak      │                      │                 │                │
```

**(1) The browser sends the question.** `POST /v1/chat/stream` with
`{"question": "...", "session_id": "..."}`. The response is a Server-Sent Events stream, so
the UI shows the agent's work as it happens instead of a spinner.

**(2) The server loads conversation state.** `SessionStore` returns the prior turns for this
`session_id`. Only the (question, answer) prose pairs are replayed as chat history. Tool
traces from earlier turns are deliberately **not** replayed: if old numbers travelled in
history, the model could restate a stale figure and bypass the numeric guard through the
back door. Follow-ups inherit *referents* ("why?" knows we were discussing LAX vs SNA),
never *numbers* (every figure is re-read from the engine each turn).

**(3) The loop starts a round.** `react.run_streaming` sends the model the question, the
history, and the schemas of 12 read-only tools (`get_airport_metrics`, `compare_airports`,
`list_region`, `get_delay_breakdown`, `estimate_unmet_demand`, `get_stage_length_mix`,
`rank_airports`, `rank_by_passenger_growth`, `rank_by_flight_growth`, `compare_growth`,
`get_delay_per_passenger`, `get_cargo_intensity`). The model decides which to call and in
what order. This choice is the agentic part, and it is the *only* decision the model owns
besides phrasing.

**(4)–(5) Tools execute, and the user watches.** Each call is emitted as a `tool_call`
event the moment the model requests it, then executed against the in-memory cards and
facts, and its result summarized in a `tool_result` event. Every tool returns
engine-computed, structured values with provenance. Repeated identical calls are served
from a per-turn cache rather than re-executed.

**(6) The loop iterates, bounded.** The results go back to the model, which may call more
tools (asked about unmet demand at SFO, it typically calls `estimate_unmet_demand`, then
`get_delay_breakdown` for the "why"). Hard bounds: **4 rounds, 10 tool calls per turn.** A
model that hits the bounds must answer with what it has; it cannot loop forever.

**(7) The answer streams.** When the model stops calling tools and writes prose, each token
is forwarded live as a `delta` event.

**(8) The server closes the turn, and adds the verification layer.** On `done`, the server:
- attaches **assumptions** (e.g., if `list_region` ran: "Region membership resolved
  deterministically, not inferred by the model");
- builds the **scorecard panel from the tool-call arguments**, never by parsing the model's
  prose. A panel derived from the prose would agree with the prose by construction and
  verify nothing. This one shows the engine's numbers for the airports the model actually
  queried, so a wrong sentence would sit visibly beside the right bars;
- **saves the turn** to the session so the next question can follow up.

**(9) The browser renders the evidence with the answer.** Prose, the expandable tool trace,
per-airport percentile bars from the deterministic engine, and amber outlines reserved
exclusively for constraint flags (a legally capped airport changes the recommendation no
matter how good its metrics look). If voice is on, `speechSynthesis` reads the prose only;
caveats and evidence stay on screen where they cannot be skimmed past by ear.

If anything throws mid-stream, the error travels as an `error` event (the HTTP status is
already sent, so a 500 cannot). If no OpenAI-compatible key is configured, `/v1/chat` falls
back to the deterministic pipeline (section 6), so the demo still works.

### SSE event reference

| Event | Payload | Meaning |
|---|---|---|
| `round` | `{round}` | The loop started another model round (1..4) |
| `tool_call` | `{tool, args}` | The model requested a tool, shown before execution |
| `tool_result` | `{tool, args, result_preview}` | The engine's answer to that call |
| `delta` | text | One token of the final prose |
| `done` | `{answer, trace, assumptions, scorecards}` | Turn complete, panel data attached |
| `error` | `{error}` | Failure after the stream opened |

---

## 5. The determinism boundary

The single most important line in the architecture is the one between "the model decides"
and "the engine computes":

```
        MODEL DECIDES                          ENGINE COMPUTES
   which tools to call            │      every KPI, percentile, composite,
   in what order                  │      range, flag and derivation
   how to phrase the answer       │
                                  │      scoring/ may not import an LLM,
   may NOT: compute, estimate,    │      network lib, random, or even
   or emit numbers of its own     │      datetime — tests/test_purity.py
                                  │      walks the AST and FAILS THE BUILD
```

Enforced three ways, in increasing strictness:

1. **Purity test** (`tests/test_purity.py`): the scoring package cannot import the means to
   be nondeterministic. Not a convention, a build gate.
2. **Placeholder narration** (fallback pipeline): the model writes `{{SFO.load_factor}}`,
   never digits; the server substitutes real values.
3. **Numeric tripwire**: after substitution, any numeric literal not in the allow-set of
   actually computed values discards the prose and ships a deterministic template instead.

## 6. The fallback pipeline

When tool calling is unavailable, `agent/answer.py` runs a fixed pipeline. It is also the
reference implementation of the boundary above:

```
 question ─▶ parse (LLM: intent + surface strings only)
          ─▶ resolve (deterministic: "New England" → codes; "LA" → Ambiguous, ask)
          ─▶ fetch   (deterministic: API → cache → snapshot)
          ─▶ score   (PURE engine)
          ─▶ narrate (LLM: prose with {{placeholders}}, no digits)
          ─▶ verify  (deterministic: substitute, then numeric tripwire)
          ─▶ answer + assumptions
```

The LLM appears exactly twice, and in neither place can a number it produced reach the user.

## 7. Data flow, source to score

```
 BTS T-100 Segment Summary          BTS On-Time Performance
 (Socrata API, keyless,             (bulk PREZIP, flight-level:
  monthly per origin airport)        delay causes, taxi-out, distance)
        │                                   │
        ▼                                   ▼
 data/bts.py ── boundary quirks     scripts/build_delay_snapshot.py
 handled here (typing, pagination,  → 24 KB committed aggregate
 duplicate months)                    (NAS delay share, taxi-out,
        │                              stage-length buckets)
        ▼                                   │
   local cache ─▶ committed snapshot ◀──────┘
        │         (data/snapshots/, offline + reproducible)
        ▼
 AirportFacts (one per airport)
        ▼
 6 KPIs ─▶ percentile WITHIN HUB CLASS ─▶ fixed-weight composite per profile
        ▼
 ScoreCard: rank, composite, per-KPI contributions, flags
            (REGULATORY_CAPS registry · airside-first flag)
```

Two structural choices matter here: percentiles are computed **within hub class** so the
ranking answers "the most investment-worthy large hub" rather than becoming a size proxy,
and the same runway metric enters one profile as *headroom* and the other as *saturation*,
which is what stops a terminal recommendation at a runway-constrained airport.

## 8. Scoring methodology

First, what is scored: **not profitability** (construction cost, lease terms and discount
rates are not public), but **investment-worthiness signal**: demand pressure measured
against physical and legal capacity. The output is a defensible shortlist for a cost and
feasibility team to price.

Six KPIs, each computed as a **percentile rank within the airport's hub class**, combined
with fixed weights:

| KPI | What it measures | Side |
|---|---|---|
| `peak_pressure` | load factor + how far the peak month exceeds the mean | landside |
| `gate_saturation` | upgauging: carriers fly *bigger* aircraft when they cannot add *more* departures, the fingerprint of a gate/slot constraint | landside |
| `demand_growth` | 2-year passenger CAGR | shared |
| `international_intensity` | international share; those passengers need roughly 2x terminal area and dwell | landside |
| `airside_saturation` / `headroom` | peak-month departures per usable runway | airside |
| `delay_congestion` | NAS delay share (0.6) + normalized taxi-out (0.4) | observed |

**Weights per profile:**

| KPI | terminal_expansion | congestion |
|---|---|---|
| peak_pressure | 0.35 | 0.15 |
| gate_saturation | 0.20 | 0.15 |
| demand_growth | 0.20 | 0.15 |
| international_intensity | 0.15 | – |
| airside headroom / saturation | 0.10 (headroom) | 0.20 (saturation) |
| delay_congestion | – | 0.35 |

The principles behind the numbers:

- **Peer-group normalisation.** Percentiles are computed within hub class (large / medium /
  small / non-hub), so size decides which league an airport is in, never its score within
  it. Tested both ways: a global ranking inverts into a size artefact.
- **No weight exceeds 0.40.** Above that, a ranking is a one-metric sort wearing a costume.
- **One metric, opposite polarity.** Peak departures per runway enters the terminal profile
  as *headroom* (spare runway is a precondition for a terminal to pay off) and the
  congestion profile as *saturation* (runway saturation *is* congestion). Above the 90th
  percentile in a terminal query, an explicit flag fires: "airside-first, a terminal will
  not relieve this."
- **Observation outranks inference.** `delay_congestion` uses NAS delay share (delay
  attributed to airspace and airport capacity, as distinct from carrier or weather delay),
  which is causal and a rate, not a size proxy. Runway-count-based saturation is inference
  and is weighted below it.
- **Regulatory caps are data, not vibes.** A hand-maintained `REGULATORY_CAPS` registry
  (JFK, LGA, EWR, DCA, SNA) flags airports where flat growth is a legal ceiling, not weak
  demand, and changes the recommendation type.
- **Unmet demand is a stated proxy.** Fundamentally unobservable, so it is estimated from
  three observable leaks (load factor above the ~82% planning comfort level, NAS delay
  share, upgauging without frequency growth) and returned as a **range with a confidence
  label**, never a point estimate.

Full rationale, including what was tried and rejected: `DESIGN.md` section 4.

## 9. Where and how AI is used

The LLM has exactly four jobs, two per path, and none of them is arithmetic:

| # | Path | Job |
|---|---|---|
| 1 | Agent (default) | Choose **which** of the 12 tools to call and **in what order** |
| 2 | Agent (default) | Phrase the final answer over the tools' structured results |
| 3 | Fallback pipeline | Parse intent and extract surface strings ("New England", "LA"), never resolve them |
| 4 | Fallback pipeline | Narrate prose containing `{{CODE.field}}` placeholders, never digits |

What the model is **never** allowed to do, and how each is enforced:

- **Compute or emit a number**: the purity test (build gate), placeholder narration, and
  the numeric tripwire (section 5).
- **Resolve entities**: `agent/resolve.py` maps strings to airport codes deterministically;
  "LA" raises Ambiguous rather than guessing.
- **Decide whether a caveat appears**: scope caveats (e.g., Anchorage's domestic-only data)
  are emitted by the tools themselves and pinned by tests.

The model itself is a runtime choice: `agent/llm.py` supports OpenAI, Groq, Gemini, and
Anthropic behind one interface with no SDK dependency; any one key works.

## 10. Key tradeoffs

- **Cached monthly BTS snapshot over live flight tracking.** Investment decisions rest on
  12-36 month demand trends; a live feed (OpenSky's free tier serves one hour of history)
  would demo well while being unable to answer any question actually asked.
- **LLM narration with server-side numeric substitution over letting the model emit
  numbers.** A hallucinated capacity figure in an investment tool is an unacceptable
  failure mode; substitution plus the tripwire makes the boundary mechanically testable
  rather than dependent on prompt wording.
- **Transparent weighted percentile model over a learned model.** There is no labelled
  "renovation profitability" ground truth to train on, and an additive model returns
  per-KPI contributions that directly answer the "why", which a black box structurally
  cannot.
- **Hub-class percentiles over a global ranking.** Tested both ways; global ranking turns
  the model into a size artefact and the top terminal candidates come out as small
  regional fields.
- **A range over a point estimate for unmet demand.** A single number would imply
  precision the method does not have.
- **stdlib over frameworks** (no FastAPI, no LangGraph, no pytest): the flow is linear and
  the routing trivial, so each framework would add a dependency without adding capability,
  and the project's claim to run on a clean clone with zero installs would quietly become
  false.
