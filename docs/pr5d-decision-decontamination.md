# PR5-D — Decision decontamination

## Goal

Make homogeneous demo runs interpretable without changing fixed TP/SL profiles,
selection thresholds, top-N limits, RiskManager constraints or managed exits.
PR5-D reduces unproven influences in the live decision while retaining their raw
evidence for counterfactual analysis.

## Live score

```text
final score
= directional score
+ compressed context [-4, +4]
+ M5 READY [-3, +3]
+ TP feasibility [-15, +15]
```

## Implemented contract

- `market_context_score_v3`: full raw context retained; live contribution is
  `clip(raw × 0.25, -4, +4)`; freshness no longer gates relative strength.
- `multi_timeframe_score_v2`: M5 READY contributes `+3/-3`; M15, M30 and H1
  remain diagnostic shadow evidence.
- `tp_feasibility_score_v4`: TP/ATR 35%, TP/momentum 30%, costs/TP 35%;
  freshness contributes 0% live.
- `heuristic_v5`: freshness, M15 and M30 are removed from the live probability
  inputs; base rates and slope remain unchanged during observation.
- live top-N ranking uses exact score, then TP feasibility, then directional
  score; calibrated EV remains journalled but does not rank candidates.
- `entry_router_v6`: `WAIT_FOR_RETEST` requires usable structure and
  `extension_percent / effective_TP >= 0.20`.

## Unchanged during the observation cohort

- US profile `1.20 / 0.70 / 60`, threshold 115, top 2;
- EU BUY `2.00 / 1.20 / 180`;
- EU SELL `1.00 / 0.70 / 75`;
- EU threshold 110 and top 1;
- crypto support;
- hard economics, session horizon and risk constraints;
- pending-entry semantics;
- net breakeven, trailing, stale and force-close behavior;
- demo-only operation.

No score, weight, threshold, profile, ranking, risk or pending-rule tuning may be
introduced between the homogeneous collection days. Operational and
observability-only corrections are allowed when they do not alter candidate
selection or trade management.

## Analysis objectives

The post-run analysis must use every evaluated candidate, not only executed
trades, and label its subsequent market path from retained market and candle
streams.

### Score and outcome monotonicity

Determine whether increasing final score, directional score and individual score
contributions correspond to improving outcomes:

- TP before SL frequency;
- MFE and MAE;
- time to TP, SL or expiry;
- net expectancy proxy;
- timeout, stale and force-close frequency.

### Ranking quality

For each decision window containing several candidates, compare the selected
candidate with candidates rejected by threshold or top-N. Measure whether the
live ranking chose the best subsequent path and identify counterfactual ranking
errors.

### TP-feasibility discrimination

Measure `TP_FIRST`, `SL_FIRST` and `NEITHER` outcomes by TP-feasibility bucket
and by its three live components:

- TP/ATR;
- TP/momentum;
- costs/TP.

### Multi-timeframe value

Measure the live marginal value of M5 READY alignment and disagreement. Evaluate
M15, M30 and H1 only as diagnostic shadow features and determine whether any of
them demonstrates stable incremental value before considering a future live
role.

### Market-context value

Separate raw benchmark, breadth, sector participation, relative strength and
regime evidence from the compressed live contribution. Determine whether
context improves ranking modestly, adds no information or remains harmful for
specific profiles and sides.

### Freshness as diagnostic evidence

Freshness remains journalled but has no live effect. Test whether it represents
exhaustion, trend maturity, a non-linear optimum or profile-specific behavior.

### Probability calibration and discrimination

Compare predicted TP-before-SL probability with observed TP-first frequency:

- globally;
- by named profile and side;
- by asset class;
- for calibration and ranking discrimination separately.

A probability may rank candidates usefully while remaining badly calibrated,
or appear calibrated globally while failing to discriminate candidates.

### Pending and structural retest outcomes

Compare immediate candidates, pending registrations, confirmations,
invalidations and expirations. Measure whether retests avoid bad entries, miss
good trends, improve TP-first probability and justify the current
`extension_to_tp_ratio >= 0.20` threshold.

## Observation cohort

Collect three complete PR5-D runs with no decision-policy tuning between runs.
The first complete run starting on 2026-07-22 is admissible as collection day 1.
It must not be excluded merely because of local observability defects when the
raw event streams remain intact and the defects do not change strategy behavior.

Known annotations for the 2026-07-22 run:

- the initial European subscription window was incomplete while the instrument
  cache was populated;
- three cooldown-blocked candidates lacked standalone `entry_decision` records;
- the partial summary exposed an obsolete schema number;
- confirmed closes reported `confirmation_checks=0` because the confirming
  portfolio absence was not counted;
- close PnL uses the observed trigger price rather than a broker exit fill.

These annotations constrain only the affected fields or windows. They do not
invalidate the run for PR5-D analysis. Raw `trades.jsonl.gz`, `candles.jsonl.gz`,
`market.jsonl.gz` and the run manifest remain the canonical evidence.

## Analysis method

- verify run identity, commit, manifest and journal continuity first;
- reconstruct candidate lineage with `candidate_id`, `origin_candidate_id` and
  `pending_entry_id`;
- label future paths without lookahead at decision time;
- preserve selected and rejected candidates in the same comparison universe;
- stratify by asset class, profile, side, session and market regime;
- report sample sizes and uncertainty; do not tune from isolated anecdotes;
- distinguish operational failures from decision-model failures;
- avoid parameter changes until the complete three-run cohort is analyzed.

## Roadmap

The canonical roadmap remains:

1. **PR4 — historical truth and analysis readiness**: deterministic closed-data
   evidence, candidate identity, lineage, MTF maturity and explicit decision
   stages.
2. **PR5 — audit scores and features**: de-harden probabilistic signals,
   decontaminate live decisions, collect homogeneous evidence and determine
   which scores, contexts, timeframes and retests have real predictive value.
3. **PR5-E — monotone outcome probability challenger**: freeze the two-stage
   `P_TOUCH × P_DIRECTION` model from the complete PR5-D cohort, replace the
   PR5-D runtime policy and validate it on an equity-only watchlist.
4. **PR6 — externally validated probability and expected value**: use new
   untouched sessions to decide whether calibration, direction edge and
   abstention are stable enough for broader deployment.

PR5-E is documented in
`docs/pr5e-monotone-outcome-score.md`. PR6 must not be designed from one day or
from executed trades alone.

## Non-goals

No profile recalibration, EU BUY suspension, dynamic TP, managed-stop change,
production-risk implementation or live-capital activation belongs in this
observability correction.
