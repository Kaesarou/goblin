# Market context scoring and entry routing

This document describes the canonical PR5-B relationship between validated market context, candidate score and structural entry routing.

## Separation of responsibilities

Goblin separates three questions:

1. **How strong is the setup?** — candidate score.
2. **Is the current entry timing structurally acceptable?** — `EntryDecisionEngine`.
3. **Can the account execute it safely?** — `RiskManager`.

Market context answers the first question only. It can increase or reduce conviction, but it cannot reject a candidate, invalidate a pending setup or force a retest by itself.

## Data-quality gate

Only accepted snapshots may:

- update strategy and benchmark state;
- construct candles;
- update context;
- produce candidates;
- drive local position lifecycle handling.

Live validation compares snapshot timestamps with actual receipt/validation time. Historical replay keeps its explicitly supplied clock.

## Trading and context universes

`WATCHLIST` is the trading universe. Only its symbols may create candidates.

Default context-only references are:

| Asset class | Benchmark |
|---|---|
| Crypto | `Crypto10` |
| US equities | `SPX500` |
| European equities | `FRA40` |

Benchmarks are fetched and validated but never receive a strategy instance, consume a ranking slot or reach execution.

## Candidate market context

Every candidate may carry an immutable `CandidateMarketContext` containing:

- asset class;
- benchmark session return;
- benchmark rolling momentum;
- same-market breadth and coverage;
- sector breadth where available;
- symbol session return;
- symbol relative strength versus benchmark;
- current spread relative to the symbol's prior accepted-spread distribution;
- descriptive regime and alignment.

The labels `risk_on`, `risk_off`, `mixed`, `aligned`, `neutral` and `opposed` remain diagnostic. No business branch is allowed to use `opposed` as a veto.

## Continuous context score

For BUY, all inputs are interpreted in the positive-price direction. For SELL, their direction is inverted.

```text
directional value = side direction × raw value
```

The context contribution combines:

```text
benchmark session contribution
+ benchmark momentum contribution
+ breadth contribution
+ sector contribution
+ directional relative-strength contribution
```

Directional relative strength is deliberately dominant. A symbol moving strongly in the trade direction can therefore compensate an adverse index and breadth.

### Initial PR5-B weights

| Component | US | Europe | Crypto |
|---|---:|---:|---:|
| Benchmark session | ±3 | ±2 | ±4 |
| Benchmark momentum | ±2 | ±1 | ±2 |
| Breadth | ±3 | ±3 | ±2 |
| Sector | ±2 | ±2 | 0 |
| Relative strength | ±12 | ±14 | ±12 |
| Total bound | ±20 | ±20 | ±20 |

Continuous components use bounded smooth scaling rather than binary thresholds.

Example:

```text
SPX500                 -1.0%
BUY symbol             +2.5%
relative strength      +3.5%

benchmark/breadth       negative contribution
relative strength       larger positive contribution
final context score     can remain positive
```

The same symbol with only marginal outperformance would receive a much smaller compensation.

## Entry actions

`EntryDecisionEngine` produces:

### `READY_FOR_SELECTION`

Timing is acceptable. The candidate must still pass its asset-specific minimum score, ranking, economics and risk checks.

### `WAIT_FOR_RETEST`

Price is sufficiently extended from the observable breakout/breakdown level and the structure remains suitable for a real retest.

This action depends only on structure and timing. Context does not create it.

### `SKIP`

A real hard constraint applies, primarily:

- expected net profit below the configured post-cost minimum;
- costs greater than or equal to gross TP distance;
- invalid structural stop or other explicit economic constraint.

Probabilistic context is never a `SKIP` reason.

## Pending retest lifecycle

A pending entry is created only from `WAIT_FOR_RETEST`.

Confirmation requires:

1. a real return to the breakout/breakdown area;
2. no structural invalidation;
3. continuation in the trade direction;
4. acceptable short-term momentum;
5. an executable spread at confirmation time.

A temporary spread breach emits `pending_entry_confirmation_blocked` and preserves the setup while its age continues to advance.

A confirmed candidate carries:

```text
entry_origin = pending_confirmation
structural_confirmation_satisfied = true
```

The router then returns `READY_FOR_SELECTION` and cannot request the same retest again.

The legacy `market_context_opposed` pending invalidation does not exist.

## Counterfactual record

Every evaluated candidate receives exactly one standalone `entry_decision` record, whether selected or rejected. It contains:

- candidate, origin and pending identifiers;
- timestamp, symbol, side and entry reference price;
- base and directional score;
- context score and components;
- MTF score and components;
- TP-feasibility score and contribution;
- economics and effective SL/TP;
- raw and calibrated TP probability;
- break-even probability, EV and probability edge;
- route action/reason;
- selection outcome/reason;
- all model versions.

This is the canonical source for post-run TP-before-SL, MFE, MAE and net-outcome analysis.

## Versioned contracts

The current runtime records:

- `market_context_v3`;
- `relative_spread_context_v1`;
- `market_context_score_v1`;
- `multi_timeframe_features_v2`;
- `multi_timeframe_score_v1`;
- `tp_feasibility_score_v2`;
- `heuristic_v3`;
- `entry_router_v5`;
- entry-decision schema v2 and run-manifest schema v13.

Relative-spread references exclude the current quote and require 20 prior
observations. Missing history is explicit; relative spread is a model feature,
never a standalone hard gate.
