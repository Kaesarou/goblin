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

Automated tests cover both WebSocket protocols (including binary JSON trade
updates), authentication/subscription failures, reconnect/resubscribe, ordering,
queue overflow, HTTP rate limits and no mutation retries, multiple legs of a
symbol, fractional closes, repeated fills, partial fills followed by cancellation,
timeout recovery, account changes and external activity. All network calls are
mocked; no account credentials or real paper/live orders were used.

**Alpaca configuration and factory selection are implemented, but V3 execution
remains gated.** `alpaca_demo` fails explicitly at bootstrap until the recovery
integration below is validated. `alpaca_live` uses the same prospective live
capital refusal as `etoro_live`. No live authority is granted by configuring it.
The existing eToro/paper behavior and strategy are unchanged. Remaining work:

1. Complete US-equity universe/benchmark validation and broker-specific manifest metadata.
   Keep live capital disabled and do not silently replace eToro index benchmarks
   with ETFs or present legacy eToro cost estimates as Alpaca actual costs.
2. Test the factory-connected streams, startup, shutdown and REST fallback
   through the complete V3 runtime (not only the adapter lifecycle).
3. Extend generic V3 close recovery to retain the client ID from an unknown
   submission. Currently V3 halts on that exception without scheduling an ID
   lookup. The adapter's recovery tests alone do not cover this runtime gap.
4. Exercise partial SELL fills/cancellations during V3 quantity reconciliation,
   including crashes between adapter persistence and V3 events. The existing
   attribution rules assume the requested reduction; intermediate partial fills
   must not release a mutation or leave an unexplained quantity reduction.
5. Review the complete integration and validate a separately authorized Alpaca
   paper run. Corporate-action reconciliation, operator recovery tooling and live
   promotion are not implemented by these first two checkpoints.
