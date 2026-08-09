# Market Data V2 — WebSocket runtime

Market Data V2 replaces the ten-second eToro rates polling loop with an
event-driven transport. The transport contract is independent from the
versioned scoring policy.

## Runtime sources

- eToro WebSocket is the primary source in `auto` and `websocket` modes.
- REST rates are fetched every 60 seconds for diagnostics only.
- A REST safety fallback exists only for symbols with an open position.
- All positions requiring fallback share one grouped request on a fixed
  ten-second cadence.
- A quiet symbol without an open position never triggers REST fallback.
- Paper mode and explicit `polling` mode use the same event runtime through a
  polling feed; the strategy no longer owns transport concerns.

## Safety rules

- deduplicate by connection plus `message_id`, never by `price_rate_id`;
- reject WebSocket events older than the last accepted WebSocket timestamp for
  the symbol;
- validate quotes before advancing the accepted timestamp watermark;
- after 20 accepted symbol changes, quarantine an isolated jump above the
  adaptive `quote_quality_v2` threshold before it can reach candles, candidates
  or position lifecycle handling;
- accept an ordinary quote immediately, and accept a genuine discontinuous
  level on its next confirming quote rather than imposing universal
  double-confirmation;
- keep a follow-up that confirms neither the baseline nor the pending level in
  quarantine, rebasing the pending level for the next observation;
- block new entries while an open-position symbol is in REST fallback,
  recovering, or blocked;
- keep REST fallback snapshots outside candles, signals, market context and the
  WebSocket timestamp watermark;
- use fallback snapshots only for TP, SL, trailing stop and breakeven position
  management;
- for a finite session, admit broker timestamps to candles, signals and market
  context only inside the half-open interval `[session_start, session_end)`;
- keep a cached snapshot outside that interval available to position
  management, but journal and exclude it from the new session's decision
  pipeline;
- keep REST control snapshots diagnostic;
- bound the transport queue and fail visibly on overflow;
- coordinate candidates by closed M1 minute before applying cross-symbol
  ranking.

The jump and confirmation distance use the largest absolute percentage move
across bid, ask and last, so executable-price anomalies cannot hide behind a
stable last price. Quote-quality decisions retain their rolling reference size,
median change, effective threshold, pending quote timestamp and
confirmation/recovery reason in the trade journal.

Position-only REST fallback uses an independent validator warmed by accepted
WebSocket quotes. A fallback anomaly therefore cannot drive lifecycle handling,
and fallback timestamps cannot advance the canonical WebSocket validator.

## Deliberately unchanged

- scoring thresholds, profiles, top-N and risk constraints are owned by the
  active scoring model rather than the transport layer;
- `LastExecution` remains the strategy and candle price;
- portfolio reconciliation cadence and request semantics;
- historical candle warm-up and gap repair;
- broker-side protective-stop policy;
- global REST request optimisation, which requires a separate inventory study.

Follow-up work is tracked in issues #28 through #32.
