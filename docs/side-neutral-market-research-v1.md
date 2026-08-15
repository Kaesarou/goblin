# Side-neutral market research V1

## Scientific objective

This contract adds prospective, read-only observations for testing whether a
market state was capturable by Goblin's actual deterministic lifecycle and
whether BUY or SELL was the better counterfactual direction. It does not add a
strategy, selector, shadow model, signal or broker action.

The target for later offline work is:

> Can this trade finish net positive with Goblin's current lifecycle?

The four experimental heads remain offline:

- EU BUY profitable;
- EU SELL profitable;
- US BUY profitable;
- US SELL profitable.

Likewise, preferred side, BUY/SELL/NO TRADE and the BUY/SELL dual-lifecycle
labels are not computed live. Future labels require future data and must be
produced offline by the shared deterministic lifecycle, keyed by
`research_state_id`. The derived classes are `BUY_ONLY`, `SELL_ONLY`, `BOTH`
and `NEITHER`.

> « Cette PR ne démontre pas l’existence d’un edge rentable. Elle ajoute une nouvelle modalité d’information et un protocole prospectif permettant de tester si cette information améliore Capturability et Direction. »

## Read-only architecture

Only accepted `MarketDataEvent.snapshot` values enter the microstructure
accumulator. Rejected and quarantined quotes never enter it. The payload-schema
observer receives the server field schema separately, because its purpose is to
discover transmission shape rather than to define a valid price observation.

At each finalized M1 candle, the sidecar checks whether the candle close is an
exact five-minute boundary. For every EU or US equity in the watchlist, it can
then write one flat state to the dedicated research journal.

The sidecar has no return path into:

- candidate generation or candidate side;
- `MANAGED_EDGE_V1`, ranking or top-N;
- `EntryDecision` or `RiskManager`;
- cooldowns, capacity, positions or session trade counts;
- broker tasks or orders;
- TP, SL, breakeven, trailing, stale exit or force close;
- live P&L.

Research is invoked after the existing accepted-market and finalized-candle
business writes. Every public sidecar boundary contains its own failure
isolation. A research I/O or calculation failure is logged by the application
logger and returns without interrupting trading.

`RESEARCH_ENABLED=false` disables the sidecar. Its default is `true` so the
prospective protocol is active after deployment.

## Sampling and session contract

`SIDE_NEUTRAL_MARKET_RESEARCH_STATE_V1` applies these rules:

- population: every `EQUITY_EU` and `EQUITY_US` symbol in the configured
  watchlist;
- cadence: one opportunity every five minutes per symbol, at exact UTC minute
  boundaries divisible by five;
- candidate independence: a state does not require a Goblin candidate;
- side neutrality: no BUY/SELL alignment, candidate side or preferred side is
  an input;
- active-session requirement: the current `TradingSessionService` decision
  must allow a new entry;
- last-hour exclusion: remaining session time is recomputed at `state_at` and
  must be strictly greater than 60 minutes;
- no portfolio gating: open positions, max positions, cooldowns, max trades and
  portfolio capacity are deliberately ignored.

Cooldown and capacity describe the portfolio, not the market. They therefore
cannot suppress a market-state observation. The final hour is different: the
current session contract forbids a new entry there, so such a state cannot be a
valid prospective entry observation under the target lifecycle.

`research_state_id` is stable across runs. It is the prefix `rs1_` followed by
the first 24 hexadecimal characters of SHA-256 over:

```text
contract version | normalized symbol | session key | UTC state_at
```

## Causality

For a state at `T`:

- a quote is eligible only if both `market_timestamp < T` and
  `received_at < T`;
- a microstructure window is half-open: `[T-W, T)`;
- an M1 candle is eligible only when `closed_at <= T`; the candle closing at T
  contains samples from the preceding interval;
- market, benchmark, breadth, sector and relative-spread histories require
  both broker and receive timestamps strictly before T;
- no candidate, future benchmark value, future candle or future lifecycle
  outcome is read.

Every record exposes `state_at`, `feature_cutoff_at`,
`latest_market_timestamp`, `latest_market_received_at` and
`latest_closed_candle_timestamp`, plus the fixed cutoff convention. This makes
lookahead checks possible without expanding the record with complete context
objects.

## Research-state schema

`research.jsonl.gz` uses the normal JSONL envelope and event type
`research_state`. Its payload is flat and versioned:

- identity and provenance: schema/contract versions, ID, symbol, asset class,
  session key and cutoff convention;
- temporal context: session start/end, session minute, recomputed minutes to
  close, progress, UTC weekday and minute of day;
- latest causal quote: bid, ask, broker last when available, midpoint, absolute
  and percentage spread, `quote_available`, broker-last provenance and
  freshness;
- latest candle quality: sample count, carried-forward/degraded flags and
  source-price age;
- availability: expected/available feature counts, completeness ratio and
  window/context flags;
- compact M1, side-neutral market-context and microstructure features.

The record does not copy a `TradeCandidate`, full `MarketContext`, MTF object,
candle series or raw WebSocket payload.

### M1 state features

All percentage returns use closing prices and are expressed in percentage
points.

| Family | Definition |
|---|---|
| Returns | close-to-close return over 1, 3, 5, 15, 30 and 60 minutes |
| Realized volatility | population standard deviation of exact M1 returns over 5, 15, 30 and 60 minutes |
| ATR | mean true range over exact 5, 15, 30 and 60-minute paths, divided by current close |
| Range location | current close position and distance to high/low over exact 15, 30 and 60-minute windows |
| Path efficiency | absolute net close displacement divided by absolute close path over 5 and 15 minutes |
| Compression | mean true range over 5 minutes divided by mean true range over 30 minutes |
| Session return | current close versus the first session candle open |
| Opening range | 15/30-minute range, current position and percentage breakout/breakdown |
| Coverage | fraction of the last 60 exact M1 closes present, plus degraded and carried-forward counts |

Features requiring an exact window are null when that window is incomplete.

### Side-neutral context features

The research context records symbol session return, benchmark return/momentum/
spread/freshness, breadth coverage and advancing ratio, sector breadth,
relative strength, market regime and prior-only relative-spread statistics.
Unlike the trading context it has no `alignment`, because alignment requires a
BUY or SELL side.

## `MICROSTRUCTURE_CONTRACT_V1`

The runtime name is `etoro_microstructure_v1`. Its primary windows are 10, 30
and 60 seconds. Updates are bounded in memory to 4,096 observations per symbol
with a 120-second retention horizon. Quote ingestion is O(1); feature
calculation scans the bounded buffer only at the five-minute state cadence.

For observations `i = 0..N-1`, define:

```text
mid_i        = (bid_i + ask_i) / 2
spread_bps_i = (ask_i - bid_i) / mid_i × 10,000
delta_i(x)   = x_i - x_(i-1)
U(x)         = count(delta_i(x) > 0)
D(x)         = count(delta_i(x) < 0)
C(x)         = U(x) + D(x)
tick_imbalance(x) = (U(x) - D(x)) / C(x), or 0 for a flat path
```

With fewer than two samples, movement features are null. Counts are still
reported.

| Feature suffix, for `micro_{10,30,60}s_` | Exact definition |
|---|---|
| `quote_count` | number of accepted snapshots in `[T-W,T)` |
| `quote_rate_hz` | `quote_count / W` |
| `sample_count` | first price state plus each subsequent change in `(bid, ask, actual broker last)` |
| `temporal_coverage_ratio` | `(last received_at - first received_at) / W`, clipped to `[0,1]` |
| `mid_return_percent` | `(mid_last / mid_first - 1) × 100` |
| `mid_absolute_path_percent` | `sum(abs(delta(mid))) / mid_first × 100` |
| `mid_tick_imbalance` | `tick_imbalance(mid)` |
| `mid_change_count` | `C(mid)` |
| `mid_directional_persistence` | `(mid_last-mid_first) / sum(abs(delta(mid)))`; 0 for a flat path |
| `mid_path_efficiency` | absolute value of directional persistence |
| `bid_tick_imbalance`, `ask_tick_imbalance` | `tick_imbalance(bid)` and `tick_imbalance(ask)` |
| `bid_change_count`, `ask_change_count` | `C(bid)` and `C(ask)` |
| `bid_vs_ask_update_imbalance` | `(C(bid)-C(ask)) / (C(bid)+C(ask))`; 0 when both are zero |
| `last_tick_imbalance` | tick imbalance over actual broker `LastExecution` observations only |
| `last_change_count` | change count over actual broker last observations |
| `last_update_activity_ratio` | broker-last changes divided by broker-last transitions |
| `spread_mean_bps` | arithmetic mean of `spread_bps` |
| `spread_change_bps` | last minus first `spread_bps` |
| `interarrival_median_ms` | median of consecutive non-negative receive-time gaps |
| `interarrival_burstiness` | `(P90-P10)/(P90+P10)` over receive-time gaps, using linear percentiles; requires at least two gaps |
| `last_position_in_spread` | `(LastExecution-bid)/(ask-bid)` for the latest actual broker last; null for zero spread or midpoint fallback |

The 60-second window additionally records spread minimum, maximum and range.
`last_position_in_spread` is intentionally not clipped: a last outside the
current spread remains observable.

“Broker last” here means the canonical state whose price source is
`broker_last`; it can be carried through eToro's merged state. Whether
`LastExecution` was actually retransmitted in a particular PATCH is answered
only by the payload-schema observer, not inferred from the price path.

These are quote-flow and price-path features. eToro's current L1 payload does
not establish traded BUY/SELL volume, bid/ask size, depth or an order book. The
contract therefore never uses names such as `buy_volume`, `sell_volume`,
`order_flow`, `bid_size`, `ask_size` or `liquidity_depth`.

## eToro payload-schema observer

`etoro_payload_schema_v1` observes field names and types without creating a raw
payload log. For each field it retains only:

- observed types;
- first/last seen time;
- PATCH presence count;
- reconstructed/merged-state presence count;
- observed asset classes;
- numeric minimum/maximum when finite;
- at most three bounded scalar examples of at most 80 characters.

PATCH and merged counts are separate. A reconstructed Bid on a patch containing
only LastExecution increments merged presence but not PATCH presence. This
prevents reconstruction from being mistaken for a server retransmission.

Field names are bounded to 128 characters with a stable hash suffix when
necessary. Sensitive-looking names containing tokens such as authorization, credential,
password, secret, token, API key or user key never retain examples. Objects,
arrays, nulls and non-finite numbers also retain no example. State is bounded to
256 fields. A new field, type or asset class triggers an immediate atomic
replace; ordinary count updates flush at most once per minute and at shutdown.

The files remain small aggregates. No raw payload is appended.

## Files, retention and compatibility

All analysis artifacts are inside `data/logs/`:

```text
data/logs/
  etoro_payload_schema.json                 # latest bounded aggregate
  runs/run_*/
    research.jsonl.gz                       # five-minute states
    etoro_payload_schema.json               # run aggregate
    trades.jsonl.gz                         # unchanged
    market.jsonl.gz                         # unchanged
    candles.jsonl.gz                        # unchanged
    errors.jsonl.gz                         # unchanged
    debug_decisions.jsonl.gz                # unchanged
    manifest.json
    summary*.json
```

Run rotation removes the complete run directory, including both new artifacts.
Zipping `data/logs/` therefore remains exhaustive.

Run-manifest schema V14 is additive. All V13 fields retain their meaning. V14
adds activation status, research/microstructure/schema-observer contracts,
feature lists and hashes, cadence, causal convention and paths. The legacy
business streams, `entry_decision` schema and existing event payloads are not
modified. Old weekly archives remain consumable by existing analysis code;
new analysis joins `research.jsonl.gz` separately.

## Volume budget

There is no research event per tick. Accepted ticks update only the bounded
in-memory accumulator. One record is persisted every five minutes per eligible
symbol, with compact JSON and gzip. The payload observer overwrites one bounded
JSON aggregate instead of appending payloads.

`tests/research/test_research_journals.py` writes a reproducible one-hour,
one-quote-per-second market slice through the production gzip journal and one
full research record every five minutes. It asserts that compressed research
size remains below 10% of compressed market size. The implementation run
generated 1,135,688 bytes for 3,600 market records and 19,742 bytes for 11
research records: 1.738330% (small timestamp-compression variation is expected
between runs).

## Historical reconstruction check

The archived 29 July 2026 run
`run_20260729T010827_207768Z` was inspected without model retraining:

- 886,713 `market_price_changed` records, 330,444,788 compressed bytes;
- 51,691 finalized M1 candles across 115 symbols, 32,873,509 compressed bytes;
- SAP.DE at 08:05 UTC: 65 historical M1 bars, all 38 candle-state features
  available, 100% 60-minute coverage;
- AAPL at 14:35 UTC: 65 historical M1 bars, all 38 candle-state features
  available, 100% 60-minute coverage.

Thus the M1/base state is reproducible from historical candle logs and the
prior-only quote path can be replayed from historical market logs. Exact
WebSocket activity, unchanged patches and unknown field presence are not
recoverable from old `market_price_changed` streams because those streams
intentionally retained only accepted price changes and no raw payload schema.
Consequently, activity counts and the schema observer are genuinely
prospective; they must not be backfilled as if exact historical values existed.

## Analysis example

```python
import json
import pandas as pd

research = pd.read_json(
    'data/logs/runs/RUN_ID/research.jsonl.gz',
    lines=True,
    compression='gzip',
)
states = pd.json_normalize(research['payload'])
states['state_at'] = pd.to_datetime(states['state_at'], utc=True)

with open(
    'data/logs/runs/RUN_ID/etoro_payload_schema.json',
    encoding='utf-8',
) as handle:
    payload_schema = json.load(handle)
```

Offline lifecycle outputs should join on `research_state_id`; they must never be
written back into the causal live state.

## Interpretation and limits

- **Certain bug:** none is asserted by the research hypotheses themselves.
- **Contract inconsistency fixed here:** a state cutoff recomputes remaining
  session time at `state_at`, preventing a sub-second stale session decision
  from admitting the exact last-hour boundary.
- **Technical debt:** the payload schema cannot reveal fields absent from the
  prospective observer and old logs cannot restore omitted PATCH presence.
- **Assumed strategic choice:** five-minute side-neutral sampling and exclusion
  of the final hour follow the current lifecycle/session target.
- **Statistical hypothesis:** sub-minute quote path may improve Capturability or
  Direction; the PR does not establish that it does.
- **Future idea, out of scope:** fit and validate the four offline heads on the
  new prospective states, then run shared-lifecycle BUY/SELL counterfactuals.

No external provider, L2/order-book abstraction, live ML inference, second
lifecycle engine, SQLite research store or hidden selector is introduced.
