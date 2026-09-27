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

Implementation in progress. Required validation includes disconnected streams,
out-of-order events, partial fills, uncertain submissions, repeated confirmation,
multiple legs of one symbol, restart recovery and external account activity.
No credentials or live/paper trading calls are needed for the automated tests.
