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

This does not make the time contribution causal. On the replay cohort, the mean
contribution among selected US SELL trades is approximately +0.118 percentage
point and reaches +0.286. The feature pair is held unchanged by the breakeven
experiment and remains a known small-sample overfitting risk to test out of sample.

## Frozen artefact

The V1 artefact was fitted on entry-ready candidates from 22, 23, 24, 27 and
28 July 2026. The frozen artifact’s labels replayed the then-current lifecycle at
one-minute resolution. The positive label requires at least 5 bp net. That artifact
is retained unchanged so this correction does not silently alter selection.

New labels use `shared_position_replay_v1`, which calls the exact live
`executable_position_lifecycle_v2` engine with side-aware prices and the
`executable_fills_explicit_costs_v2` convention. Any retrained managed model must
version its artifact and label provenance; it may not reuse `managed_outcome_v1`.

The probability estimates are shrunk 50% toward their segment prior. Models
are trained separately for EU/US and BUY/SELL. Crypto uses an explicit,
journalled transfer from the matching US-side model; this is not a hidden
fallback.

This is an experimental demo policy. The frozen model is intentionally not
retuned after each session. Position sizing, RiskManager limits, top-N and
non-backfill selection are unchanged. Position economics and cooldown taxonomy
are corrected by Position lifecycle V2 without changing selector coefficients.
