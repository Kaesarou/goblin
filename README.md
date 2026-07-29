# Goblin!

> A deterministic intraday trading bot that lives in a cave, watches markets all day, and refuses to confuse activity with opportunity.

**Goblin!** is an experimental and auditable trading engine written in Python. It validates broker data, builds deterministic market structure, detects directional setups, estimates trading costs, models the race between take profit and stop loss, then applies explicit risk controls before an order can reach the broker.

> [!WARNING]
> Goblin is research software, not financial advice. Use `paper` or `etoro_demo` while the strategy is being calibrated. Real-money trading can lose capital.

## Core principles

- **Deterministic execution** — the same accepted data and versioned configuration must produce the same decision.
- **Demo first** — an edge is never assumed from a handful of trades.
- **Validated data only** — rejected or quarantined snapshots cannot create candles or entries.
- **Activity and direction remain separate** — `P_TOUCH` estimates whether a barrier will be reached; segmented `P_DIRECTION` estimates which barrier wins.
- **Costs are part of the trade** — the conditional break-even probability uses net TP gain and net SL loss.
- **One active policy** — no shadow selector, legacy score, hidden fallback or compatibility shim participates in runtime decisions.
- **Every decision leaves evidence** — raw data, bars, candidates, model inputs, routes, summaries and manifests are retained.
- **Doing nothing is valid** — zero trades can still be the correct outcome.

## Current capabilities

Goblin currently includes:

- one active, code-versioned `BalancedStrategyConfig`;
- local paper, eToro demo and eToro live broker adapters;
- crypto, US-equity and European-equity support;
- canonical M1 candles and deterministic M5/M15/M30/H1 bars;
- benchmark, breadth, sector and relative-strength context;
- deterministic BUY and SELL trend/breakout candidates;
- named fixed TP/SL profiles, structural pending stops and TP feasibility;
- a frozen two-stage outcome model;
- four trained equity direction segments and two explicit provisional crypto segments;
- a five-point conditional direction-edge gate applied before top-N;
- net breakeven, trailing stop, stale exit, cooldown and session controls;
- SQLite position/cooldown persistence and JSONL audit journals;
- a broad pytest suite validated by GitHub Actions.

## Decision pipeline

```mermaid
flowchart TD
    A[Broker snapshots] --> B[MarketDataValidator]
    B -->|accepted| C[Canonical M1]
    B --> D[Market context]
    C --> E[TrendStrategy]
    C --> F[MTF aggregation]
    E --> G[TradeCandidate BUY or SELL]
    D --> G
    F --> G
    G --> H[Named TP/SL and costs]
    H --> I[TP feasibility and entry route]
    I -->|READY| J[P_TOUCH frozen activity model]
    I -->|WAIT| K[PendingEntryManager]
    I -->|SKIP| L[Counterfactual journal]
    J --> M[Segmented P_DIRECTION]
    M --> N[50/50 shrinkage to segment prior]
    N --> O[direction_edge >= 5 points]
    O --> P[Rank eligible then top-N]
    K --> G
    P --> Q[RiskManager]
    Q --> R[TradeExecutor]
    R --> S[Managed position lifecycle]
    S --> L
```

## Two-stage outcome probabilities

For a candidate whose side, TP, SL and horizon already exist:

```text
P_TOUCH     = P(TP_FIRST or SL_FIRST)
P_DIRECTION = P(TP_FIRST | one barrier is touched)

P_TP      = P_TOUCH × P_DIRECTION
P_SL      = P_TOUCH × (1 - P_DIRECTION)
P_NEITHER = 1 - P_TOUCH

probability_score = round(200 × P_TP, 4)
```

`P_DIRECTION` is not the probability that the next candle rises. It is the conditional probability that the candidate's TP beats its SL among decisive paths.

### Frozen `P_TOUCH`

The activity component is unchanged from the previous probability model. It was fitted on 1,958 usable US/EU candidates from 22–24 July 2026 and is reproduced byte-for-byte by the V2 fitting script. This isolates the direction experiment from changes in activity calibration.

### Segmented `P_DIRECTION`

The direction component uses an exact segment selected from market and side:

| Segment | Status | Feature family |
|---|---|---|
| `EQUITY_EU_BUY` | trained | core movement/context |
| `EQUITY_EU_SELL` | trained | multi-timeframe |
| `EQUITY_US_BUY` | trained | plan geometry/feasibility |
| `EQUITY_US_SELL` | trained | multi-timeframe |
| `CRYPTO_BUY` | provisional transfer | US BUY geometry |
| `CRYPTO_SELL` | provisional transfer | US SELL multi-timeframe |

There is no generic runtime fallback. Crypto models are explicit artifact entries with `training_status=provisional_transfer`, zero crypto training rows and a named US source segment. They can be replaced later without changing the runtime contract.

Signed market variables are aligned to the candidate side: a negative return favorable to a SELL becomes a positive aligned feature.

### Conservative calibration

Each raw direction prediction is shrunk toward its segment's observed decisive TP rate:

```text
P_DIRECTION final
= 0.50 × raw model probability
+ 0.50 × segment prior
```

The journal retains the raw probability, prior, final probability, segment, feature family, training status and source segment.

## Economic gate and selection

The conditional probability needed to break even is:

```text
direction_break_even
= net loss at SL / (net gain at TP + net loss at SL)

direction_edge
= P_DIRECTION - direction_break_even
```

The active policy requires:

```text
direction_edge >= 0.05
```

This means five **percentage points**, not a relative five-percent increase.

Selection order:

1. apply entry route, readiness, feasibility and hard economics;
2. reject candidates below the five-point direction margin;
3. rank the eligible candidates by `direction_edge`, then `P_TP`, then deterministic candidate ID;
4. keep the asset-specific top-N: two crypto, one US equity, one EU equity;
5. apply RiskManager and execute.

The former minimum-`P_TP` and maximum-`P_TOUCH` gates do not exist. A rejected top candidate cannot consume a slot and block the next eligible candidate.

## Evidence behind V2

The direction dataset contains all 3,527 labelled candidates from 22, 23, 24, 27 and 28 July 2026, including selected and rejected candidates. It contains 1,547 decisive paths: 475 `TP_FIRST` and 1,072 `SL_FIRST`.

Cross-day validation of the segmented direction architecture produced:

```text
P_DIRECTION AUC:   0.643
P_DIRECTION Brier: 0.202
```

A stricter train-22–24/test-27–28 check produced approximately AUC 0.588 and Brier 0.217. These figures justify a demo experiment, not a profitability claim. The five-point margin was exploratory and must remain frozen during the next three complete demo sessions.

## Fixed profiles and managed exits

| Profile | Side | TP | SL | Stale horizon |
|---|---|---:|---:|---:|
| `us_intraday_fixed_v1` | BUY/SELL | 1.20% | 0.70% | 60 min |
| `eu_trend_buy_v1` | BUY | 2.00% | 1.20% | 180 min |
| `eu_intraday_fixed_v1` | SELL/base | 1.00% | 0.70% | 75 min |
| `crypto_intraday_fixed_v1` | BUY/SELL | 3.00% | 1.50% | 60 min |

Breakeven protection is net of estimated costs. Trailing protection activates only when the candidate stop locks the configured minimum net gain. Finite-session entries must have enough time for the stale horizon plus force-close buffer.

## Running

Python 3.12 or newer is required.

```bash
cp .env.example .env
bash scripts/start_goblin.sh
```

Local execution:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

## Analysis contract

Run-manifest schema V11 records:

- model and feature-contract versions;
- activity and direction dataset hashes;
- all six direction segments and their provenance;
- the five-point margin and 50/50 calibration weights;
- the packaged artifact SHA-256;
- code fingerprint, watchlist, profiles and runtime settings.

Standalone `entry_decision` records include the complete nested outcome estimate, so raw/final direction probabilities and segment metadata remain auditable without duplicate shadow decisions.

## Pre-live status

Goblin remains **demo-only**. Before real capital, the project still requires verified broker exits, catastrophe protection, reconciliation, drawdown/kill-switch controls, watchdogs and controlled price precision. The current objective is repeatable calibration, not a claim of profitability.
