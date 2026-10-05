# Goblin!

Goblin is an experimental, auditable inventory trading engine written in Python.
The active application is **V3**, using the frozen `INVENTORY_RR5_ETORO5_V1`
profile. It consumes validated quotes, builds causal M1 features, plans inventory
entries/exits and records broker-confirmed fills in an append-only SQLite ledger.

> [!WARNING]
> Research/demo only. The V3 bootstrap allows `paper` and `etoro_demo`, rejects
> `etoro_live`, and restricts its universe to US/EU equities. No profitability
> or real-money trading authority is implied.

## Active application

- `app/main.py` composes the application; `app/runtime/restart_guard.py` remains
  the container entrypoint and owns the persistent restart circuit breaker.
- `app/v3/runtime.py` coordinates quotes, causal candles, shared EU/US decision
  windows, sessions, persisted features and the read-only research sidecar.
- `app/v3/planner.py`, `book.py`, `risk.py` and `economics.py` own the frozen
  inventory strategy shared by runtime and replay.
- `app/v3/live_execution.py` translates intents into broker mutations, preserves
  per-symbol BUY reservations across uncertain outcomes/restarts, and reconciles
  close quantities separately from confirmed economic fills.
- `app/brokers/base.py` defines execution, account preflight, equity provenance
  and rejection contracts. Concrete payload parsing belongs to broker adapters.
- `app/runtime/factories.py` builds independent execution and market-data clients.
  Paper execution currently consumes the same eToro market-data pipeline.
- `app/research/` and audit journals retain causal observations without changing
  the strategy. Legacy replay/calibration tools remain available offline.

See [V3 inventory architecture](docs/v3-inventory-recoverability.md),
[broker integrity](docs/v3-runtime-broker-integrity.md), and
[experimental refactoring checkpoints](docs/refactoring-experimental.md).

## Historical directional research (V1/V2)

The sections below document the earlier directional experiments and retained
replay/calibration contracts. They are **not the active V3 trading policy**.
The unused V1 live orchestrator, pending-entry workflow and position-store
writers have been retired. Shared historical scoring, lifecycle, replay and
archive deserialization remain available; V3 still refuses active legacy
SQLite positions rather than migrating or deleting them.

### Diagnostic outcome probabilities

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

### Historical managed edge and selection

The conditional direction break-even estimate remains journalled:

```text
direction_break_even
= net loss at SL / (net gain at TP + net loss at SL)

direction_edge
= P_DIRECTION - direction_break_even
```

It is no longer a universal gate. The active `MANAGED_EDGE_V1` policy estimates:

```text
P_PROTECTION
P_MANAGED_POSITIVE
EXPECTED_MANAGED_NET_RETURN
managed_edge = EXPECTED_MANAGED_NET_RETURN - 0.05%
```

Candidates must exceed their frozen segment floors for protection and positive
outcome, then have non-negative managed edge.

Selection order:

1. apply entry route, readiness, feasibility and hard economics;
2. apply the managed protection, positive-outcome and managed-edge gates;
3. rank eligible candidates by managed edge, protection, positive probability,
   retained direction edge and deterministic candidate ID;
4. keep the asset-specific top-N: two crypto, one US equity, one EU equity;
5. apply RiskManager and execute.

The former minimum-`P_TP`, maximum-`P_TOUCH` and direction-edge vetoes do not
exist. The top-N policy and the absence of portfolio backfill remain deliberate.

### Evidence behind V2

The direction dataset contains all 3,527 labelled candidates from 22, 23, 24, 27 and 28 July 2026, including selected and rejected candidates. It contains 1,547 decisive paths: 475 `TP_FIRST` and 1,072 `SL_FIRST`.

Cross-day validation of the segmented direction architecture produced:

```text
P_DIRECTION AUC:   0.643
P_DIRECTION Brier: 0.202
```

A stricter train-22–24/test-27–28 check produced approximately AUC 0.588 and Brier 0.217. These figures justify a demo experiment, not a profitability claim. The five-point margin was exploratory and must remain frozen during the next three complete demo sessions.

### Historical fixed profiles and managed exits

| Profile | Side | TP | SL | Stale horizon |
|---|---|---:|---:|---:|
| `us_intraday_fixed_v1` | BUY/SELL | 1.20% | 0.70% | 60 min |
| `eu_trend_buy_v1` | BUY | 2.00% | 1.20% | 180 min |
| `eu_intraday_fixed_v1` | SELL/base | 1.00% | 0.70% | 75 min |
| `crypto_intraday_fixed_v1` | BUY/SELL | 3.00% | 1.50% | 60 min |

Breakeven protection is net of estimated costs. Trailing protection activates only when the candidate stop locks the configured minimum net gain. Finite-session entries must have enough time for the stale horizon plus force-close buffer.

Position management uses executable prices throughout: bid for a BUY exit and
ask for a SELL exit. Broker fills are canonical when available. Post-trade P&L
deducts explicit costs only; spread remains a pre-trade feasibility input and is
not deducted a second time from executable fills.

Two explicit breakeven profiles exist:

| Profile | Crypto | EU equities | US equities | Status |
|---|---:|---:|---:|---|
| `corrected_baseline_v1` | 0.20% | 0.55% | 0.60% | historical default |
| `delayed_equity_trigger_v1` | 0.20% | 0.65% | 0.70% | selectable experiment |

The buffer, trailing, TP/SL, stale horizon, sizing, risk and selector are identical
between profiles. Select an experiment only with `BREAKEVEN_PROFILE`; the chosen
name and thresholds are recorded in every manifest and startup event.

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
python -m pip install -e ".[dev]"
python -m app.main
```

Replay a logged cohort with the shared lifecycle:

```bash
python scripts/replay_breakeven_profiles.py PLAN.json report.json \
  --output-markdown report.md --validate-archives
```

## Historical analysis contracts

The V1/V2 run-manifest schema V14 recorded:

- model and feature-contract versions;
- activity and direction dataset hashes;
- all six direction segments and their provenance;
- the managed selector and artifact provenance;
- the executable-price, position-economics, lifecycle, cost, close-taxonomy and
  cooldown contract versions;
- the selected breakeven profile and exact thresholds;
- the broker-fill-priority and explicit-cost-only convention;
- the packaged artifact SHA-256;
- code fingerprint, watchlist, profiles and runtime settings.
- the side-neutral research, microstructure, payload-schema,
  reconstructibility and run-health contracts, causal cutoff, cadence, feature
  hashes and paths.

Standalone `entry_decision` records include the complete nested outcome estimate, so raw/final direction probabilities and segment metadata remain auditable without duplicate shadow decisions.

Daily summary schema V10 and analysis-ready schema V13 expose each closed
position’s signal price, executable estimate, broker fill provenance, bid/ask,
observed spread, gross P&L, explicit costs, net P&L and executable MFE/MAE.

See [Position lifecycle V2](docs/position-lifecycle-v2.md),
[Managed Edge V1](docs/managed-edge-v1.md) and
[the breakeven replay decision](docs/breakeven-replay-v1.md). The separate
[Side-neutral market research V1](docs/side-neutral-market-research-v1.md)
contract documents the read-only prospective dataset and its limits.

## Pre-live status

Goblin remains **demo-only**. The close-detail endpoint is integrated, but real
run evidence must still confirm fill availability and timing. Before real capital,
the project also requires catastrophe protection, drawdown/kill-switch controls,
watchdogs and controlled price precision. The current objective is repeatable
calibration, not a claim of profitability.
