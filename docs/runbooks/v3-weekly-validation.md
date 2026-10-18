# Weekly V3 validation after S2

The production deployment runs the refactored eToro DEMO runtime. Alpaca remains
on `alpaca-experimental` and its separate deployment. ETORO5 signal, reentry,
trailing, recoverability, hedge and exposure parameters remain frozen.

## Entry economics

Each confirmed eToro open retains its raw order response, raw execution price,
the causal bid/ask/last and quote timestamps, and signed price deviation in bp.
An absolute deviation greater than **100 bp** from the triggering ask, or an
invalid price, quarantines the fill as `ECONOMICS_UNRESOLVED`. This is a
conservative adapter alarm, not proof that every such fill is impossible and not
a slippage target. It deliberately catches the four S2 deviations of 229–597 bp.

Confirmed units and conservative USD exposure remain in the book. The causal
ask is explicitly a **provisional placeholder**, never a certified execution
price. Inventory planning/trailing and profit-exit dispatch are disabled for
that inventory, and further BUYs are blocked. Other inventories may continue
their reduce-only exits. An explicit emergency reduction of quarantined units
does not manufacture realized P&L: `unresolved_exit_units` records the gap.

Read-only revalidation uses one governed P&L GET for the affected open positions.
It verifies exact position identity, cached instrument, BUY/1x, current broker
units, `openRate` and USD `amount`. If the opening order identity is retained,
P&L `orderId` must match **positionExecutions.openingData.orderId**, not the
distinct top-level order-lookup identity. A corrected price that returns to normal must pass the original causal price
alarm. A separately corroborated exceptionally favorable price follows the
controlled full-close procedure below. Missing/ambiguous/invalid responses keep
the halt.
The suspect price is never replaced by the quote as broker economic truth.

Successful proof appends `OPEN_FILL_ECONOMICS_RECONCILED` and/or
`OPEN_ACCOUNT_NOTIONAL_RECONCILED`, then updates the projection. Quantity and
fill count are unchanged. Price resolution restarts the causal trailing bundle;
it does not infer a historical peak using the rejected basis. Independent
unknown-order, external-account or close/reconciliation halts are preserved.

Retry deadlines are committed to the append-only SQLite ledger **before** GET
dispatch. Backoff is 60, 120, 240, then 300 seconds; restart preserves deadlines.
The shared query lane prioritizes active close mutations, periodic quantity
reconciliation, open-economics recovery, then historical close economics. No
read-only recovery GET sends a mutation; the exceptional close is a separate
explicit reduce-only execution task.

## Resolved exposure is distinct from resolved historical economics

A notional-only anomaly can also retire after a complete broker-confirmed close
ledger and a new exact portfolio observation prove zero remaining units. Mere
absence from P&L, a timeout, or local flatness cannot rearm BUY. This event carries
`resolution=confirmed_closed_exposure` and
`original_account_notional_resolved=false`: it removes an obsolete **risk**
block, without certifying the original invested principal or rewriting S2 P&L.
Closed price anomalies without independent exact original-price proof remain
unresolved and blocked; a broker-confirmed favorable fill can be cleared only
after the complete close ledger and exact zero-unit broker observation.

Legacy active $1 account-notional fills restore conservative requested exposure
without altering their original immutable events.

## Confirmed exceptionally favorable eToro execution

An outlier is **not** automatically a trading opportunity or proof of profit.
Only an abnormally **favorable** raw fill with an independent authoritative
`etoro_exact_pnl_position_usd` response matching the same position, opening
order identity, units, price and plausible account notional is eligible for
automatic exception handling. Unfavorable, missing, conflicting or unverifiable
fills remain quarantined. The broker's actual favorable price is preserved as
cost-basis evidence; the original causal quote stays in the audit journal.

The durable `OPEN_EXCEPTIONAL_PRICE_CONFIRMED` event does **not** clear BUY
authority. On a fresh accepted in-session WebSocket quote, V3 schedules a
reduce-only **100% close of only that physical position_id**, without changing
Point-M's 84% normal close geometry or another leg's exposure. The close action
identity is stable across restarts. A pending or unknown submission cannot be
resubmitted blindly; broker close acceptance alone is not a fill.

Even after a confirmed full close, BUY authority remains disabled until
broker portfolio reads prove **exact zero units**, all closed units are
attributed by durable `EXIT_FILLED` / `EXIT_ECONOMICS_CONFIRMED` events, and
no independent halt survives. Later corrected independent broker evidence may
supersede a preliminary outlier report; it must still pass the normal causal
sanity check. Journal-based realized economics must never be called confirmed
net P&L without matched broker cost/FX evidence.

## Alpaca market-data outage

Distinguish transport subscription/health from **accepted quote readiness**.
`market_data_phase` and `fresh_quotes_healthy` describe recovery and gate
new BUYs. The first accepted quote from a replacement connection retires old
resting intents rather than executing stale crosses. Exit-only reconciliations,
heartbeats and broker maintenance continue. No REST diagnostic quote is promoted
to executable market evidence. The bounded FIFO, 128-event/25-ms runtime budget,
IEX subscription and Point-M settings are unchanged.

`ordering_equal_timestamps`, `ordering_older_timestamps`, and
`ordering_old_epoch` distinguish causes of dropped quotes; no drop filter is
disabled without empirical evidence. Heartbeats now include active inventory
age, exposure, gross price-times-units proxy, MFE/MAE and resting SELL
limit-to-bid distance. That proxy is **not** account-currency net P&L.

## Partial-close contract

Strategic profit closes remain **84% pro-rata** across every active physical leg.
If **any physical leg** would retain less than **10 USD** principal, the existing
adapter collapses the **entire inventory** to a 100% close. This is a deterministic
broker translation, not a strategy threshold change. The manifest declares this
exception; close events retain both strategic and execution fractions and dust
diagnostics. Scientific Point M remains the frozen historical reference; a
broker-adapted comparison must explicitly account for this exception.

## Sunday cleanup and next weekly audit

Before deleting logs, archive the complete week's ZIP plus the small manifest,
summary/QC and `state_start`/`state_end` files. Preserve the persistent SQLite
outside the log directory; obtain a consistent copy using SQLite's online backup
API rather than copying a live database without its WAL. Do not reset SQLite or
delete broker positions to create a new week. A clean process restart after
archiving creates a new run ID while retaining position/retry/feature state.

Retain account screenshots at both boundaries: account value, cash, invested
principal and unrealized P&L, with timestamps and any external transfers. Compare
those boundaries with the actual fill ledger; zero explicit fee fields do not
prove zero all-in costs. Never label provisional or unresolved economics net P&L.

The raw quota remains **512 MiB/run** and the free-disk reserve **1 GiB**. Check
quota/suppression markers and heartbeat counters; candles, inventory events and
broker ledger must continue when raw persistence is suppressed. Manual deletion
of part of an ongoing run limits reconstruction; it is not evidence of runtime
corruption.

Collect a full week after deployment without parameter changes. Check quarantine
and recovery events, BUY authority continuity, physical close quantities,
independent risk halts, boundary account accounting and raw quota behavior. Tests
validate adapter/runtime behavior; they do not establish prospective alpha or
physically rerun the full Point M corpus.

Endpoint contracts checked against official eToro documentation:

- https://api-portal.etoro.com/api-reference/trading--demo/get-account-pnl-and-portfolio-details
- https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details
