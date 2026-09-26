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
2. Broker boundaries: account preflight, durable external-activity policy,
   equity provenance and structured open rejection are generic contracts.
   eToro payload parsing stays in its adapter; caching forwards safety reads
   without TTL reuse. The V3 execution/decision core no longer imports a concrete
   broker (the deployment manifest retains its eToro provenance). Contract tests
   cover another broker, nested caches, rejection, unavailable preflight and
   unchanged paper/demo/live metadata.
3. Retire 25 unreachable V1 live modules (runtime, executor, old strategy,
   manifest writer and position/pending-close store writers). Keep historical
   scoring, candidate selection, position lifecycle, replay, deserialization,
   calibration and research dependencies. V3 legacy-SQLite startup guards remain
   intact. Move research non-interference/failure tests onto the actual V3 runtime.
   Correct the README so historical strategy descriptions are not presented as
   the active policy.
4. eToro open submission: share one `EtoroClient.open_position` pipeline. The
   resilient adapter now supplies only three policy hooks: uncertain
   confirmation translation, ambiguous execution translation and suspicious
   account-notional reconciliation. Request payloads, identifiers, position
   metadata and returned `OpenPositionResult` remain unchanged. Validation:
   41 focused broker/V3 tests and the full 928-test suite pass.
5. Replay cleanup: remove an unused symbol-id slice and an obsolete duplicate
   trailing-extrema helper from the V3 Point-M replay. The active
   `_ext_values` implementation is unchanged; Point-M behavior is preserved.
   Validation: the full 928-test suite passes.
6. eToro payload contracts: replace the old tuple-based key mappers and
   recursive envelope/case fallbacks with the documented endpoint fields. The
   adapter now reads `orderId`/`referenceId`, `positionExecutions[].positionId`,
   `openingData.avgPrice`/`units`, `orderForClose.positionID`/`statusID`,
   `clientPortfolio.positions[].positionID`/`units`, aggregate
   `accountTotals.accountTotalValue`, instrument `items[].internalInstrumentId`,
   REST rates `instrumentID`/`Bid`/`Ask`/`Last`, P&L root `ordersForOpen`/`orders`,
   and the canonical WebSocket `Bid`/`Ask`/`LastExecution`/`Date`/
   `PriceRateID` fields. Unused generic scalar/string mapper modules and their
   compatibility tests were removed. Validation: the full 907-test suite
   passes.
7. Application-wide orphan cleanup: remove the unused legacy
   `BrokerOrderResult` model and the unused `TradeCandidateScorer` façade.
   Historical signal scoring remains available through the exercised scorer
   modules and candidate-ranking path. Validation: the full 907-test suite
   remains green.

## Retirement evidence

The dependency audit parsed absolute and relative Python imports, including
package imports, starting from `app.main`, `app.runtime.restart_guard`, all
scripts, all V3 modules, all backtesting modules and all research modules.
The 25 removed application modules had no reachable consumer; their remaining
consumers were only V1-specific tests. CLI entrypoints in Docker/Compose, shell
scripts, workflows and documentation were also checked. No dynamic imports or
plugin entrypoints target these modules.

Test accounting: 1,034 passing after checkpoint 2 minus the retired V1-only and
duplicate compatibility coverage through checkpoint 5, then the obsolete
payload-key mapper coverage in checkpoint 6 = **907 passing**. The retained
tests exercise the documented eToro shapes and assert that unknown/legacy
shapes fail closed. Four research tests are retained and now exercise V3
instead of a fabricated V1 runtime. No active V3 safety/strategy test was
removed. Removed code and tests remain recoverable from the baseline commit
above.

## Validation

Run `.venv/bin/python -m pytest -q` after each checkpoint. New broker boundaries
need contract tests; payload parsing must retain fail-closed behavior. The test
suite runs without broker credentials or network order submission. Publish each
validated checkpoint on the PR so progress is recoverable independently of the
workspace lifetime.
