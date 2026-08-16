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

The latest-quote provenance buffer receives accepted `MarketDataEvent.snapshot`
values from every current eToro market-data source. The microstructure
accumulator receives only accepted **WebSocket** snapshots. Rejected and
quarantined quotes enter neither. This keeps REST fallback observable in the
base state without presenting its slower cadence as sub-minute WebSocket
microstructure.

Payload-schema observation is an optional callback injected into the eToro
parser. Its neutral transport arguments are the symbol, current PATCH, merged
state and receive time. PATCH and merged state are exposed as read-only
top-level views. `MarketDataEvent` contains no research DTO, and neither
`app.market_data` nor `app.brokers` imports `app.research`. The observer is a
consumer of transport shape; it does not define a valid economic price
observation. Observer exceptions and mutation attempts are isolated from
Bid/Ask/Last parsing.

After the existing one-second candle grace and all due business-candle
processing, a runtime-clock scheduler checks the latest exact five-minute
boundary. For every active-session EU or US equity in the watchlist it attempts
one flat state, even when that symbol has no quote or no boundary candle. Such a
state remains useful as an explicit incomplete observation rather than silently
removing a poor-data market state from the research population. A boundary at
or before runtime startup is never backfilled.

The sidecar has no return path into:

- candidate generation or candidate side;
- `MANAGED_EDGE_V1`, ranking or top-N;
- `EntryDecision` or `RiskManager`;
- cooldowns, capacity, positions or session trade counts;
- broker tasks or orders;
- TP, SL, breakeven, trailing, stale exit or force close;
- live P&L.

Research is invoked after the existing accepted-market and finalized-candle
business writes. At each due boundary the scheduler first calculates each
symbol independently, then persists all successful states with one
`JsonlJournal.write_many()` gzip open. A calculation failure for one symbol
does not suppress the others. A global batch-write failure is counted and
reported without interrupting trading. Legacy users of `write()` retain the
same envelope and one-record behavior.

`RESEARCH_ENABLED=false` disables the sidecar. In that mode the parser receives
no observer callback, so it does not build field-path samples, no research
journal is constructed, and no research state or schema aggregate is emitted.
Only the compact disabled `research_summary.json` is written at startup and
clean shutdown. The setting defaults to `true` so the prospective protocol is
active after deployment.

## Sampling and session contract

`SIDE_NEUTRAL_MARKET_RESEARCH_STATE_V1` applies these rules:

- population: every `EQUITY_EU` and `EQUITY_US` symbol in the configured
  watchlist;
- cadence: one opportunity every five minutes per symbol, at exact UTC minute
  boundaries divisible by five;
- candidate independence: a state does not require a Goblin candidate;
- side neutrality: no BUY/SELL alignment, candidate side or preferred side is
  an input;
- availability independence: a missing quote or candle does not suppress the
  state; availability fields and null features expose the gap;
- prospective start: only `state_at > runtime_started_at` is eligible, so a
  restart cannot manufacture observations for boundaries it did not observe;
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
  and percentage spread, `quote_available`, market-data source, broker-last
  provenance and freshness;
- latest candle availability and quality: timestamp, sample count,
  carried-forward/degraded flags and source-price age;
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

## `MICROSTRUCTURE_CONTRACT_V2`

The runtime name is `etoro_microstructure_v2`. Its input is accepted eToro
WebSocket snapshots only. Its primary windows are 10, 30 and 60 seconds.
Updates are bounded in memory to 4,096 observations per symbol with a
120-second retention horizon. Quote ingestion is O(1); feature calculation
scans the bounded buffer only at the five-minute state cadence.

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
| `sample_count` | first price state plus each subsequent change in `(bid, ask, canonical broker last)` |
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
| `last_tick_imbalance` | tick imbalance over the canonical broker-last value path |
| `last_change_count` | change count over the canonical broker-last value path |
| `last_value_change_ratio` | canonical broker-last value changes divided by canonical broker-last transitions; this is not a retransmission ratio |
| `spread_mean_bps` | arithmetic mean of `spread_bps` |
| `spread_change_bps` | last minus first `spread_bps` |
| `interarrival_median_ms` | median of consecutive non-negative receive-time gaps |
| `interarrival_burstiness` | `(P90-P10)/(P90+P10)` over receive-time gaps, using linear percentiles; requires at least two gaps |
| `last_position_in_spread` | `(canonical broker last-bid)/(ask-bid)` for the latest broker-last-valued state; null for zero spread or midpoint fallback |

The 60-second window additionally records spread minimum, maximum and range.
`last_position_in_spread` is intentionally not clipped: a last outside the
current spread remains observable.

“Broker last” here means the canonical state whose price source is
`broker_last`; it can be carried through eToro's merged state. Consequently,
`last_tick_imbalance` and `last_value_change_ratio` describe changes in that
canonical value path. They do not count actual transport retransmissions.
Whether `LastExecution` was present in a particular PATCH is answered only by
the payload-schema observer's PATCH-presence count.

These are quote-flow and price-path features. eToro's current L1 payload does
not establish traded BUY/SELL volume, bid/ask size, depth or an order book. The
contract therefore never uses names such as `buy_volume`, `sell_volume`,
`order_flow`, `bid_size`, `ask_size` or `liquidity_depth`.

## eToro payload-schema observer

`etoro_payload_schema_v2` observes deterministic field paths and types without
creating a raw payload log. It walks objects to a maximum depth of two, so an
object such as `OrderBook` can expose `OrderBook.BidSize` and
`OrderBook.AskSize`. Arrays are recorded as arrays with their bounded container
size but their elements are never traversed. For each retained path it stores
only:

- observed types;
- first/last seen time;
- PATCH presence count;
- reconstructed/merged-state presence count;
- observed asset classes;
- numeric minimum/maximum when finite;
- container-size minimum/maximum for objects and arrays;
- at most three bounded scalar examples of at most 80 characters.

PATCH and merged counts are separate. A reconstructed Bid on a patch containing
only LastExecution increments merged presence but not PATCH presence. This
prevents reconstruction from being mistaken for a server retransmission.

Paths are sorted independently of dictionary insertion order and bounded to
128 characters with a stable hash suffix when necessary. Sensitive-looking
paths containing tokens such as authorization, credential, password, secret,
token, API key or user key never retain examples. Objects, arrays, nulls and
non-finite numbers also retain no example. State is globally bounded to 256
paths; truncation is recorded. A new path, type or asset class triggers an
immediate atomic replace; ordinary count updates flush at most once per minute
and at shutdown. After an I/O failure, ordinary updates also respect this
one-minute attempt interval instead of retrying disk I/O on every quote; a
genuinely new path or type still requests an immediate attempt.

The files remain small aggregates. No raw payload is appended.

## Files, retention and compatibility

All analysis artifacts are inside `data/logs/`:

```text
data/logs/
  etoro_payload_schema.json                 # latest bounded aggregate
  runs/run_*/
    research.jsonl.gz                       # five-minute states
    research_summary.json                   # bounded run health/completeness
    etoro_payload_schema.json               # run aggregate
    trades.jsonl.gz                         # unchanged
    market.jsonl.gz                         # unchanged
    candles.jsonl.gz                        # unchanged
    errors.jsonl.gz                         # unchanged
    debug_decisions.jsonl.gz                # unchanged
    manifest.json
    summary*.json
```

Run rotation removes the complete run directory, including all research
artifacts.
Zipping `data/logs/` therefore remains exhaustive.

Run-manifest schema V14 is additive. All V13 fields retain their meaning. V14
adds activation status, research/microstructure/schema-observer contracts,
feature lists and hashes, reconstructibility metadata, health-summary schema,
cadence, causal convention and paths. The legacy business streams,
`entry_decision` schema and existing event payloads are not modified. Old
weekly archives remain consumable by existing analysis code; new analysis
joins `research.jsonl.gz` separately.

`MARKET_DATA_MODEL_VERSION` remains `market_data_v2_ws_clocked_2`: removing the
research-only field restores `MarketDataEvent` to the core contract that
predated this research branch, so there is no net externally persisted
Market Data contract change to version. The changed research contracts are
versioned independently (`etoro_microstructure_v2`,
`etoro_payload_schema_v2`, `research_summary_v1` and
`research_reconstructibility_v1`).

### Research health and completeness

`research_summary.json` is run-scoped, compact, versioned as
`research_summary_v1`, bounded and atomically replaced. It is written at
startup, after each observed research boundary and on clean shutdown—not on
each quote. Its manifest role is
`run_scoped_research_completeness_and_health`.

`expected_state_count` is incremented at each unique five-minute boundary
actually observed after run start, once for every configured research symbol
whose session is research-tradable at that boundary. A state remains expected
when quote, candle, context or microstructure input is missing. No boundary
before a restart is inferred. `emitted_state_count` is incremented only after
the corresponding `research_state` JSONL record is successfully persisted.
Their difference therefore measures a collection gap rather than missing
market input.

The summary records enabled status; expected/emitted, not-tradable, duplicate,
state-calculation, journal-write and payload-schema counts; missing quote and
candle counts; 10/30/60-second microstructure availability counts; first/last
state times; boundary count; last/maximum calculation, persistence and total
durations for state calculation plus JSONL persistence; cumulative processing
time; research-journal gzip open count; and summary-write failures. The
summary's own atomic-replace time is excluded from its self-reported duration;
the external wall-clock benchmark below includes it.

## Volume budget

There is no research event per tick. Accepted ticks update only the bounded
in-memory accumulator. One record is persisted every five minutes per eligible
symbol, with compact JSON and gzip. The payload observer overwrites one bounded
JSON aggregate instead of appending payloads.

`tests/research/test_research_journals.py` writes a reproducible one-hour market
slice through the production gzip journal, with one price-changing quote every
four seconds and one full research record every five minutes. Four seconds is
slightly sparser, and therefore more demanding for this ratio, than the 29 July
archive (886,713 market records over 51,691 symbol-minutes). The validation run
generated 283,512 market bytes, 19,915 research-stream bytes, 1,349 health-
summary bytes and 3,972 payload-schema bytes (1,986 each for the run aggregate
and the latest aggregate). All new research artifacts total 25,236 bytes, or
**8.901211%** of the market stream (small timestamp-compression variation is
expected between runs).

An archive-shaped upper-bound measurement also wrote one full synthetic state
for every five-minute candle in the 29 July run, including states that the live
last-hour rule would actually exclude: 10,283 states produced 18,675,543 bytes
against 330,444,788 bytes of `market.jsonl.gz`, or **5.651638%**. The deployed
ratio should therefore be lower than this deliberately over-inclusive bound.

## Boundary performance check

`tests/research/test_research_boundary_performance.py` runs five consecutive
boundaries for a production-shaped universe of 110 symbols against real gzip
files. On the validation host, complete wall-clock boundary duration, including
the atomic health-summary refresh, had a median of **21.652 ms** and a maximum
of **24.955 ms**. Instrumented calculation median/maximum were 5.066/8.879 ms
and research-stream persistence median/maximum were 15.278/16.685 ms. The
reproducible guard uses a deliberately generous 5,000 ms catastrophic-
regression ceiling rather than a hardware-sensitive microbenchmark threshold.
The same test feeds one WebSocket snapshot for every symbol and verifies zero
research-journal opens before the first boundary.

The isolated persistence comparison wrote the same 110 records over five
runs. Legacy per-record writes opened gzip **550** times with a 9.452 ms
median; the batch path opened it **5** times with a 6.135 ms median (maxima
10.760 ms and 6.824 ms respectively). Thus production uses one research-journal
open per boundary instead of one per emitted symbol. These measurements are a
performance-risk check, not a claim that the previous burst caused a measured
trading defect.

## Historical reconstruction check

The archived 29 July 2026 run
`run_20260729T010827_207768Z` was inspected without model retraining:

- 886,713 `market_price_changed` records, 330,444,788 compressed bytes;
- 51,691 finalized M1 candles across 115 symbols, 32,873,509 compressed bytes;
- SAP.DE at 08:05 UTC: 65 historical M1 bars, all 38 candle-state features
  available, 100% 60-minute coverage; the retained price-change stream also
  yields 30 causal records in the prior 60 seconds and all 72 microstructure
  fields are defined;
- AAPL at 14:35 UTC: 65 historical M1 bars, all 38 candle-state features
  available, 100% 60-minute coverage, but the boundary candle is explicitly
  degraded/carried-forward with a 675.722118-second source age. There are no
  retained AAPL price changes in the prior 120 seconds, so only the 12
  zero/availability microstructure fields are defined and movement fields are
  null.

Thus the M1/base state is reproducible from historical candle logs and the
prior-only quote path can be replayed from historical market logs. Exact
WebSocket activity, unchanged patches and unknown field presence are not
recoverable from old `market_price_changed` streams because those streams
intentionally retained only accepted price changes and no raw payload schema.
Consequently, activity counts and the schema observer are genuinely
prospective; they must not be backfilled as if exact historical values existed.

The machine-readable manifest contract
`research_reconstructibility_v1` classifies every name in the research feature
set and gives the required historical materialization policy:

| Class | Families | Historical policy |
|---|---|---|
| `EXACT_HISTORICAL` | all M1/candle and side-neutral context features, when their retained inputs exist | materialize the value; otherwise null |
| `HISTORICAL_PRICE_CHANGE_PROXY` | microstructure price-path features reconstructed from accepted price-changing market events | use an explicitly proxy-named value or null; never present it as the prospective exact feature |
| `PROSPECTIVE_ONLY` | `quote_count`, `quote_rate_hz`, temporal coverage, canonical last-value change ratio, inter-arrival median/burstiness, unchanged PATCH activity and payload-schema presence | null/unavailable in historical weeks |

This prevents an offline analysis from comparing historical price-change
proxies with prospective WebSocket activity as though the populations and
measurement processes were identical.

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

with open(
    'data/logs/runs/RUN_ID/research_summary.json',
    encoding='utf-8',
) as handle:
    research_health = json.load(handle)
```

Offline lifecycle outputs should join on `research_state_id`; they must never be
written back into the causal live state.

## Interpretation and limits

- **Certain implementation bug fixed before publication:** a failed schema
  write could otherwise be retried by every subsequent quote; failed ordinary
  attempts are now rate-limited.
- **Contract inconsistencies fixed here:** Market Data and broker parsing no
  longer own a research DTO; historical/prospective comparability is
  machine-readable; expected versus emitted completeness is explicit; and the
  canonical broker-last change ratio is no longer named as a transport update
  ratio. Earlier fixes also recompute finite-session eligibility at `state_at`,
  keep cadence independent of quote/candle presence, and require the actual
  session-open M1 candle for session return.
- **Technical debt:** the payload schema cannot reveal fields absent from the
  prospective observer and old logs cannot restore omitted PATCH presence.
- **Measured performance risk, not a proven trading bug:** the old per-symbol
  persistence shape created up to one gzip open per symbol at a shared
  boundary. Batch persistence reduces this to one open; the production-shaped
  benchmark and generous regression guard quantify the remaining synchronous
  work.
- **Robustness improvements:** nested paths are discovered within strict
  bounds, per-symbol calculation failures are isolated, and the atomic health
  summary exposes incomplete collection.
- **Assumed strategic choice:** five-minute side-neutral sampling and exclusion
  of the final hour follow the current lifecycle/session target.
- **Statistical hypothesis:** sub-minute quote path may improve Capturability or
  Direction; the PR does not establish that it does.
- **Future idea, out of scope:** fit and validate the four offline heads on the
  new prospective states, then run shared-lifecycle BUY/SELL counterfactuals.

No external provider, L2/order-book abstraction, live ML inference, second
lifecycle engine, SQLite research store or hidden selector is introduced.
