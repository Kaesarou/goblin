# PR5-E — Monotone outcome score

## Goal

Replace the PR5-D technical score with an interpretable probability scale:

```text
probability_score = 200 × P(TP_FIRST)
```

The score is monotone by definition. If candidate A has score 40 and candidate
B has score 20, the model estimates respectively 20% and 10% probability of
reaching the effective TP before the effective SL.

PR5-E does not claim that the strategy is profitable. It replaces PR5-D for
the next ordered session and turns that session into an external validation of
a frozen challenger.

## Evidence cohort

The model contract is frozen from the homogeneous PR5-D runs of 22, 23 and
24 July 2026:

| Item | Value |
|---|---:|
| Labelled candidates | 1,980 |
| Usable model rows | 1,958 |
| Usable `TP_FIRST` | 231 |
| Usable `SL_FIRST` | 571 |
| Usable `NEITHER` | 1,156 |
| Asset classes | US and European equities |
| Dataset SHA-256 | `4608e799936ac007292dcb5cfc1894880893e3f9544d0b7a33096d23b5bb8290` |

The cohort contains selected and rejected candidates. It is not limited to
orders actually sent.

Crypto is absent. The runtime records that a crypto prediction is outside the
training domain but does not reject the asset class. Crypto is simply omitted
from the watchlist of the next validation runs.

## Why two probabilities

The PR5-D analysis showed that feasibility could detect whether a path would
be active, but barely distinguished the economically favourable direction.
PR5-E therefore models two questions separately.

### `P_TOUCH`

```text
P_TOUCH = P(TP_FIRST or SL_FIRST)
```

It estimates whether either barrier will be reached before the observation
horizon. Its inputs describe activity and reachability:

- effective TP and SL distances;
- costs, net reward/risk and profile horizon;
- time and progress in the trading session;
- base and directional evidence;
- TP feasibility and its direct components;
- movement consumed, freshness and extension;
- asset, side and named profile;
- M5/M15/M30/H1 maturity and alignment.

High `P_TOUCH` does not mean a good trade. It means a decisive move is more
likely.

### `P_DIRECTION`

```text
P_DIRECTION = P(TP_FIRST | TP_FIRST or SL_FIRST)
```

It estimates which barrier is more likely to be first, conditional on a
decisive path. Its deliberately smaller feature set is:

- progress in the session;
- directional relative strength;
- sector participation;
- weak benchmark momentum evidence;
- M5 maturity;
- interaction between M5 alignment and movement already consumed;
- asset class, side and named profile.

Example:

```text
P_TOUCH = 40%
P_DIRECTION = 35%

P_TP      = 40% × 35% = 14%
P_SL      = 40% × 65% = 26%
P_NEITHER = 60%
```

`P_DIRECTION = 35%` is not a 35% overall chance of TP. It says that, among
paths expected to hit a barrier, only 35% are expected to hit TP first.

## Three exclusive outcomes

The complete outcome distribution is:

```text
P_TP      = P_TOUCH × P_DIRECTION
P_SL      = P_TOUCH × (1 - P_DIRECTION)
P_NEITHER = 1 - P_TOUCH
```

The runtime verifies this through the frozen model contract and journals all
three values. The live score is:

```text
probability_score = round(200 × P_TP, 4)
```

The scale is not capped at the historical maximum. The largest
leave-one-day-out prediction in the cohort was 41.5%, hence score 83. Future
data may legitimately produce a higher or lower value.

## Conditional economic comparison

The pre-existing hard economics guard remains unchanged:

> If the TP is reached, does the position leave at least the configured
> minimum net profit after opening cost, closing cost and spread?

PR5-E adds a different ranking quantity among decisive paths:

```text
net_gain_at_tp = expected_net_profit_percent
net_loss_at_sl = effective_sl_percent + estimated_cost_percent

direction_break_even
= net_loss_at_sl / (net_gain_at_tp + net_loss_at_sl)

direction_edge
= P_DIRECTION - direction_break_even
```

If `P_DIRECTION` is below `direction_break_even`, the candidate has a negative
conditional margin even when its reached TP would individually cover costs.
This margin ranks candidates; it does not replace the hard TP-profit guard.

## Frozen model and reproducibility

Training uses scikit-learn only in the offline calibration script:

```bash
python -m pip install -e ".[calibration]"
python -m scripts.fit_pr5e_probability_model \
  path/to/candidates.csv \
  app/execution/scoring/models/pr5e_outcome_probability_v1.json
```

The live runtime has no scikit-learn dependency. It loads the exported
coefficients and applies deterministic preprocessing and logistic inference in
pure Python.

The artifact records:

- model version `outcome_probability_v1`;
- feature contract `pr5e_features_v1`;
- dataset hash and cohort days;
- training row and outcome counts;
- leave-one-day-out validation metrics.

The full-fit artifact is used only after the architecture and hyperparameters
were chosen with leave-one-day-out predictions.

## Validation results

Each collection day was predicted using a model trained on the other two:

| Metric | PR5-D heuristic | PR5-E challenger |
|---|---:|---:|
| Mean predicted `TP_FIRST` | 34.5% | 11.2% |
| Observed `TP_FIRST` | 11.8% | 11.8% |
| TP/rest AUC | 0.509 | 0.667 |
| Brier score | 0.168 | 0.101 |
| Expected calibration error | 22.7% | 1.8% |

Pooled out-of-day probability bins were:

| Predicted TP | Observed TP |
|---:|---:|
| 3.0% | 2.8% |
| 7.1% | 8.2% |
| 10.0% | 12.2% |
| 13.6% | 14.8% |
| 22.5% | 20.9% |

`P_DIRECTION` is the less mature part. Its TP/SL AUC was 0.575, with bootstrap
uncertainty roughly 0.519–0.629. PR5-E must therefore expose it separately and
must not present direction ranking as solved.

## Initial live policy

Within each asset class:

1. apply entry route, readiness, structural feasibility and hard economics;
2. rank by `direction_edge`;
3. break ties with directional score, then deterministic candidate ID;
4. keep top 1 Europe or top 1 US; crypto remains top 2 but is omitted from the
   validation watchlist because it is outside the training domain;
5. require `P_TP >= 12.5%`;
6. require `P_TOUCH < 40%`;
7. do not backfill a slot rejected by either probability gate.

The top-N-before-gates order is intentional. The strict policy retained 28
candidates in the leave-one-day-out replay: 8 `TP_FIRST`, 2 `SL_FIRST` and
18 `NEITHER`, with a mean counterfactual net result of approximately +0.150%.

This policy was selected after observing the three-day cohort. It is therefore
a post-hoc hypothesis, not independent profitability evidence. The next five
complete sessions must keep the model, thresholds, top-N and order of
operations frozen. Intraday tuning would invalidate the external test.

The `P_TOUCH < 40%` gate is an empirical abstention rule from this challenger,
not a universal claim that quiet markets are better. It must be re-evaluated
after external sessions.

## PR5-D retirement

PR5-D demonstrated no profitable policy on the completed cohort. PR5-E
therefore removes its combined score, thresholds, selector and shadow events
instead of preserving a compatibility path. The underlying raw evidence
remains available for analysis.

## Journalling contract

Run-manifest schema 10 and summary schema 12 expose:

- `probability_score`;
- `P_TOUCH`, `P_DIRECTION`, `P_TP`, `P_SL`, `P_NEITHER`;
- conditional break-even and direction edge;
- model and feature-contract versions;
- frozen feature values and missing features;
- PR5-E live selection outcome;
- whether each prediction is inside the frozen training domain.

## External validation

The next sessions must be evaluated without refitting during the run. Required
checks are:

- TP frequency by fixed probability bucket;
- monotonicity pooled and by collection day;
- calibration error and Brier score;
- TP/rest and TP/SL discrimination;
- order count, TP/SL/NEITHER mix and net result;
- outside-training-domain and missing-feature rates;
- stability by profile and side;
- replay of the original broader 10% / 50% policy from journalled evidence for
  comparison, without keeping a second live selector.

One positive session is not sufficient to validate the model. A failed
monotonicity or a large calibration drift is evidence to investigate, not a
reason to tune coefficients intraday.

## Session-boundary runtime safety

The validation run must not restart when the broker sends a cached quote from
just before a finite session opens. A snapshot with a broker timestamp outside
`[session_start, session_end)` remains available to TP/SL and open-position
management, but is excluded from:

- strategy snapshots;
- M1 candle construction and downstream timeframes;
- benchmark, breadth and sector context.

The runtime journals the exclusion as `market_data_event_ignored`, including
the snapshot timestamp and session bounds. The multi-timeframe exception for a
candle preceding its session anchor remains in place as an invariant.
