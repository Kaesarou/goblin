# MANAGED V2 segment-first challenger

## Runtime status

MANAGED V2 is a frozen equity research challenger. Its runtime role is
`shadow`; its deployment status is `shadow_not_approved`. The active selector
remains `managed_edge_v1` for equities and crypto.

Shadow evaluation may calculate and journal a V2 decision, but it cannot:

- submit or block an order;
- replace the active V1 ranking;
- consume a top-N slot;
- backfill a rejected or unavailable V1 candidate;
- change sizing, capacity, cooldown or lifecycle behavior.

The journal records two different outcomes: component-floor eligibility
(`managed_v2_gate_outcome`) and the complete shadow selection after deterministic
ranking/top-N (`managed_v2_shadow_selection_outcome`). They are never treated
as synonyms.

This separation is intentional. The available replay evidence does not support
activating V2.

## Preserved execution invariants

The experiment changes candidate modelling and quote validation only. These
contracts remain unchanged:

| Contract | Frozen behavior |
|---|---|
| TP/SL | Existing named asset/side profiles |
| Breakeven | Crypto 0.20%, EU equity 0.55%, US equity 0.60% |
| Lifecycle | Shared breakeven, trailing, TP, initial stop, stale and force-close engine |
| Portfolio | Existing cooldowns, capacity, top-N and no-backfill rules |
| Risk | Existing `RiskManager` and position sizing |
| Prices | BUY enters at ask and exits at bid; SELL enters at bid and exits at ask |
| Costs | Explicit costs deducted once; spread is not deducted again from executable fills |

## First-class segments

Every runtime candidate has a `StrategySegment` derived from its asset class
and side. V2 supports exactly four trained domains:

| Segment | Deliberately retained feature structure |
|---|---|
| `EQUITY_EU_BUY` | Direct benchmark and relative-strength context plus M30/H1 structure |
| `EQUITY_EU_SELL` | Sparse movement, context and spread inputs; no borrowed MTF family |
| `EQUITY_US_BUY` | Plan geometry, context, spread, ATR and session progress; no arbitrary MTF inputs |
| `EQUITY_US_SELL` | Context and spread plus M15/M30 movement structure |

There is no generic equity fallback and no V2 crypto transfer. A missing or
mismatched segment, feature contract, model term, version, floor, provenance
field or artifact file fails closed.

## Three separate questions

V2 does not collapse candidate quality into one target.

### Opportunity

`Opportunity = 1` when side-aware executable MFE reaches the unchanged live
breakeven activation threshold within the lifecycle horizon. The label follows
the counterfactual path after an earlier initial stop so it answers whether the
opportunity existed at all.

### Path Quality

`Path Quality` exists only when `Opportunity = 1`. It is positive when the
protection threshold is reached strictly before the initial stop. This separates
a usable path from an opportunity that appears only after the live trade would
already have failed.

### Economics

Economics is fitted only where both Opportunity and Path Quality are positive.
Its label is the shared lifecycle's net return, using side-aware executable
prices and explicit costs once.

The three estimates have separate frozen models, versions and floors. Ranking
uses their joint evidence, but each floor remains an independent gate in V2
counterfactual selection.

## Feature lineage and no-lookahead rules

- `aligned_benchmark_momentum` comes directly from the configured benchmark in
  `CandidateMarketContext`. It is not read from `P_DIRECTION` metadata.
- Benchmark momentum and symbol relative strength are aligned to the candidate
  side at extraction time.
- M15/M30/H1 values are accepted only when their bar close is no later than the
  candidate candle close.
- `relative_spread_context_v1` compares the current spread with at most 512
  strictly prior accepted observations for that symbol. It becomes available
  after 20 observations.
- Offline extraction and replay keep quotes received during a delayed decision
  batch until every candidate in that batch is scored, then compact the
  history. This prevents later quotes from evicting the 512 strictly-prior
  observations. Exact duplicate snapshots from overlapping source runs are
  removed before either labels or spread history consume them.
- Relative-spread ratio, prior percentile and recent-reference change are model
  inputs only. They are not standalone hard gates.
- Optional input absence is represented by `None` and frozen missing indicators;
  it is never silently replaced by a feature from another model family.

The following available data was deliberately not retained:

- V1 `P_PROTECTION` is not an input to V2. Although it was informative for US
  BUY analysis, consuming it would make the new path contract depend on a V1
  model that is evaluated later in the active selector. Path Quality instead
  owns its inputs and label explicitly.
- US BUY receives no arbitrary M30/H1 family; EU SELL receives no MTF family.
- raw SELL detail dictionaries, opening-range fields and candle-quality fields
  remain journalled but are absent from the frozen artifact because they did
  not show sufficient independent out-of-sample value.
- benchmark, breadth and relative strength remain continuous context. No
  SPX500/FRA40 direction gate was introduced.

## Frozen fitting cohort

The packaged `managed_v2_20260808` artifact was fitted once with deterministic
regularization, random state `20260808` and a single numerical worker so BLAS
reductions remain byte-for-byte reproducible.

| Partition | Dates | Rows |
|---|---|---:|
| Training | 22–24 and 27–28 July 2026 | 2,605 |
| Untouched validation | 29–31 July and 3–7 August 2026 | 4,542 |

28 July is explicitly marked incomplete in provenance. Validation dates never
participate in fitting or floor selection. Opportunity and Path probabilities
use a 75% model / 25% training-prior blend. Training-only segment priors are
clipped to `[0.35, 0.60]` for Opportunity and `[0.50, 0.70]` for Path;
Economics uses a fixed `0.00%` net floor. Continuous training and automatic
retuning are disabled.

The canonical extracted dataset SHA-256 is
`509bf6e5305107a360b63af3bf35c372c0cd59ad9c3e7f68a6a7897f2de9111d`.
The frozen aggregate artifact SHA-256 is
`f991f1080afd1c8f820edae35c059a5350bdfef50cc199c66e33680ab109d5aa`.
The coefficients live in the four segment JSON artifacts; their file hashes
are:

| Artifact | SHA-256 |
|---|---|
| `EQUITY_EU_BUY.json` | `e73f00e180e0e38f4fa4808ff1b68ddc509219c742928584926cc4923f3223df` |
| `EQUITY_EU_SELL.json` | `f1d8c7f90ce3721adee393d7104081b58a2c7d05b154fa0b23994ce5de78f03a` |
| `EQUITY_US_BUY.json` | `2e83541dee0b0face4fe85e349fce58b27c625f5c5cbc75322586a3f09305211` |
| `EQUITY_US_SELL.json` | `9fbc64ab2534a02aabbad0c4af0dd040d0aa32481c489af0c5fb7b926da6701a` |
| `manifest.json` | `8693e73208c3e7261eb5c168e5853eb111fbd6ff50842ab4ce9e0d97981dfbd0` |

The floors were fixed from training data before the validation replay:

| Segment | Training rows | Opportunity floor | Path floor | Economics floor |
|---|---:|---:|---:|---:|
| `EQUITY_EU_BUY` | 575 | 0.441739 | 0.70 | 0.00% |
| `EQUITY_EU_SELL` | 636 | 0.350000 | 0.70 | 0.00% |
| `EQUITY_US_BUY` | 747 | 0.368139 | 0.70 | 0.00% |
| `EQUITY_US_SELL` | 647 | 0.412674 | 0.70 | 0.00% |

## Validation evidence

Opportunity discrimination outside training is uneven:

| Segment | Opp. AUC | Opp. Brier | Path AUC | Path Brier | Economics expected / realized | Economics corr. |
|---|---:|---:|---:|---:|---:|---:|
| `EQUITY_EU_BUY` | 0.474 | 0.288 | 0.732 | 0.041 | 0.235% / 0.203% | -0.069 |
| `EQUITY_EU_SELL` | 0.594 | 0.229 | 0.692 | 0.061 | 0.264% / 0.252% | -0.026 |
| `EQUITY_US_BUY` | 0.682 | 0.215 | 0.722 | 0.078 | 0.332% / 0.274% | 0.051 |
| `EQUITY_US_SELL` | 0.665 | 0.218 | 0.666 | 0.086 | 0.311% / 0.218% | 0.067 |

Path Quality contains useful ordering information, especially for BUY segments.
Opportunity is not useful for EU BUY on this cohort, and the conditional
Economics component has almost no linear association with realized returns.
Calibration bins are retained rather than collapsed into an aggregate claim:
Opportunity is materially overconfident in the upper EU BUY and US SELL bins,
while Path labels are both highly imbalanced and concentrated in the upper
probability bins. This is another reason not to promote the challenger.
These results require longitudinal shadow evidence; they do not justify live
activation. The complete frozen metrics and source provenance are retained in
the [fit report](replays/managed-v2-fit-report-v1.json).

## Empirical replay verdict

The final replay covers 13 journaled days from 22 July through 7 August 2026.
It uses the fixed candidate universe found in the journals; it does not claim
to reconstruct strategy candles or candidates that the original runs never
emitted. The 28 July source day is incomplete: realized P&L remains included,
while mark-to-market P&L and end-of-day open positions are reported separately
instead of being silently forced closed.

| Arm | Mode | Trades | Gross | Explicit costs | Cost/trade | Net | Realized drawdown |
|---|---|---:|---:|---:|---:|---:|---:|
| V1 live validation | Recorded fills first | 92 | 134.0249 | 201.7651 | 2.1931 | -67.7402 | 97.4218 |
| V1 historical stream | Counterfactual | 92 | 109.5046 | 201.7651 | 2.1931 | -92.2605 | 121.9421 |
| V1 + `quote_quality_v2` | Counterfactual | 92 | 115.5744 | 201.7651 | 2.1931 | -86.1907 | 115.8723 |
| V2 + `quote_quality_v2` | Counterfactual | 463 | -109.3431 | 1,014.9483 | 2.1921 | -1,124.2914 | 1,131.8250 |

The primary matched comparison is the last two arms. V2 changes the selection
materially: 20 trades overlap, 443 are added and 72 are removed. It executes
371 more trades and loses an additional 1,038.1007 net. Every V2 segment is
negative:

| Segment | V2 trades | V2 net |
|---|---:|---:|
| `EQUITY_EU_BUY` | 149 | -370.6959 |
| `EQUITY_EU_SELL` | 70 | -125.8117 |
| `EQUITY_US_BUY` | 119 | -299.8798 |
| `EQUITY_US_SELL` | 125 | -327.9040 |

V1 with quote quality records 20 initial stops, 31 protected breakevens, 17
protected trailing exits, 13 stale exits, 10 take-profits and one session
force-close. V2 records respectively 151, 127, 48, 84, 41 and 12. Average
executable MFE/MAE is 0.6874%/0.4751% for the matched V1 arm and
0.5304%/0.5287% for V2. Capacity blocks rise from 15 to 1,139 and cooldown
blocks from 47 to 360, exposing the challenger's much broader candidate flow
without changing either portfolio constraint.

Quote quality is isolated from policy selection. It quarantines 254 quote
observations while leaving all 92 V1 trade identifiers unchanged. The only V1
trade outcome changed is GOOGL on 4 August: an anomalous initial-stop result of
-9.3852 becomes a stale exit of -3.3154, improving net by 6.0698.
The live-validation arm has explicit broker fills for that trade and records
-2.2942 net. This corroborates that the raw quote was anomalous without
claiming that the quality-filter counterfactual reproduces the broker fill.

Historical fill coverage is limited: 29 of 165 recorded openings have an
explicit broker entry fill and 26 have an explicit broker exit fill. The live
validation arm names the remaining contractual fallbacks and never presents
them as inferred broker fills.

This evidence rejects V2 for activation. V2 remains `shadow_not_approved` and
cannot affect orders. It does not validate V1 either: all four replay arms are
net negative on this cohort. The full trade-level evidence is retained in the
[JSON replay report](replays/managed-v2-replay-comparison-v1.json) and the
[human-readable replay summary](replays/managed-v2-replay-comparison-v1.md).

## Replay price modes

`replay_pricing_v2` exposes two explicit modes:

- `live_validation` prioritizes canonical historical broker entry and exit
  fills. An explicit entry becomes lifecycle-active at its recorded time and an
  explicit exit closes at its recorded fill time. Missing fills use a named
  contractual fallback; ambiguous legacy price fields are never promoted
  silently.
- `counterfactual` always uses side-aware executable estimates because the
  hypothetical trade has no broker fill.

Every replay trade records mode, selection policy, entry/exit provenance and
pricing-contract version. The validation report uses four arms:

1. V1 live validation: explicit historical broker fills first, with a named
   fallback where no typed fill exists;
2. V1 counterfactual on the historical accepted stream;
3. V1 counterfactual with `quote_quality_v2` reapplied;
4. V2 counterfactual with `quote_quality_v2` reapplied.

The primary policy comparison is arms 3 and 4. Holding quote quality constant
prevents the GOOGL anomaly correction from being misattributed to V2 selection.
The full trade-level result is retained in the
[JSON replay report](replays/managed-v2-replay-comparison-v1.json), with a
[human-readable summary](replays/managed-v2-replay-comparison-v1.md).

## Anomalous quote protection

`quote_quality_v2` learns a rolling distribution of accepted absolute changes
per symbol. Each observation uses the largest absolute percentage change across
bid, ask and last, matching the executable prices used by the lifecycle. After
20 accepted changes, a quote is suspect when its jump exceeds the larger of
`0.75%` and 20 times the rolling median. Existing instrument limits still apply.

A single suspect quote is quarantined before it can update candles, create
candidates or drive TP, initial stop, breakeven or trailing logic. The next
quote can either confirm the new level within the instrument's configured
tolerance or establish that the suspect quote was isolated. An intermediate
follow-up that confirms neither remains quarantined and becomes the pending
level for the next observation. Ordinary quotes remain single-observation and
immediate; there is no universal double-confirmation rule.

Position-only REST fallback has an independent quality-validator state warmed
from accepted WebSocket quotes. It receives the same quarantine behavior but
cannot advance or contaminate the canonical WebSocket validator state.

The logs retain `market_data_quarantined` and
`market_data_quality_resolved` events with threshold, reference distribution,
pending timestamp and resolution reason.

## Frozen two-week protocol

The collection starts on the first Monday after the manual `develop -> main`
deployment, at the first full equity session in `Europe/Paris`. It ends after
the following week's Friday session: two complete Monday–Friday observation
windows. Exchange holidays or source outages remain explicit missing sessions;
they are not hidden by extending or retuning the window.

During that window:

1. keep V1 as the only execution selector;
2. keep the artifact, features, labels, floors and quote-quality constants
   unchanged;
3. retain every canonical `entry_decision`, V2 shadow outcome, missing-feature
   list, quote-quality resolution and lifecycle result;
4. do not tune against partial results;
5. record the deployed main SHA and artifact SHA in every run manifest;
6. monitor only data and operational health; do not use partial P&L, AUC or
   calibration results to alter the strategy;
7. after Friday of week two, seal and hash the corpus before any analysis;
8. replay the untouched cohort once and report
   results by segment, day, close reason, costs, drawdown and selection overlap;
9. decide explicitly whether to reject, redesign or run another frozen shadow
   cohort. No automatic promotion exists.

## Reproduction

```bash
python -m pip install -e ".[calibration]"

python scripts/fit_managed_v2.py PLAN.json \
  app/execution/scoring/models/managed_v2 fit-report.json \
  --dataset-json managed-v2-dataset.json

python scripts/replay_managed_v2.py PLAN.json replay-report.json \
  --output-markdown replay-report.md
```

Run-manifest schema v13, analysis-ready summary schema v14 and entry-decision
schema v2 retain the versions, artifact hash, segment, component estimates,
floors, shadow decision, relative-spread state and deployment status required
to reproduce the result.
