# Alpaca experimental adapter

Work starts from refactoring PR #89, merged into `alpaca-experimental`.
This PR targets the same experimental branch. Production is not promoted.

## Contract and scope

- Alpaca **Trading API** with API key/secret, initially paper accounts and USD US
  equities. The existing V3 strategy, risk caps and research economics remain frozen.
- Market data: explicit IEX or SIP subscription, authenticated quote WebSocket,
  reconnect/resubscribe, broker timestamps and REST quote fallback using the same feed.
- Orders: notional market/day BUY, quantity market/day SELL, durable
  `client_order_id`, status lookup and `trade_updates` WebSocket. Acceptance is
  never treated as a fill and a transport error never triggers another POST.
- Attribution: Alpaca aggregates positions by symbol. A durable adapter ledger
  must keep each Goblin BUY as a separate leg, attribute SELL orders to that leg,
  and compare the sum of remaining quantities with the broker position. An
  unexplained account difference must block trading rather than invent an allocation.
- Recovery: journal the request before submission; recover status by client ID;
  reconcile broker account identity, positions and pending orders on startup.

## Official endpoint references

Verified on 2026-09-27:

- [Order requests](https://docs.alpaca.markets/us/reference/postorder)
- [Lookup by client ID](https://docs.alpaca.markets/us/reference/getorderbyclientorderid)
- [Trading WebSocket](https://docs.alpaca.markets/us/docs/websocket-streaming)
- [Market WebSocket protocol](https://docs.alpaca.markets/us/docs/streaming-market-data)
- [Stock quote schema](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)

## Progress

Checkpoint 1: HTTP transport, market-data REST fallback and both authenticated
WebSocket protocols implemented; 16 transport/quote tests pass locally. No
runtime broker selection is changed by this checkpoint.

Checkpoint 2: paper execution adapter and SQLite order journal implemented.
Requests are persisted before submission. BUY uses USD notional; SELL uses the
specific leg's quantity, never an aggregate symbol liquidation. Cumulative fills
are idempotent across REST/stream duplicates. Account identity is pinned in the
journal. Conflicting stream economics leave a persistent safety fault rather
than disappearing on restart. Unknown submissions are looked up by client ID;
a 404 never authorizes another POST. External pending orders on a closing symbol
block conflicting SELLs.

The adapter deliberately consumes quote midpoints, with explicit
`bid_ask_midpoint` provenance and the broker quote timestamp. It does not attach
an old trade price to a fresh quote, add bracket orders, or change V3 exits.
This is a new data source, not evidence of equivalent strategy performance.

Checkpoint 3: provider/environment selection is now controlled solely by
`BROKER`, with Alpaca-specific credentials, instrument cache and IEX/SIP feed.
The factory constructs both streams and REST clients without connecting or
submitting orders. The generic market-data interface no longer requires eToro
numeric instrument IDs. Alpaca credentials are excluded from manifest snapshots.

Checkpoint 4: V3 persists the Alpaca close client identity in its start event
before dispatch. Unknown submissions retain their leg reservation and schedule
read-only confirmation, including after a crash before the completion event.
Terminal fill/no-fill evidence releases the reservation exactly once and clears
only the halt owned by that uncertainty. Missing evidence never retries a POST.
Existing brokers without preassigned identities retain their recovery guards.

Checkpoint 5: broker reconciliation can provide actual leg units and cumulative
fills for the requested close identities from one journal snapshot. V3 verifies
that each reduction matches that action's fill, baseline and requested bounds.
It keeps the leg locked and the booked exposure conservative until terminal
execution confirms the actual quantity and economics. Partial cancellations are
booked once; they are neither mistaken for full execution nor double-debited on
restart. Adapters without this evidence keep the existing portfolio-attribution
path. Startup also rejects pending closes with no matching V3 action.

Order lookup uses one HTTP attempt per scheduler dispatch. V3 keeps ownership of
persisted retry/backoff deadlines, including 429/timeouts. Late completions cannot
reopen resolved actions, and rejected actions cannot reuse an already journaled
start with a different client identity.

Checkpoint 6: the execution contract now supports preassigned BUY client IDs and
read-only recovery of the exact symbol/notional request. Alpaca returns durable
order identity/status alongside terminal execution, including partial fills after
cancellation. Startup preflight exposes owned BUY identities even when the order
is no longer pending. A missing order or nonterminal fill never proves rejection.

Checkpoint 7: V3 now persists BUY identities before dispatch and restores
request-bound, read-only confirmation after timeouts or crashes. Its separate
BUY retry journal preserves backoff deadlines across restarts. Missing evidence
retains the reservation and never resubmits an order. Terminal partial fills are
booked once using confirmed units and USD economics; only validated terminal
partial-fill evidence can relax the legacy minimum-notional guard. A late BUY
after a reduce-only exit starts a fresh inventory lifecycle.

Startup preflight recognizes only BUY evidence matching the persisted request.
Pending buys also require restored causal feature state before new risk can
return. Completing one action or recovering a reconciliation failure preserves
other unresolved BUYs and independent safety halts. Close confirmations keep
priority, and order lookups use the query lane so they do not block reduce-only
dispatch.

Checkpoint 8: the application bootstrap locks its data directory for the entire
run and persists a broker/database namespace in both a directory marker and
SQLite. Alpaca requires a fresh dedicated directory; all of its configured
output/cache paths must remain inside that directory without aliasing each
other. It pins the authoritative `/v2/account.id` in the V3 SQLite before loading
inventories, independently of the adapter journal. Changing broker/environment
or account fails closed. Existing unlabelled eToro data is preserved and bound
to its selected broker/environment; eToro account-ID pinning is not implemented
by this checkpoint. Old binaries do not honor this new lock: separate Docker
bind mounts remain mandatory. The restart guard no longer routes Alpaca through
the eToro-only manual-close watcher.

Checkpoint 8 validation: **1129 tests passed**. New cases cover concurrent
separate deployments, duplicate processes, broker/account mismatches, copied
databases, legacy eToro preservation, symlink/path escapes, output aliases,
corrupt namespace markers and restart-wrapper routing. Alpaca bootstrap remains
gated pending the universe and full-runtime checks below.

## Environment configuration

```dotenv
BROKER=alpaca_demo
ALPACA_API_KEY=replace_me
ALPACA_SECRET_KEY=replace_me
ALPACA_INSTRUMENT_ID_CACHE_PATH=data/alpaca_instrument_ids.json
ALPACA_DATA_FEED=iex
```

Canonical modes are `paper`, `etoro_demo`, `etoro_live`, `alpaca_demo`,
`alpaca_live`. The spellings `alpacademo`, `alpacalive`, `etorodemo`, `etorolive`
and hyphenated names are normalized to the canonical names.

- `paper` retains the existing local execution and eToro market data.
- eToro modes read only eToro credentials and the eToro instrument cache.
- Alpaca modes read only `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. There is no
  fallback to eToro credentials. Set the credentials of the selected account.
- `alpaca_demo` chooses `https://paper-api.alpaca.markets` and its `/stream`
  WebSocket; `alpaca_live` chooses `https://api.alpaca.markets` and its `/stream`.
  Both use Alpaca market data, with the explicitly selected `iex`/`sip` feed.
- The Alpaca instrument cache stores symbol-to-UUID mappings. Every purchase
  still fetches current asset permissions; cached UUIDs are used for lookup,
  never as proof that an asset remains tradable. Only a 404 retries by symbol.
- The durable journal is separate from the instrument cache and is derived as
  `POSITION_STORE_PATH.alpaca_demo.orders.sqlite` or
  `POSITION_STORE_PATH.alpaca_live.orders.sqlite`. Each journal is pinned to both
  environment and account ID. Back it up with the V3 state and never delete it
  as disposable cache. Keep separate V3 state/log directories per broker/account.

References: [paper/live accounts](https://docs.alpaca.markets/us/docs/paper-trading),
[asset lookup by symbol or ID](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id).

## Validation and remaining work

Local validation at checkpoint 2: **980 tests passed**, including **48 Alpaca
tests**. `compileall`, targeted Ruff unused-symbol/import checks and diff
whitespace checks pass.

Checkpoint 3 validation: **1004 tests passed**, including **72 Alpaca tests**.
Coverage includes all five broker modes, both requested Alpaca aliases, provider
credential/cache isolation, demo/live endpoint and journal separation, secret
redaction, cache restart/refresh behavior and both stream lifecycles. No network
connection is made during factory construction.

Checkpoint 4 adds V3/journal integration tests for lost responses, crashes before
completion, nested broker caches, dispatch failures and terminal rejections.
Checkpoint 5 validation: **1042 tests passed**, including **110 Alpaca tests**
and **35 V3/Alpaca recovery integration cases**. Coverage includes growing partial
fills, partial cancellation/expiry, successive pro-rata closes across multiple
legs, duplicate/out-of-order stream evidence, delayed reconciliation results,
untracked closes, external reductions and invalid attribution evidence. These
tests combine the real adapter, SQLite journals and V3 executor with mocked APIs.

Checkpoint 7 validation: **1104 tests passed**, including **171 Alpaca tests**
and **86 V3/Alpaca recovery integration cases**. BUY coverage adds interrupted
submission, missing identities/evidence, terminal zero/partial fills, restart
backoff, duplicate completions, invalid request/economic evidence, late fills
after a closed inventory and independent halt ownership. V3 startup tests cover
pending, partially filled and filled-but-unbooked BUYs with and without causal
feature checkpoints. Those startup tests stub market feeds and equity refresh;
they do not replace the full factory-connected runtime validation below.

Automated tests cover both WebSocket protocols (including binary JSON trade
updates), authentication/subscription failures, reconnect/resubscribe, ordering,
queue overflow, HTTP rate limits and no mutation retries, multiple legs of a
symbol, fractional closes, repeated fills, partial fills followed by cancellation,
timeout recovery, account changes and external activity. All network calls are
mocked; no account credentials or real paper/live orders were used.

Checkpoint 9 validation: **1146 tests passed**, including **17 new assembled-runtime
integration cases**. The real bootstrap, provider factory, HTTP clients, threaded
streams, task runners, SQLite stores, candle/features and frozen V3 planner are
combined with simulated HTTP/socket transports and a controlled clock. IEX and
SIP cases exercise an actual planner BUY, restart without resubmission, and the
84% trailing exit. Disconnect coverage proves REST fallback is reduce-only and
does not advance candles/features, reconnection restores subscriptions, fresh
accepted quotes are required for recovery, and duplicate binary trade updates
do not book inventory twice. Finite scenarios drive runtime methods directly;
two fatal-stream scenarios also exercise the actual continuous `run()` loop.

Startup now verifies active US-equity assets, fractional trading permissions for
the trading universe, an explicitly configured benchmark and complete quote
responses from the selected feed. Benchmarks are context only: they need not be
fractionally tradable. There is no automatic SPX500-to-SPY substitution. Manifest
schema 21 identifies Alpaca, its account and feed, and explicitly labels the
unchanged legacy research cost assumptions; the eToro payload observer is off.

Integration testing found and fixed stale stream health during reconnect backoff
and incomplete cleanup when startup fails after starting streams. Shutdown waits
for dispatched broker work, projects completions before checkpointing, keeps the
storage lease throughout, and discards late fallback quotes rather than submit a
new exit to a closed worker. A concurrent lease-acquisition regression test
covers that boundary. This is not yet a Docker SIGTERM/SIGKILL validation.

Checkpoint 10 validation: **1155 tests passed**. Six new tests spawn independent
Python processes running the actual continuous loop, with broker evidence owned
by the parent mock server so a killed runtime cannot erase it. They cover normal
requested stop, SIGTERM and SIGINT during an in-flight BUY (including repeated
signals and exclusive storage ownership until completion), SIGTERM after stream
startup, and SIGKILL after a broker-side BUY or 84% SELL fill but before the HTTP
response arrives. Restart books the exact quantity once using GET requests only.
The process tests verify final manifest status, handler restoration and released
storage locks. They do not run a Docker daemon or reach an actual Alpaca account.

SIGTERM/SIGINT handlers now request main-loop shutdown without performing I/O or
raising inside a SQLite transaction. They remain installed through worker joins
and checkpoint finalization; a stop request received during startup is preserved.
Queued quotes cannot dispatch orders after a stop request. Three restart-guard
regressions cover signal forwarding during child creation, during wait, and a
child-exit race; previous signal handlers are restored on wrapper exit.

Checkpoint 11 opens **experimental `alpaca_demo` bootstrap** after the assembled
runtime and process-lifecycle checks. The integration/process harness no longer
bypasses the broker-mode guard. Review also added an immediate transport-health
entry check and rejection of queued quotes from a previous connection, so a new
subscription cannot restore authority to an old buffered price. Strategy rules,
quantity attribution and eToro execution paths are retained. **1156 tests pass**.
Account/universe/feed validation failures now finalize their manifests as failed
instead of leaving a run labelled running before the event loop has started.

`alpaca_live` retains the same prospective live-capital refusal as `etoro_live`.
Configuration alone grants no live authority. The conservative operator procedure
for missing/contradictory evidence is in [alpaca-operations.md](alpaca-operations.md).
These tests use simulated transports: they do not prove account/feed entitlement,
actual network behavior or Docker deployment behavior.

Remaining deployment validation:

1. Review the operator's redacted `.env`, selected US trading universe and explicit
   benchmarks. Run eToro and Alpaca in separate containers with distinct writable
   data/log/cache mounts; no repository Compose change is required or made here.
2. Validate a separately authorized Alpaca paper run on the VPS, including stream
   availability, shutdown/restart and the first confirmed order/reconciliation.

Automatic corporate-action reconciliation, force-reconciliation operator tooling
and live promotion are outside this experimental paper integration. Missing
evidence remains blocked and never authorizes resubmission or fabricated fills.
