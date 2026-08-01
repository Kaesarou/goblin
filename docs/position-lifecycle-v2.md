# Position lifecycle V2

Position lifecycle V2 is the only runtime and replay contract for open-position
economics. It replaces the former last-price lifecycle and textual close reasons;
there is no runtime alias or fallback for either contract.

## Versioned contracts

| Contract | Version |
|---|---|
| Executable prices | `side_aware_executable_prices_v2` |
| Position lifecycle | `executable_position_lifecycle_v2` |
| Position economics | `position_economics_v2` |
| Trade costs | `executable_fills_explicit_costs_v2` |
| Close taxonomy | `position_close_taxonomy_v2` |
| Cooldown | `trade_cooldown_v2` |
| Breakeven profiles | `breakeven_profiles_v1` |
| Run manifest | schema 12 |
| Daily summary | schema 10 |
| Analysis-ready summary | schema 13 |

Every run manifest records these versions and the selected breakeven profile.

## Price convention

`LastExecution` remains the candle and signal price. It is diagnostic once a
position is open.

Executable economics use the side that would close the position:

| Position | Entry estimate | Exit estimate | Lifecycle price |
|---|---|---|---|
| BUY | ask | bid | bid |
| SELL | bid | ask | ask |

The lifecycle price drives high/low extrema, MFE, MAE, breakeven activation,
trailing activation and movement, active-stop tests, TP, initial SL, stale exits
and session force closes.

The entry P&L price is the broker fill when present, otherwise the executable
entry estimate. The exit P&L price is the broker close fill when present,
otherwise the executable exit estimate captured at detection. Signal price,
last-execution detection price, bid, ask and observed spread remain separate
fields and cannot silently replace one another.

## Cost convention

Pre-trade economics estimate:

```text
estimated total cost
= observed spread cost
+ estimated open fee
+ estimated close fee
+ estimated fixed fees
```

This total remains the input used for TP feasibility, expected profit and sizing
checks.

Post-trade economics use:

```text
gross P&L = side-aware change between entry and exit P&L prices
net P&L   = gross P&L - explicit costs
```

Explicit costs are the open, close and fixed fees that are not already present in
the prices. Spread is not deducted again because executable entry/exit sides or
broker fills already materialise it. Live, replay, mark-to-market and protected
exit counterfactuals all call the same `calculate_position_pnl` function.

## Deterministic rule order

For each accepted market snapshot, `PositionLifecycleEngine` evaluates exactly:

1. update executable high and low, while retaining last-price extrema as
   diagnostics;
2. activate or improve breakeven;
3. activate or improve trailing;
4. test the active protected stop;
5. test take profit;
6. test the initial stop;
7. evaluate stale exit;
8. apply session force close.

The engine is pure: position state plus snapshot plus configuration produces the
next immutable state, optional stop update and optional close signal. Live
`PositionTracker`, deterministic position replay and stateful cohort replay all
use this implementation. Label generation must call `replay_position`; it has no
separate OHLC lifecycle formula.

## Canonical close taxonomy

Only `PositionCloseReason` is accepted:

| Reason | Meaning | Cooldown |
|---|---|---:|
| `take_profit` | configured TP | 30 min, same side |
| `initial_stop` | unprotected initial SL | 45 min, same side |
| `protected_breakeven` | active breakeven stop | 15 min, same side |
| `protected_trailing` | active trailing stop | 15 min, same side |
| `stale_exit` | stale-position rule | 15 min, same side |
| `session_force_close` | finite-session force close | 15 min, same side |
| `manual_or_broker_close` | confirmed external close with an executable estimate | 15 min, same side |
| `unknown_confirmed_close` | confirmed close without an available price | 15 min, same side |

An `initial_stop` also creates the existing 15-minute bilateral symbol lock. The
same-side initial-stop cooldown remains 45 minutes. A protected exit is never
reclassified from the sign of gross or net P&L, so a positive breakeven cannot
become a take profit.

## Broker close confirmation

A close request creates a durable pending close and keeps its risk reservation.
Request acceptance records `close_order_id`, but never creates a fill.

Broker work remains outside the WebSocket consumer:

1. the main loop emits a typed close signal and persists the pending state;
2. a broker worker submits the request;
3. background reconciliation obtains a fresh portfolio snapshot;
4. only confirmed portfolio absence permits closure;
5. when a close-order ID exists, the worker queries eToro’s close-order detail
   endpoint;
6. a returned position `rate` is the canonical broker exit fill;
7. missing or failed fill lookup is explicitly journalled and retains the
   detection-time executable estimate as provenance;
8. only the main loop mutates `RiskManager`, `PositionTracker` and SQLite and
   releases risk.

Submission-unknown, accepted-but-still-open and restored pending states all keep
risk reserved. Ordinary external broker disappearance still requires the existing
three spaced portfolio absences after its grace period.

## Persistence upgrade rule

`open_positions`, `pending_closes` and `closed_trade_memory` store their contract
version and canonical typed fields. Empty obsolete tables are rebuilt. Non-empty
obsolete tables cause startup to fail while preserving every row; Goblin does not
infer entry/exit sides, costs or historical close reasons. Deployment therefore
requires a flat, reconciled portfolio and no unresolved legacy cooldown state.

## Observability

Every completed calculation records:

- signal/last-execution price;
- executable entry estimate and entry broker fill;
- detection bid, ask, last and observed spread;
- executable exit estimate and exit broker fill;
- selected P&L entry and exit prices with provenance;
- gross P&L, explicit costs deducted and net P&L;
- executable MFE and MAE;
- last-price extrema only as diagnostics;
- canonical close reason.

Summaries aggregate P&L by exit-price source and close reason and retain the full
per-position calculation. A missing broker fill is therefore visible, not
fabricated.
