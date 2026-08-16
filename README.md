# AirportIQ

An agent that ranks US airports as candidates for capacity investment, from public aviation data.

Ask it questions like:

- *Which airports in New England are strong candidates for terminal expansion?*
- *Compare LA and Santa Ana airport congestion levels.*
- *What is the unmet flight demand at SFO and why?*

## Run it

```bash
python scripts/build_and_rank.py --profile terminal_expansion   # no LLM, no API key needed
cp .env.example .env                                            # add ONE key for the chat agent
python scripts/ask.py "Which New England airports need terminal expansion?"
pytest tests/ -q                                                # or: python tests/test_purity.py
```

`build_and_rank.py` runs with **no API key at all** — the scoring path contains no model.
A committed snapshot (`data/snapshots/`) means it also runs with **no network**.

Any one of `OPENAI_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY` or `ANTHROPIC_API_KEY` works;
Groq and Gemini have free tiers that are ample here. Set `LLM_PROVIDER` / `LLM_MODEL` to choose.

## The one thing worth looking at first

`tests/test_purity.py` walks the AST of the scoring package and **fails the build** if it
imports any LLM library, any network library, or even `datetime`.

That is the mechanical answer to "the scoring must be deterministic, not only LLM output". The
model classifies intent and phrases the answer. It never touches the arithmetic — and that is
enforced, not intended.

To see it for yourself: run `build_and_rank.py` (pure, no model) and then ask the agent the same
question. Identical numbers, because it is the same function.

## Data

**BTS T-100 Segment Summary by Origin Airport** via the Socrata API — public, keyless,
~131,700 rows, currently through 2026-04 (roughly a four-month lag).

Three things that will bite anyone reusing this source, all handled at the boundary in
`data/bts.py`:

1. The filter field is `origin_airport_code`. Using `origin` returns HTTP 400.
2. Every value returns as a **string**, including numerics. Coercion failure becomes `None`
   ("unknown"), never `0.0` ("we know it is zero") — a missing load factor read as zero would
   rank a data gap as perfectly uncongested.
3. Socrata **omits null fields entirely** from a row, so small airfields return no international
   keys at all. Never assume a key exists.

And the schema trap that silently corrupts totals:

```
domestic_passengers + outbound_international_1 = total_passengers    (exact)
```

`total_*` is departure-based (≈ enplanements). `inbound_international_*` is a **separate arrivals
block, not part of the total**. Summing everything matching "international" inflates SFO by
about a third.

## What it does not do

It does not rank profitability — that needs construction cost, land and lease data that are not
public. It ranks *investment-worthiness signal*: demand pressure against physical and legal
capacity, which is the input to a profitability model rather than the model itself.

Full reasoning, weights, and the complete limitations list are in [DESIGN.md](DESIGN.md).
