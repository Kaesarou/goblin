# Managed Edge V1

`MANAGED_EDGE_V1` replaces the universal fixed-barrier direction-edge gate in
candidate selection. It is enabled for the demo portfolio, not shadowed.

## Decision target

The selector estimates three quantities for every economically admissible
candidate:

- `P_PROTECTION`: probability that the current managed stop policy can protect
  the position before the initial loss is realised;
- `P_MANAGED_POSITIVE`: probability that the exact managed lifecycle closes at
  least 5 basis points net positive;
- `EXPECTED_MANAGED_NET_RETURN`: expected net return after stop loss,
  breakeven, trailing, stale and session-close outcomes.

A candidate is eligible only when both probabilities exceed their frozen
segment priors and expected managed return exceeds the frozen 5 bp safety
margin. Eligible candidates are ranked by managed edge, then protection,
managed-positive probability, and finally the retained direction edge.

`P_TOUCH` and `P_DIRECTION` remain calculated, journalled and used as model
features. They are no longer universal selection gates.

## Time treatment

There is no opening, middle-session or closing route. `session_progress` and
its squared term are continuous numeric features of the same model used for
all candidates. The journal records the separate contribution of those two
terms and explicitly records `hard_time_route=false`.

## Frozen artefact

The V1 artefact was fitted on entry-ready candidates from 22, 23, 24, 27 and
28 July 2026. Labels replay the then-current managed lifecycle at one-minute
resolution: fixed TP/SL, net breakeven, trailing stop, stale exit, session
close and estimated costs. The positive label requires at least 5 bp net.

The probability estimates are shrunk 50% toward their segment prior. Models
are trained separately for EU/US and BUY/SELL. Crypto uses an explicit,
journalled transfer from the matching US-side model; this is not a hidden
fallback.

This is an experimental demo policy. The frozen model is intentionally not
retuned after each session. Position sizing, RiskManager limits, broker
execution, cooldowns, and all exit rules are unchanged.
