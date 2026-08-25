# Goblin V3 — Inventory / Recoverability architecture

Status: **research/demo only**. V3 is deliberately not production-authorized.

## Why V3 exists

The directional Goblin research program exhausted the current M1/context and eToro quote-flow information without finding a robust economically positive BUY/SELL classifier. External strategy autopsy then identified a different formulation: a Passivbot-style long inventory engine can monetize oscillation without solving Direction first.

V3 therefore pivots from `Direction -> trade` to:

```text
CausalMarketState + PortfolioState
  -> OscillationOpportunity
  -> InitialEntry
  -> InventoryState
  -> RecoverabilityAssessment
  -> Reentry / NoReentry / ReduceRisk
  -> TrailingExit / Unstuck
  -> Economics
  -> BrokerOrderPlan
```

SELL is **not** a mirrored alpha strategy. The only planned SELL sleeve is an aggregate portfolio hedge whose invariant is that it must reduce distance to the configured beta target.

## Design contracts

1. **One planner for replay and live.** Strategy code emits deterministic intents; environments own fills and broker submission.
2. **Inventory is the economic object.** One Goblin inventory may aggregate several broker positions/legs.
3. **Fills are truth.** Broker acceptance is not a fill. Inventory accounting changes on confirmed fills only.
4. **Multi-day is normal.** Session close does not imply inventory close.
5. **Hard risk outranks ML.** Recoverability may deny risk; it may never override max fills, symbol exposure, portfolio exposure, or catastrophic controls.
6. **Gross and broker-net economics stay separate.** Demo research may execute a gross-positive hypothesis that a temporary broker makes net-negative, but logs must say so explicitly.
7. **Recoverability ranking is distinct from calibrated probability.** The current model is useful as a ranking signal; calibration is not assumed.
8. **No legacy shadow.** V1 compatibility is not a runtime goal while Goblin remains demo-only.
9. **Broker capability is explicit.** eToro currently closes by position id, so partial aggregate inventory exits require deterministic broker-leg translation.
10. **Logs stay under `logs/`.** New events should be causal, versioned, compact, and sufficient to reconstruct decisions.

## Point M — replay parity gate

Point M is not a profitability optimization. It is a **translation-integrity test** proving that the V3 replay engine can reproduce the frozen screening mechanics that motivated the pivot.

Canonical local reconstruction used for the gate:

- 934,557 deduplicated M1 candles;
- 115 equities;
- 18 sessions;
- 2026-07-29 07:00 UTC through 2026-08-21 19:58 UTC.

Frozen execution assumptions:

- no-volume Forager proxy;
- 22.5 bp cost per fill (45 bp simple round trip);
- long-only inventory;
- positions may carry across sessions;
- final open inventories force-realized at end of corpus.

### Exact max7 parity

V3 reproduced the frozen max7 reference to floating-point precision:

- return: `+0.8575287996%`;
- 236 entries;
- 311 exits;
- 131 cycles;
- 3 final force-closes;
- max gross exposure `22.2406697%`;
- max single-symbol exposure `12.4078160%`;
- max drawdown `0.8095739%`;
- gross realized P&L `$1,389.404658`;
- fill costs `$531.875858`.

### Exact RR5 parity

The bounded max-five-entry profile also reproduces the earlier risk-reduced screening:

- return about `+0.525483%` after the same 45 bp stress;
- max drawdown about `0.280617%`;
- max gross exposure about `13.2030%`.

### RR5 hard-risk boundary

The Passivbot wallet-exposure limit used by dynamic spacing is **strategy geometry**, not a Goblin safety cap. V3 keeps `effective_wallet_exposure_limit_pct` frozen independently from hard symbol/portfolio caps. Hard caps crop order size to remaining budget; they do not silently change re-entry/close thresholds.

### Recoverability caveat

The historical `~+0.687%` RR5+Recoverability screening number was never materialized as a standalone ledger. It is therefore **not** a golden parity target. V3 must not tune implementation details merely to hit that number.

The exact replay should instead report every frozen Recoverability policy independently, with its model artifact, feature manifest, gate semantics, exposure, drawdown, fills and costs. If a policy fails to improve RR5 under exact replay, the correct result is to reject or demote that policy—not to bend the replay.

That is now the active decision: the exact engine could not reproduce the historical `~+0.687%` figure. The strongest static gate with the historical all-feature artifact was only about `+0.539%`, a small gain over RR5, while the better chronological OOS no-volume artifact (`AUC ~0.615`) reduced exact replay P&L under the tested gates. **Recoverability authority is therefore disabled by default.** The scorer remains versioned and journalable so future prospective policies can be tested without changing strategy mechanics.

## Current model artifact

`RECOVERABILITY_LONG_LOGIT_V1_20260825` uses the no-volume/activity feature set that improved the chronological 17–21 August AUC to about `0.615`. It is evaluated with a lightweight versioned linear artifact; scikit-learn is not required in execution. The default RR5 policy observes/logs it but does not grant or deny re-entry authority.

## eToro execution boundary

The research replay can close an exact percentage of aggregate inventory units. eToro's public trading surface closes by position id, so live/demo execution cannot assume that an arbitrary 84% aggregate close maps to one broker request. `WholeLegCloseAllocator` makes that translation explicit and measurable instead of hiding it inside strategy math.

This broker divergence must be measured separately from Point M before any production discussion.
