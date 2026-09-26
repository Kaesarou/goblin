# Experimental application simplification

Target: `alpaca-experimental`. Work branch: `refactor/simplify-goblin-for-multibroker`.
Baseline: `a3dac175bfc5171cde621a3a914d05b48f4aa0df` (1,026 tests passing).

## Constraints

- Preserve `INVENTORY_RR5_ETORO5_V1`: signals, thresholds, sizing, inventory
  lifecycle, exits, trailing, fees and decision-window timing are unchanged.
- Preserve durable BUY reservations, unknown-outcome halts, account preflight,
  external-account acknowledgment, quantitative reconciliation and close retries.
- No Alpaca implementation, deployment, merge to develop/main, branch cleanup,
  or live broker calls in this refactor.
- Keep replay/research tools and historical data formats usable. Delete code
  only after checking production, scripts, research and test dependencies.

## Checkpoints

1. Consolidate the V3 executor: merge the guarded implementation and its base
   into `app/v3/live_execution.py`. Remove the `sys.modules` replacement and the
   unreachable duplicate unknown-error classification. Existing public imports
   and clock monkeypatches work directly. Validation: 1,026 tests pass.

## Validation

Run `.venv/bin/python -m pytest -q` after each checkpoint. New broker boundaries
need contract tests; payload parsing must retain fail-closed behavior. The test
suite runs without broker credentials or network order submission. Publish each
validated checkpoint on the PR so progress is recoverable independently of the
workspace lifetime.
