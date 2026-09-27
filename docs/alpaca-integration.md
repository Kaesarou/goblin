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

## Validation and remaining work

Local validation at checkpoint 2: **980 tests passed**, including **48 Alpaca
tests**. `compileall`, targeted Ruff unused-symbol/import checks and diff
whitespace checks pass.

Automated tests cover both WebSocket protocols (including binary JSON trade
updates), authentication/subscription failures, reconnect/resubscribe, ordering,
queue overflow, HTTP rate limits and no mutation retries, multiple legs of a
symbol, fractional closes, repeated fills, partial fills followed by cancellation,
timeout recovery, account changes and external activity. All network calls are
mocked; no account credentials or real paper/live orders were used.

**This is not yet a runnable Alpaca V3 mode.** The existing eToro/paper factory
and strategy are unchanged. Required next steps before enabling it:

1. Add validated Alpaca settings and factory selection, US-equity universe/feed
   configuration, separate persistence paths and accurate manifest metadata.
   Keep live capital disabled and do not silently replace eToro index benchmarks
   with ETFs or present legacy eToro cost estimates as Alpaca actual costs.
2. Connect both stream lifecycles to V3 and test startup, shutdown and REST fallback
   end to end. Remove the eToro instrument-ID requirement from the generic market
   data protocol rather than manufacture numeric IDs for Alpaca symbols.
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
