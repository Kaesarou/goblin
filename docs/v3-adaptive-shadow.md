# Adaptive RR5 — prospective research v1

Two isolated virtual portfolios receive the same accepted US equity M1 decision
quotes: frozen RR5 ETORO5 and INVENTORY_ADAPTIVE_RR5_V1. No broker, executor,
real order, real SQLite state, risk authority, or real order intent book is
passed to the experiment. Active RR5 remains unchanged. Both demo brokers enable
the experiment by default, paper/live do not. An exception disables shadow for
the current runtime, not trading.

## Experimental assumptions (not calibrated)

- Each arm starts flat at USD 100,000 on every runtime process start. For now
  no restart recovery of shadow state: analyze each run separately. Existing
  demo positions are not imported into either arm. EU/crypto are excluded until
  quote/account currency normalization is independently validated.
- Fills use the next consecutive accepted M1 decision quote, ask for buys and
  bid for sells. No intraminute fills, no bridging missing minutes or overnight
  gaps. This intentionally conservative proxy is not execution parity.
- Same RR5 planner/forager and 5 slots, 5 fills, 4%/15% principal caps in both
  arms; fill-time caps use marked equity including immediate spread and fees.
  $10 entry/dust proxy for both, not an authoritative broker minimum. RR5
  profit exits have precedence except at hard experimental age expiry.
- Costs use broker research schedule (eToro $1 per entry or per physical
  simulated close leg, Alpaca $0); not broker-confirmed fees. No latency,
  slippage, market impact, financing, or taxes modeled beyond quoted spread.
- Market regime uses return from the first observed quote of the US session,
  15-minute benchmark momentum, and positive-return breadth of the configured
  US watchlist. Require 60% coverage and at least 5 valid symbols. Stress:
  benchmark <= -1.5% and breadth <=25%; bearish: <=-0.5% and breadth <=40%.
  Non-stress transitions require 3 consecutive windows. Unknown blocks adaptive
  entries and reinforces; stress blocks both; bearish blocks both. Recovery
  requires observed rebound momentum and breadth; not an AI-bubble predictor.
- Age counts observed US session transitions since initial fill, not calendar
  days or guaranteed complete exchange sessions. No reset on reinforcement.
  At 5 transitions stale: block reinforces; stress can once reduce 50%;
  rotation may close a losing stale inventory when all slots occupied and a
  ranked alternative has readiness <=0 and a positive RR5 gross-capture proxy
  after estimated costs including incumbent exit fees. It does NOT predict
  superior return. At 10 transitions schedule full risk exit. A risk-closed
  symbol cannot reopen in the same observed session. Rotation waits for an
  actual simulated sell before planning fresh risk. Cash is permitted.
- Sector limit: two inventories per sector, unknowns grouped conservatively.
  Partial static sector map, explicitly not a validated factor model.

## Evidence and replay

`adaptive_shadow.jsonl.gz` is per run, capped at 32 MiB with 1 GiB minimum free
disk reserve; logs are reset by deployment policy. It records regime/lifecycle
transitions, fills, rotation proposals and five-minute equity/age/exposure
checkpoints. Heartbeats include both arms and suppression/failure counts.
Mark-to-bid equity includes unrealized P/L. Stale marks are identified and block
new exposure. Capital-time and saturation are observed-window estimates, not
wall-clock totals. A full simulation on historical archives must feed the same
ShadowExperiment.step and observe_benchmark methods in chronological order
with the original decision/receive timestamps. No historical experiment or
statistical validation is claimed by this implementation.
Compare baseline/adaptive only on the same run/same broker, then examine
robustness across brokers and sessions. Do not promote to order authority.
