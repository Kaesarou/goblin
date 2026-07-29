# Segmented direction probability V2

## Purpose

V2 keeps the frozen activity probability `P_TOUCH` and replaces the shared equity direction model with market-and-side-specific logistic models. Runtime selection uses only one policy: a candidate must exceed its conditional break-even probability by at least five percentage points before top-N is applied.

## Cohorts

- Activity: 1,958 candidates from 22–24 July 2026, unchanged from V1.
- Direction: all 3,527 candidates from 22, 23, 24, 27 and 28 July 2026.
- Decisive direction labels: 475 `TP_FIRST`, 1,072 `SL_FIRST`.
- `NEITHER` remains part of `P_TOUCH`, not `P_DIRECTION` training.

Both input CSV hashes are hard-coded in `scripts/fit_segmented_outcome_probability_model.py`. Another cohort requires another model version.

## Segments

- `EQUITY_EU_BUY`: core movement and context.
- `EQUITY_EU_SELL`: multi-timeframe dynamics.
- `EQUITY_US_BUY`: plan geometry and feasibility.
- `EQUITY_US_SELL`: multi-timeframe dynamics.
- `CRYPTO_BUY`: explicit provisional copy of US BUY.
- `CRYPTO_SELL`: explicit provisional copy of US SELL.

Crypto is not silently routed to an equity model. Its artifact entries expose the transfer source and zero crypto training rows.

## Calibration

The frozen coefficient artifact remains unchanged. Runtime calibration is defined by the explicit, versioned policy artifact `outcome_probability_calibration_v2.json`, which enumerates all six supported segments. There is no implicit default or fallback.

Five segments retain the original calibration:

```text
P_DIRECTION = 0.5 × P_DIRECTION_RAW + 0.5 × SEGMENT_PRIOR
```

`EQUITY_EU_SELL` uses:

```text
P_DIRECTION_EU_SELL
= 0.6 × P_DIRECTION_RAW + 0.4 × SEGMENT_PRIOR
```

The EU SELL prior is approximately 28.82%. Under 50/50 shrinkage, the maximum possible final probability was approximately 64.41%. Requiring five additional percentage points above the usual EU SELL break-even made almost every candidate mathematically ineligible, even with a perfect raw prediction. The 60/40 policy raises the ceiling to approximately 71.53% while retaining material shrinkage.

This correction does not change the fitted coefficients, training cohort, features, `P_TOUCH`, direction margin or any other segment calibration.

Then:

```text
P_TP = P_TOUCH × P_DIRECTION
P_SL = P_TOUCH × (1 - P_DIRECTION)
P_NEITHER = 1 - P_TOUCH
```

## Selection contract

```text
direction_edge = P_DIRECTION - direction_break_even
required direction_edge >= 0.05
```

The edge gate is applied before top-N. Eligible candidates rank by edge, then `P_TP`, then candidate ID. There are no minimum-`P_TP` or maximum-`P_TOUCH` gates and no backfill problem because ineligible candidates never enter the top-N ranking.

## Validation

Cross-day direction validation: AUC 0.643, Brier 0.202. A stricter historical/external split yielded approximately AUC 0.588 and Brier 0.217. Those figures describe the frozen coefficients. The EU SELL calibration correction fixes selection reachability and must be evaluated on new sessions.

## Observability

Every outcome estimate retains:

- `P_TOUCH`;
- raw direction probability;
- segment prior and effective calibration weights;
- final `P_DIRECTION`;
- `P_TP`, `P_SL`, `P_NEITHER`;
- break-even and direction edge;
- segment, feature family, training status and transfer source;
- exact activity and direction feature maps.

The run manifest records the effective weights for every segment, while the code fingerprint and policy artifact hash identify the exact calibration policy used.
