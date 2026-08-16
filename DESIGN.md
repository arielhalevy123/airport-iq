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

### Weights

**terminal_expansion** — peak_pressure 0.35, gate_saturation 0.20, demand_growth 0.20,
international_intensity 0.15, airside_headroom 0.10

**congestion** — airside_saturation 0.35, demand_growth 0.20, peak_pressure 0.15,
gate_saturation 0.15, international_intensity 0.15

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
- **Delay data is not yet wired in.** BTS On-Time Performance provides `TaxiOut` and `NASDelay`,
  and `NASDelay ÷ total delay minutes` isolates the airport-attributable share. That is the first
  thing to add with more time.
- **Data lags roughly four months** (currently through 2026-04). Fine for trend analysis; stated
  in every answer.

## 8. Not modelled at all

Construction cost. Land availability. Political and NEPA feasibility. Airline
majority-in-interest lease clauses, which are often the actual binding constraint on capital
projects. Debt capacity and PFC/AIP headroom. ROI, NPV, payback. Origin-destination versus
connecting mix, which determines whether the need is curbside or concourse.
