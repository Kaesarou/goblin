# V3 runtime / broker integrity

This change implements the supplied post-hotfix execution findings for
`INVENTORY_RR5_ETORO5_V1`. It is not a strategy experiment or a deployment.

## Account equity contract

eToro equity is read only from
`GET /api/v1/trading/info/aggregate-portfolio`,
`accountTotals.accountTotalValue`. Values must be numeric (not booleans or
numeric strings), finite and positive. There is no `/pnl`, credit, cash,
portfolio or component-sum fallback. The documented formula describes the
broker total; the runtime does not calculate it itself.

The last valid broker reference persists value, UTC observation time, source
and version. On restart it can plan only existing inventory exits. Initial
BUY and reentry require an equity validated during the current run, the
session's new-entry permission, coordinator permission and executor authority.
A failed refresh does not erase a previously validated reference. Without
any reliable reference, existing reduce-only intents remain and the runtime
reports `reduce_only_planning_blocked_no_equity_reference`. It does not invent
the exposure-dependent trailing thresholds.

Account reads and REST market data share a conservative 45-request/60-second
rolling budget. Order lookups and close confirmations have a distinct
45/60 budget. A 429 cools down only its bucket; Retry-After is honored when
present. Market-data classification remains deliberately conservative.

## Broker close state machine

Each close action persists `pre_close_units`, independently of older actions
on the same position. An accepted submission is not a fill; returned
`UnitsToDeduct` is not execution proof. The documented close-details parser
remains unchanged. Confirmed execution units are required even for full closes.

Strict book/broker comparison remains separate from action attribution. Only
legacy actions lacking a persisted baseline can use the existing 1% migration
tolerance for attribution. Newly submitted actions use strict comparison.
Malformed or missing authoritative portfolio collections never mean zero units.

A confidently attributable broker reduction produces
`BROKER_QUANTITY_RECONCILED`, updates book quantity, and immediately releases
that action's position lock. Its economics can remain pending while another
partial close operates on the same position ID. Late
`EXIT_ECONOMICS_CONFIRMED` updates economics only, never quantity again, and
cannot unlock a newer mutation. Event projection and live updates are
idempotent and tested for identical units and P&L after restart.

An ambiguous mutation remains locked. An unattributed reduction blocks new
risk even though book quantity is reconciled to broker truth. Only one
mutation per broker leg is possible; multi-leg pro-rata plans wait if any
participating leg is still locked. Economics-only recovery is not a risk halt.

## Restart-safe confirmation recovery

Both errors and `execution is None` use exponential backoff, beginning at
15 seconds. Unknown mutations cap at 300 seconds; confidently quantity-resolved
economics-only actions cap at 3,600 seconds. These caps are internal load
shedding, **not assumptions about eToro close-order retention**, which is
undocumented. The initial accepted-order delay remains 10 seconds.

The dedicated action-keyed scheduler persists attempt count, UTC
`next_attempt_at`, last error type, HTTP status and result state. A retry
deadline is saved before dispatch too, covering interruption during GET.
Restart computes `max(0, next_attempt_at - utc_now)` and converts only that
remaining delay to local monotonic time. No monotonic timestamp is persisted.
Resolved or explicitly abandoned actions delete their scheduler rows.

The single-worker QUERY lane dispatches due work in this order: active close
mutation confirmation, periodic broker quantity reconciliation, then historical
economics-only confirmation. Priorities are reconsidered after each completed
query; an already running query is not interrupted. A due reconciliation therefore
runs before the next economics-only lookup, even with a historical backlog.
Backoff deadlines, rate budgets and the absence of an economics-only risk halt
are unchanged. The manifest exposes this ordered `query_priority` list.

The stale-close halt applies only to unresolved mutations. Metrics distinguish
active mutations, quantity-resolved economic confirmations, unattributed
reconciliations, stale categories, and mutation/economics-only attempts.

## Operator acknowledgment

Stop the runtime before applying an acknowledgment. Back up its SQLite ledger
using the existing operational backup process. Use the same broker environment
and state path as the stopped runtime. No symbol or position is hardcoded.

```bash
python -m scripts.acknowledge_v3_broker_reconciliation \
  --state-path /path/to/goblin.sqlite \
  --position-id POSITION_ID \
  --broker-units EXPECTED_UNITS \
  --reason "Human explanation; explicitly abandon this close's unprovable economics"
```

The command is dry-run by default. Only add `--apply` after inspecting the
proposed audit. It reconstructs events, requires an actual outstanding
unattributed reconciliation, reads broker quantity, and strictly compares
broker, expected and ledger units. It never sends an order. Apply atomically
checks that the ledger has not changed during the broker read and appends
`BROKER_RECONCILIATION_ACKNOWLEDGED` with the human reason, source event IDs
and explicitly abandoned action IDs. It invents neither realized P&L nor fee.

On restart, acknowledged actions no longer poll or monopolize the position;
the corresponding unattributed halt disappears. Broker-reconciled quantities
remain, and abandoned economics remain unknown. This acknowledgment cannot
resolve an unknown opening order. The operator must still keep the runtime
stopped: the ledger guard does not coordinate with an external trading process.

## Session / candle lifecycle

EU/US equities are closed on local Saturday and Sunday; crypto 24/7 is unchanged.
No holidays/calendar dependency is introduced. Clock finalization uses the
session containing the last accepted in-session snapshot and never passes its
end. The last M1 of that session is retained, without flat overnight candles.

Session-key changes finalize the old session, clear obsolete decision-window
state, and reset candle builder, MTF engine, coordinator and microstructure
research state. Stored MTF history, inventory identities and inventory trailing
state survive. A candle's own `opened_at` determines its session before any
MTF, feature, inventory or decision-window update; invalid candles are diagnosed
and rejected. This covers the observed 06:59 → 07:00 failure.

The end-of-session entry cutoff blocks both initial BUY and reentry. Reduce-only
planning remains available. There is no daily ETORO5 force-close.

## Observability and versioning

State-start/end, heartbeats, QC and final manifest expose combined risk
authority and its per-symbol inputs/blockers, equity reference provenance,
pending confirmation categories and retry deadlines. Repeated equity failures
increment dedicated refresh metrics, not the generic error counter or repeated
incident events. Unexpected runtime exceptions set `stop_reason=error` and
are re-raised; the run is failed consistently.

The runtime contract is `inventory_runtime_v3_6`, broker exit translation is
`pro_rata_partial_close_point_m_dust_v3`, checkpoint/QC schemas are 2 and manifest
schema is 20. These declare changed authority/lifecycle semantics. Existing
feature/inventory restart schema `v3_runtime_state_v1` is unchanged; equity and
retry state have separate v1 contracts and reject unknown versions.

## Tests and frozen non-goals

Tests cover strict aggregate equity; restored exit-only authority; blocked BUY
and reentry with unchanged exit math; sequential partial closes and late
economics; strict mismatch and legacy attribution; operator dry-run/apply,
restart and concurrent-ledger rejection; persisted UTC retry/backoff; independent
GET buckets; timezone-aware weekends; Friday → Monday and 06:59 → 07:00;
cutoff, inventory preservation, and coherent error/QC artifacts.

No threshold, retracement, EMA, reentry or close parameter in `app/v3/config.py`
is changed. Long-only, 5 inventories, 5 fills, 4% per symbol, 15% gross,
Recoverability authority OFF, hedge OFF, leverage 1, 84% normal exit,
pro-rata legs, native `UnitsToDeduct`, same partial-close position ID,
Point-M residual below $10 → full close, strict unit reconciliation and
10-second raw sampling all remain frozen. No strategy analysis, retuning,
edge search, broker order, VPS deployment or merge is part of this PR.

The supplied incident findings and API contract underpin these regressions.
Incomplete uploaded archives cannot establish fresh end-to-end broker behavior;
tests use mocks, and no assumption is made about old close-order retention.

## PR #75 review validation

The scheduler review adds coverage for active-mutation precedence, due
reconciliation before economics-only recovery, a multi-action historical backlog,
resumption of economic confirmation and separate attempt counters. Existing tests
continue to verify sequential partial closes, late fills and exact event replay.

Failure diagnostics now format only container `.State`; the full inspect payload
is never printed or persisted. Regression tests reject raw inspect invocations and
sensitive diagnostic selectors, and execute the diagnostic function with a fake
Docker for both successful and failed diagnostic commands. Existing image identity
verification, logs, rollback and deployment conditions are unchanged.

Validation after the review corrections: 926 repository tests passed;
`git diff --check` and `bash -n scripts/deploy_release.sh` passed. The full PR diff
against develop was reviewed. Frozen config is byte-identical and `_exit` /
`_reentry` ASTs are unchanged. No additional broker concurrency, retry threshold,
rate budget or strategy change was introduced by this review. Docker diagnostics
were tested with mocks; no production deployment or live broker order was run.
