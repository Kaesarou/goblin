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

Automated tests cover both WebSocket protocols (including binary JSON trade
updates), authentication/subscription failures, reconnect/resubscribe, ordering,
queue overflow, HTTP rate limits and no mutation retries, multiple legs of a
symbol, fractional closes, repeated fills, partial fills followed by cancellation,
timeout recovery, account changes and external activity. All network calls are
mocked; no account credentials or real paper/live orders were used.

**Alpaca configuration and factory selection are implemented, but V3 execution
remains gated.** `alpaca_demo` fails explicitly at bootstrap until the universe
and full-runtime integration below are validated. `alpaca_live` uses the same prospective live
capital refusal as `etoro_live`. No live authority is granted by configuring it.
The strategy rules and existing broker quantity-attribution rules are unchanged.
Remaining work:

1. Complete US-equity universe/benchmark validation and broker-specific manifest metadata.
   Keep live capital disabled and do not silently replace eToro index benchmarks
   with ETFs or present legacy eToro cost estimates as Alpaca actual costs.
2. Test the factory-connected streams, startup, shutdown and REST fallback
   through the complete V3 runtime (not only the adapter lifecycle).
3. Complete operational recovery for unknown BUYs, including partial fills still
   pending at the BUY timeout. They currently retain the reservation and fail
   closed; they are not automatically projected into V3 on later execution.
4. Define operator recovery for permanently missing evidence. A close started
   before a crash but without an adapter reservation remains locked; a reserved
   close returning 404 remains pollable, but absence never proves rejection.
   These crash boundaries are tested and never cause a second POST. Transient
   REST/stream position races also fail closed rather than guess attribution.
5. Review the complete integration and validate a separately authorized Alpaca
   paper run. Corporate-action reconciliation, operator recovery tooling and live
   promotion are not implemented by these checkpoints.
