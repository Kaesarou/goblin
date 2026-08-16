# Side-neutral market research V1

## Scientific objective

This contract adds prospective, read-only observations for testing whether a
market state was capturable by Goblin's actual deterministic lifecycle and
whether BUY or SELL was the better counterfactual direction. It does not add a
strategy, selector, shadow model, signal or broker action.

The target for later offline work is:

> Can this trade finish net positive with Goblin's current lifecycle?

The four experimental profitability heads remain offline: EU BUY, EU SELL,
US BUY and US SELL. Preferred side and dual-lifecycle labels are also offline.
Future labels are keyed by `research_state_id` and derived as `BUY_ONLY`,
`SELL_ONLY`, `BOTH` and `NEITHER`.

> « Cette PR ne démontre pas l’existence d’un edge rentable. Elle ajoute une nouvelle modalité d’information et un protocole prospectif permettant de tester si cette information améliore Capturability et Direction. »

## Read-only architecture

The research path is a sidecar. Accepted market snapshots feed the existing
runtime first. The research pipeline can observe them, but nothing produced by
research is read by candidate generation, selection, risk or execution.

The sidecar has no return path into:

- candidate generation or candidate side;
- `MANAGED_EDGE_V1`, ranking or top-N;
- `EntryDecision` or `RiskManager`;
- cooldowns, capacity, positions or session trade counts;
- broker tasks or orders;
- TP, SL, breakeven, trailing, stale exit or force close;
- live P&L.

`RESEARCH_ENABLED=false` disables the sidecar. In that mode no payload-schema
observer is injected into the eToro WebSocket feed, no research field-path
samples are constructed and no research journal is opened. A compact disabled
`research_summary.json` is still written so the run is explicitly auditable.

The eToro parser exposes an optional neutral transport callback consisting of
symbol, PATCH, merged state and receive time. `MarketDataEvent` contains no
research DTO, and the core `market_data`, `brokers`, `strategies`, `execution`
and `risk` packages do not import `app.research`.

At a due research boundary, states are calculated independently per symbol and
persisted using a single `JsonlJournal.write_many()` call. A per-symbol
calculation failure does not suppress other states. A batch-write failure is
counted and does not interrupt trading. A final exception barrier around the
runtime research boundary guarantees that an unexpected sidecar exception does
not escape into the trading loop.

## Sampling and session contract

`SIDE_NEUTRAL_MARKET_RESEARCH_STATE_V1` uses:

- population: every configured `EQUITY_EU` and `EQUITY_US` watchlist symbol;
- cadence: one state every five minutes per symbol;
- candidate independence: no candidate is required;
- side neutrality: no BUY/SELL alignment or preferred side is an input;
- missing-data preservation: missing quotes/candles produce an incomplete state
  instead of removing the observation;
- prospective start: only `state_at > runtime_started_at` is eligible;
- session eligibility: the current session must be active and collecting;
- final-hour exclusion: remaining session time at `state_at` must be strictly
  greater than 60 minutes;
- no portfolio gating: open positions, capacity, cooldowns and trade-count
  limits do not suppress market-state collection.

Cooldown and capacity describe the portfolio, not the market. They therefore do
not belong in the sampling population.

`research_state_id` is stable across runs: `rs1_` plus the first 24 hexadecimal
characters of SHA-256 over contract version, normalized symbol, session key and
UTC `state_at`.

## Causality

For a state at `T`:

- a quote is eligible only if both market timestamp and receive timestamp are
  strictly earlier than `T`;
- microstructure windows are half-open `[T-W,T)`;
- an M1 candle is eligible only when `closed_at <= T`;
- benchmark, breadth, sector and relative-spread histories are prior-only;
- no candidate, future candle or future lifecycle result is read.

Each record exposes `state_at`, `feature_cutoff_at`,
`latest_market_timestamp`, `latest_market_received_at` and
`latest_closed_candle_timestamp` so causality can be audited offline.

## Research-state schema

`research.jsonl.gz` uses the normal JSONL envelope with event type
`research_state`. The payload is flat and versioned. It contains identity,
session context, the latest causal quote, compact M1 features, side-neutral
market context, microstructure and availability/provenance fields.

Two candle availability fields are deliberately distinct:

- `latest_candle_available`: at least one causal closed M1 candle exists at or
  before `state_at`;
- `boundary_candle_available`: a causal M1 candle exists with
  `closed_at == state_at`.

This prevents a stale prior candle from being counted as complete boundary data.
The research health summary tracks both no-candle and no-boundary-candle cases.

The record does not copy a `TradeCandidate`, full `MarketContext`, complete MTF
object, candle history or raw WebSocket payload.

## M1 state features

Percentage returns are percentage points. The contract includes close returns
at 1/3/5/15/30/60 minutes, realized volatility, ATR, range location,
path-efficiency, compression, session return, opening-range state and exact
60-minute coverage/quality counts. Features requiring an exact window are null
when that window is incomplete.

## Side-neutral context

The research context contains symbol session return, benchmark return/momentum/
spread/freshness, breadth, sector breadth, relative strength, regime and
relative-spread statistics. It never contains side alignment.

Prospectively these values use the accepted runtime market stream, including
accepted WebSocket messages whose price may be unchanged. This matters for the
historical reconstructibility contract described below.

## `MICROSTRUCTURE_CONTRACT_V2`

The runtime version is `etoro_microstructure_v2`. Input is accepted eToro
WebSocket snapshots only. REST fallback may provide provenance for the base
research state but is excluded from sub-minute microstructure.

Primary windows are 10, 30 and 60 seconds. Memory is bounded to 4,096
observations per symbol and a 120-second horizon. Quote ingestion is O(1);
feature construction occurs only on the five-minute research boundary.

For a numeric path `x`:

```text
U(x) = count(delta(x) > 0)
D(x) = count(delta(x) < 0)
C(x) = U(x) + D(x)
tick_imbalance(x) = (U-D)/(U+D), or 0 for a flat path
```

Main families are:

- activity: `quote_count`, `quote_rate_hz`, sample count and temporal coverage;
- mid path: return, absolute path, tick imbalance, change count, directional
  persistence and path efficiency;
- bid/ask: tick imbalances, change counts and bid-vs-ask change imbalance;
- broker-last canonical value: tick imbalance, change count,
  `last_value_change_ratio` and position in spread;
- spread: mean/change, plus min/max/range on 60 seconds;
- timing: median inter-arrival and robust burstiness.

`last_value_change_ratio` is explicitly a ratio of canonical broker-last value
changes to canonical broker-last transitions. It is **not** a rate of actual
`LastExecution` retransmission. PATCH presence in the payload-schema observer is
the source of truth for whether `LastExecution` was retransmitted.

These are quote-flow and price-path features. They are not traded BUY/SELL
volume, bid/ask size, L2 depth or an order book.

## eToro payload-schema observer

`etoro_payload_schema_v2` observes field paths and types without retaining raw
payloads. It walks objects to depth two, so nested fields such as
`OrderBook.BidSize` could be discovered if eToro actually sends them. Arrays
are recorded as containers but their elements are not traversed.

The observer retains, within strict bounds:

- observed types;
- first/last seen time;
- PATCH and merged-state presence counts separately;
- asset classes;
- finite numeric min/max;
- container-size min/max;
- at most three bounded scalar examples.

State is globally bounded to 256 paths. Sensitive-looking paths never retain
examples. New paths/types trigger an atomic update; ordinary updates are
rate-limited. No raw payload journal is created.

## Files, retention and weekly ZIP compatibility

All required analysis artifacts stay under `data/logs/`:

```text
data/logs/
  etoro_payload_schema.json
  runs/run_*/
    research.jsonl.gz
    research_summary.json
    etoro_payload_schema.json
    trades.jsonl.gz
    market.jsonl.gz
    candles.jsonl.gz
    errors.jsonl.gz
    debug_decisions.jsonl.gz
    manifest.json
    summary*.json
```

Run rotation removes the complete run directory. Zipping `data/logs/` remains
an exhaustive weekly export.

Run-manifest schema V14 is additive. Existing V13 business semantics remain
unchanged. Research contracts and paths are added separately. The relevant
research contracts are currently:

- side-neutral research state V1;
- `etoro_microstructure_v2`;
- `etoro_payload_schema_v2`;
- `research_summary_v1`;
- `research_reconstructibility_v2`.

`MARKET_DATA_MODEL_VERSION` remains `market_data_v2_ws_clocked_2` because the
temporary research-only field introduced earlier on the branch was removed;
the core persisted Market Data contract therefore has no net research change.

## Research health and completeness

`research_summary.json` is run-scoped, bounded and atomically replaced at
startup, after each observed research boundary and on clean shutdown. It does
not write per quote.

`expected_state_count` means: at each unique five-minute boundary actually
observed after run start, one state for every configured research symbol whose
session is research-tradable at that boundary, even if its inputs are missing.

`emitted_state_count` increments only when the corresponding JSONL state has
been successfully persisted. Their difference therefore exposes collection
holes.

The summary also records calculation/write/schema failures, missing quotes,
missing any candle, missing the exact boundary candle, microstructure
availability, first/last state times, boundary counts, calculation/persistence
latencies, cumulative processing time and research-journal open count.

## Log-volume budget

There is no new persisted event per tick. Ticks update bounded in-memory state;
one compact record per eligible symbol is persisted every five minutes.

The existing reproducible log-budget validation measured all new research
artifacts at approximately **8.90%** of the corresponding synthetic market
stream, below the 10% target. The July-29 archive-shaped upper-bound estimate
for the research stream alone was approximately **5.65%** of
`market.jsonl.gz`.

These are engineering sizing checks, not market-performance claims.

## Boundary performance validation

Two complementary tests exist.

`test_research_boundary_performance.py` isolates orchestration and persistence
for 110 symbols. It verifies one gzip open per boundary instead of one per
symbol and retains the deliberately generous 5,000 ms catastrophic-regression
guard.

`test_research_boundary_realistic_performance.py` addresses the limitation of
the earlier stub benchmark. It constructs a 110-symbol EU/US universe using the
real `InstrumentRegistry`, real `MarketContextService`, real
`FullSessionMultiTimeframeService`, 65 populated M1 bars per symbol and populated
session market history before executing a research boundary. It verifies 110
expected/emitted states, zero calculation failures, one research-journal open
and a complete wall-clock duration below the same 5,000 ms catastrophic guard.

The GitHub Actions run validating this revision executed the full **727-test**
suite successfully in **5.12 s**, including the realistic benchmark. Pytest
captures successful-test stdout, so an individual benchmark latency is not
persisted as a stable CI metric. The 5,000 ms threshold is intentionally a
catastrophic-regression detector, **not** a latency SLA and not a claim that the
boundary normally takes 5 seconds.

## Historical reconstructibility — V2

Historical `market.jsonl.gz` retains accepted **price-changing** snapshots, not
every accepted WebSocket message. A prospective unchanged message can refresh
receive/timestamp state without producing an old `market_price_changed` record.
Therefore freshness-dependent context cannot honestly be labelled exact when
reconstructed from old archives.

`research_reconstructibility_v2` classifies each feature:

| Class | Main families | Historical policy |
|---|---|---|
| `EXACT_HISTORICAL` | M1/candle research features whose retained candle inputs exist | materialize value; otherwise null |
| `HISTORICAL_PRICE_CHANGE_PROXY` | side-neutral market context and microstructure price-path features reconstructed from retained price-changing events | use an explicitly proxy-labelled value or null; never present as prospective-exact |
| `PROSPECTIVE_ONLY` | exact quote count/rate, temporal coverage, last-value-change ratio, inter-arrival/burstiness, unchanged PATCH activity and payload-schema presence | null/unavailable for historical weeks |

In particular, benchmark freshness/availability, breadth coverage, sector
availability and derived regime are proxies historically because their
prospective values can depend on accepted unchanged WebSocket observations that
old market logs did not retain.

This distinction is machine-readable in the run manifest and prevents future
analysis from silently comparing a historical price-change proxy with a
prospective exact transport measurement.

## Failure isolation

Research failures are isolated at several levels:

- rejected/quarantined market data never enters accepted research inputs;
- per-symbol state calculation failure does not suppress other symbols;
- batch journal failure is recorded and does not enter trading decisions;
- payload-schema failures are counted and bounded;
- a final runtime exception barrier prevents an unexpected research-boundary
  exception from escaping into the trading loop;
- a failed boundary is not retried on every tight-loop iteration.

## Interpretation and limits

Classification of the current work:

- **Certain contract inconsistency fixed:** historical context was initially
  classified as exact even though old market logs omit accepted unchanged
  WebSocket messages. V2 now classifies it conservatively as a proxy.
- **Robustness improvement:** exact-boundary candle availability is distinct
  from merely having an older candle.
- **Robustness improvement:** the runtime now has a final research-only
  exception barrier.
- **Dead-code removal:** the old single-symbol `maybe_emit()` path was removed;
  tests use the production batch `emit_boundary()` path.
- **Measured engineering risk:** synchronous boundary processing is guarded by
  both an I/O-focused benchmark and a real-context/MTF benchmark. No evidence
  currently shows a trading defect caused by that work.
- **Known data limit:** historical logs cannot recreate exact unchanged-message
  activity or payload PATCH presence.
- **Statistical hypothesis:** sub-minute eToro quote-path information may improve
  Capturability or Direction. This PR does not establish that it does.

No external data provider, L2/order-book abstraction, live ML inference, second
lifecycle engine, SQLite research store or hidden selector is introduced.
