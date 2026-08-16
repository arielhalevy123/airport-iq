# AirportIQ — Design & Architecture

An agent that ranks US airports as candidates for capacity investment, from public aviation data.

Built in a day. What follows is what it does, what it deliberately does not do, and why.

---

## 1. What this actually ranks

**It does not rank profitability.** "Most profitable renovation" needs construction cost, land
availability, lease terms and a discount rate. None of those are public, and estimating them
would be fabrication dressed as analysis.

What it ranks is **investment-worthiness signal**: demand pressure measured against physical and
legal capacity. It is the demand-side input to a profitability model, not the model itself. Its
job is to take a large candidate set down to a defensible shortlist that a cost and feasibility
team then prices.

That distinction is stated first because everything else follows from it.

---

## 2. Architecture

Two paths, and which one runs depends on the question.

**Tool-calling agent** (default). The model is given six read-only tools and decides which to
call and in what order. This is what makes open-ended questions answerable — a fixed pipeline
can only answer questions whose shape it anticipated. Asked "what is the unmet demand at SFO
and why", it independently calls `estimate_unmet_demand` then `get_delay_breakdown`: the number,
then the cause.

```
  question ──▶ model picks tools ──▶ tools return ENGINE values ──▶ model narrates
                     ▲                        │
                     └────────────────────────┘   up to 4 rounds, 10 calls, repeats cached
```

Tools: `list_region`, `get_airport_metrics`, `compare_airports`, `get_delay_breakdown`,
`estimate_unmet_demand`, `rank_airports`.

The boundary does not move. Every tool returns values computed by the pure engine — the model
chooses the *questions*, never the *answers*. Giving a model tools is usually where determinism
quietly dies, because the model starts computing in prose over tool output. The defence is that
tools return structured values with provenance, and `/v1/score` reproduces any figure quoted.

**Deterministic pipeline** (fallback, and the reference implementation):

```
  user question
       │
       ▼
  ┌─────────────┐
  │ parse       │  LLM — classify intent, extract SURFACE STRINGS only
  └─────┬───────┘
        ▼
  ┌─────────────┐
  │ resolve     │  deterministic — "New England" → 6 states → airport codes
  └─────┬───────┘  "LA" → raises Ambiguous rather than guessing
        ▼
  ┌─────────────┐
  │ fetch       │  deterministic — BTS API, cache, then committed snapshot
  └─────┬───────┘
        ▼
  ┌─────────────┐
  │ score       │  PURE — no network, no model, no clock, no randomness
  └─────┬───────┘
        ▼
  ┌─────────────┐
  │ narrate     │  LLM — writes prose containing {{SFO.load_factor}} placeholders,
  └─────┬───────┘        never digits
        ▼
  ┌─────────────┐
  │ verify      │  deterministic — substitute values, then scan for any numeric
  └─────┬───────┘   literal not in the allow-set. Violation → template fallback
        ▼
     answer + assumptions
```

The LLM appears exactly twice and cannot produce a number that reaches the user in either place.

---

## 3. How the model is walled off from the arithmetic

The brief asks for "deterministic scoring or ranking logic (not only LLM output)". Three
mechanisms, in increasing strictness:

**a. A purity test that fails the build.** `tests/test_purity.py` walks the AST of every module
under `scoring/` and rejects any import of `openai`, `anthropic`, `langchain`, `httpx`,
`requests`, `random`, `secrets` — or even `datetime` and `time`, because a clock is
nondeterminism too.

So the answer to *"how do you know the LLM cannot change a score?"* is not "that's the design."
It is: the build fails.

**b. The narrate step cannot emit digits.** The model receives computed values and must reference
them as `{{CODE.field}}`. The server substitutes real numbers afterwards.

**c. A numeric tripwire.** After substitution, every numeric literal in the final text is checked
against the allow-set of values actually substituted (years and ordinals excepted). If the model
smuggled in a figure, the prose is discarded and replaced with a deterministic template. The
guard fires rather than the answer shipping.

**The reviewer's proof:** run `scripts/build_and_rank.py` — a pure path with no LLM — then ask the
agent the same question. The numbers are identical because they come from the same function. If
they ever diverge, that is a bug in the narrative layer, not a difference of opinion.

---

## 4. Scoring methodology

Six KPIs, each a **percentile rank within the airport's hub class**, combined with fixed weights.

| KPI | What it measures | Side |
|---|---|---|
| **peak_pressure** | load factor + how far the peak month exceeds the mean | landside |
| **gate_saturation** | upgauging — carriers fly *bigger* aircraft when they cannot add *more* departures. The fingerprint of a gate/slot constraint | landside |
| **demand_growth** | 2-year passenger CAGR | shared |
| **international_intensity** | international share; those passengers need ~2× terminal area and dwell | landside |
| **airside_saturation / headroom** | peak-month departures per usable runway | airside |
| **delay_congestion** | NAS delay share (0.6) + normalised taxi-out (0.4) | observed |

### Weights

**terminal_expansion** — peak_pressure 0.35, gate_saturation 0.20, demand_growth 0.20,
international_intensity 0.15, airside_headroom 0.10

**congestion** — delay_congestion 0.35, airside_saturation 0.20, demand_growth 0.15,
peak_pressure 0.15, gate_saturation 0.15

`peak_pressure` leads the terminal profile because terminals are sized on peak-hour design-day
flow (IATA ADRM), not annual totals — so the metric closest to the design condition carries the
plurality. **No weight exceeds 0.40**, a deliberate ceiling: above that the ranking is a
one-metric sort wearing a costume.

### The structural point about the two profiles

They are not a reshuffle. The *same* measure — peak-month departures per usable runway — enters
the terminal profile as **headroom** (weight 0.10: spare runway is a precondition for a new
terminal to pay off) and the congestion profile as **saturation** (weight 0.35: runway saturation
*is* congestion). One metric, opposite polarity.

This is what stops the model recommending a terminal at an airport whose bottleneck is runways.
When airside saturation exceeds the 90th percentile in a terminal query, the output carries an
explicit flag: *"airside-first: a terminal will not relieve this."*

### Observed congestion beats inferred congestion

FAA ASPM is login-gated, which removes the obvious congestion source. But BTS publishes
flight-level On-Time Performance at a stable bulk URL, and it carries the metric that actually
attributes congestion to the *airport*:

    NAS delay share = NASDelay / total delay minutes

NAS delay is National Airspace System delay — volume, capacity, flow control and holding. As
distinct from `CarrierDelay` (the airline's own problem) and `WeatherDelay` (nobody's fault).
An airport where NAS delay dominates is one hitting its capacity ceiling. That is **causal**,
not correlational, and it is a **rate**, so it is not a size proxy.

Combined with mean taxi-out time — the cleanest ground-congestion signal — this became
`delay_congestion`, weighted 0.35 in the congestion profile. `airside_saturation`, which infers
capacity from runway *count*, dropped to 0.20 as a consequence: **observation outranks
inference**, and runway count was always a proxy.

The metric validates itself. Ranking April 2026 by NAS delay share puts EWR (32.5%), JFK (31.6%)
and LGA (27.8%) at the top — the New York metroplex, the most airspace-constrained region in the
United States. That is the result you would want from a working measure. SFO shows 25.8 minutes
mean taxi-out, second only to JFK, with 26.4% of flights delayed 15+ minutes.

One month is committed as a 24 KB aggregate so the reviewer gets real delay data without a 30 MB
download or a BTS account.

### Unmet demand: a proxy, stated as one

The hardest question in the brief. The first implementation refused it — "unmet demand data is
not available" — which is technically true and the wrong answer, because the brief asks it.

Suppressed demand genuinely never enters the data: the passenger who did not book, the airline
that did not add a frequency. So the estimate is built from three observable leaks — load factor
above the ~82% planning comfort level, airspace-attributable delay share, and upgauging without
frequency growth — and returned as a **range**, because a point estimate would imply precision
the method does not have.

For SFO: 3-9% of current traffic, roughly 0.9M-2.4M passengers a year, at medium confidence.

And the mechanism, which is the half that matters: **SFO's parallel runway pairs are roughly
750 ft apart — too close for simultaneous independent approaches under instrument conditions.
When the marine layer moves in the arrival rate roughly halves and the published schedule cannot
be flown.** That is a physical cause, not a restatement of the question, and it says the
investment case at SFO is airside rather than terminal.

### Conversational follow-up

"Compare LAX and SNA" then "why?" must work. Airports, profile and already-stated assumptions
carry across turns.

**Numbers deliberately do not.** Every answer re-reads values from the score cards. If a figure
could be inherited from conversation history, the model could restate a stale or misremembered
number — reintroducing precisely the hallucination the numeric guard exists to prevent, through
the back door.

*Why this is not LangGraph:* `agent/session.py` is shaped like a checkpointer — thread id,
persisted state, a reducer merging each turn. But this pipeline is strictly linear, with no
branching, cycles or agent handoff, so LangGraph's router would add a dependency and no
capability, and the repo would stop running on a clean clone. The framework earns its place when
routing is dynamic. Adding it because it looks good is the scope sprawl this role screens for.

### Peer-group normalisation — the decision that makes it work

Percentile ranks are computed **within hub class** (large / medium / small / non-hub by share of
national departing passengers), not globally.

This was tested both ways. Without peer grouping the pure-ratio model does not become a size
proxy — it **inverts**, and the top terminal candidates come out as small regional fields.
Nobody funds a terminal at a regional airport over Newark.

The principle: **size decides which league you are in; it does not decide your score within it.**
The output is "the most investment-worthy large hub", which is also the question a client asks.

---

## 5. Regulatory caps

Some airports are capped by law, not demand. SNA has a noise curfew and a passenger cap; DCA has
slots and a perimeter rule; JFK and LGA are slot-controlled.

At those airports flat growth is a **ceiling, not weakness**, and a naive model reads it as dying
demand. `REGULATORY_CAPS` is a hand-maintained registry with the authority named — deterministic
and auditable, never inferred at runtime.

The flag changes the *recommendation type*, which is the part that matters commercially: at a
capped airport the answer is not "build gates", because gates that cannot legally be used return
nothing.

---

## 6. Key tradeoffs

**We chose a cached monthly BTS snapshot over live flight tracking** because investment decisions
rest on 12-36 month demand trends, and OpenSky's free tier serves one hour of history — a live
feed would demo well while being unable to answer any question actually asked.

**We chose LLM narration with server-side numeric substitution over letting the model emit
numbers** because a hallucinated capacity figure in an investment tool is an unacceptable failure
mode, and substitution plus a numeric tripwire makes the boundary mechanically testable rather
than dependent on prompt wording.

**We chose a transparent weighted percentile model over a learned one** because there is no
labelled "renovation profitability" ground truth to train against, and an additive model returns
per-KPI contributions that directly answer the "why" in the user's question — which a black-box
model structurally cannot.

---

## 7. Known limitations, stated rather than discovered

- **Gate counts are absent.** The single best landside metric, and it exists in no free
  structured source (checked: NASR, ADIP, FAA capacity profiles). `gate_saturation` is an
  indirect proxy via upgauging. Notably, FAA's FACT3 states that gates do not constrain capacity
  at most airports, which is why the omission is defensible rather than merely unavoidable.
- **Runway count is not runway capacity.** Two parallels 4,300 ft apart permit simultaneous
  independent approaches; two at 750 ft do not — which is exactly why SFO's arrival rate roughly
  halves in low visibility. The named upgrade path is to replace the denominator with FAA
  Capacity Profile called rates.
- **Monthly is the finest granularity BTS offers.** Real terminal sizing uses peak-hour design
  day. This is the model's largest single approximation.
- **Small hubs have few peers**, so their percentile ranks are unstable — a small-sample artefact,
  not a strong signal.
- **Unmet demand is fundamentally unobservable.** Suppressed demand never appears in the data by
  construction. Any figure here is a proxy with a stated counterfactual.
- **Delay data covers one month** (April 2026). A single month is seasonal; a trailing twelve
  would be more robust and is a straightforward extension of `build_delay_snapshot.py`.
- **Data lags roughly four months** (currently through 2026-04). Fine for trend analysis; stated
  in every answer.

## 8. Evaluation

`evals/run_evals.py` runs eight cases: the four questions from the brief, two refusals, a
nonexistent airport, and a follow-up. It asserts on the **structured payload** — resolved
intent, airports referenced, assumptions disclosed — and deliberately not on prose style. A
suite that fails when a sentence is reworded gets ignored within a week, and an ignored suite
is worse than none because it looks like coverage.

It earned itself immediately. Within minutes it caught:

- the Anchorage case regressing after a parser prompt change, because the model sometimes
  extracts "flights out of Anchorage" rather than "Anchorage";
- the fix for that resolving **"Atlantis" to LAX**, since the string contains "la" — inventing
  an airport for a nonsense query, which is the worst failure this system can have. Now
  word-boundary matched, with a regression test;
- `rank_airports` returning only BOS for New England, a worse answer than the pipeline it
  replaced.

All three would have shipped otherwise.

## 9. Not modelled at all

Construction cost. Land availability. Political and NEPA feasibility. Airline
majority-in-interest lease clauses, which are often the actual binding constraint on capital
projects. Debt capacity and PFC/AIP headroom. ROI, NPV, payback. Origin-destination versus
connecting mix, which determines whether the need is curbside or concourse.
