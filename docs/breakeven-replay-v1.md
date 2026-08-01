# Breakeven profile replay V1

## Decision

The live default remains **`corrected_baseline_v1`**:

- EU equities: 0.55%;
- US equities: 0.60%;
- crypto: 0.20%.

`delayed_equity_trigger_v1` is available only through the explicit
`BREAKEVEN_PROFILE` setting and uses EU 0.65% / US 0.70%, with crypto unchanged.
It is not activated by this change.

The replay signal is interesting but not clearly superior. Most of its aggregate
gain comes from the incomplete 28 July session, the complete-day cohort remains
negative under both profiles, exposure increases, and four of the five historical
days are part of the frozen managed model’s training cohort. The only genuinely
post-training day is 31 July.

## Cohort and reconstruction

The stateful replay uses the recorded 22, 23, 24, 27 and 28 July sessions and the
31 July day reconstructed across the pre- and post-deployment runs. All source ZIP
members pass CRC validation. Events are deduplicated by run, sequence, timestamp
and snapshot identity.

Coverage is approximately 07:00–20:00 UTC for every complete day. The 22 July
starts at 07:01 UTC. The 28 July archive ends at 18:44 UTC and is explicitly
incomplete; positions remain open and are marked at the last executable tick.

The candidate universe contains every logged `candidate_economics` candidate plus
the candidates the historical runtime rejected at cooldown before economics.
Candidate objects, market context and MTF context are deserialised into current
typed contracts, then all economics and model outputs are recalculated.

## Fixed comparison contract

Both scenarios use:

- corrected bid/ask executable prices;
- explicit-cost-only post-trade P&L;
- the shared pure lifecycle and canonical close taxonomy;
- corrected cooldown classification and existing duration policy;
- frozen `MANAGED_EDGE_V1` artifacts and selection gates;
- current per-asset top-N and no portfolio backfill;
- actual logged equity, sizing, max positions, max per symbol and max trades per
  session;
- pending candidate lineage, cooldown locks, stale exits and force closes.

Only the EU and US breakeven activation thresholds differ. Buffer, trailing
trigger/distance, TP, SL, stale horizon, sizing, risk and selection are identical.

Entries and exits are replay estimates because historical broker close fills are
not present in the archives. BUY enters at ask and exits at bid; SELL enters at bid
and exits at ask. The spread is therefore present in prices and is not deducted a
second time.

## Aggregate results

| Metric | Baseline 0.55/0.60 | Variant 0.65/0.70 |
|---|---:|---:|
| Net realised | 28.6867 | 36.4048 |
| MTM net | -3.7281 | -3.0488 |
| Realised + MTM | 24.9586 | 33.3560 |
| Trades closed | 44 | 43 |
| Max realised drawdown | 23.1392 | 23.1392 |
| Max intraday equity drawdown | 26.8373 | 26.8373 |
| Average duration | 62.65 min | 68.36 min |
| Capital immobilised | 2,024,850.72 capital-minutes | 2,202,926.88 capital-minutes |
| Peak simultaneous positions | 5 | 5 |
| Capacity blocks | 6 | 6 |
| Cooldown blocks | 28 | 35 |
| Mean MFE | 0.7981% | 0.8357% |
| Mean MAE | 0.4077% | 0.4191% |

The variant adds 7.7181 realised units and 8.3974 including MTM, but consumes
178,076.17 additional capital-minutes (+8.79%) and creates seven more cooldown
blocks. Its worst session-level intraday equity drawdown is unchanged at 26.8373:
this measure includes executable marks on open positions, whereas the realised
drawdown only advances when trades close.

## Daily results

| Date | Status | Baseline realised | Variant realised | Delta | Baseline MTM | Variant MTM |
|---|---|---:|---:|---:|---:|---:|
| 22 Jul | complete | 8.4139 | 8.2499 | -0.1640 | 0 | 0 |
| 23 Jul | complete | -22.7798 | -22.7798 | 0 | 0 | 0 |
| 24 Jul | complete | 13.2256 | 13.2256 | 0 | 0 | 0 |
| 27 Jul | complete | -2.8061 | -2.8061 | 0 | 0 | 0 |
| 28 Jul | incomplete | 33.5117 | 39.9363 | +6.4246 | -3.7281 | -3.0488 |
| 31 Jul | reconstructed complete | -0.8786 | 0.5789 | +1.4575 | 0 | 0 |

Across the five complete days, baseline is -4.8250 and variant is -3.5315: an
improvement of only 1.2935, with both still negative. The 28 July realised delta
alone represents about 83% of the total realised advantage. Including its MTM,
the incomplete-day advantage is 7.1039.

## Robustness

| Segment | Baseline net | Variant net | Delta |
|---|---:|---:|---:|
| EU | 12.8323 | 12.6683 | -0.1640 |
| US | 15.8544 | 23.7365 | +7.8821 |
| BUY | 48.6168 | 57.1179 | +8.5011 |
| SELL | -19.9301 | -20.7131 | -0.7830 |

The observed improvement is concentrated in US BUY. It does not generalise to EU
or SELL. Both realised and marked intraday worst drawdown remain unchanged because
the common 23 July loss is still the cohort maximum. The variant does turn 31 July
from slightly negative to slightly positive, but that single post-training day is
not enough evidence to replace the live default.

## Lifecycle and counterfactuals

| Close reason | Baseline | Variant |
|---|---:|---:|
| Take profit | 6 | 7 |
| Initial stop | 4 | 4 |
| Protected breakeven | 19 | 15 |
| Protected trailing | 9 | 10 |
| Stale | 5 | 5 |
| Session force close | 1 | 2 |

Five baseline breakeven exits later reach TP before the initial SL; three later
reach the initial SL. Under the variant those counts are three and three. Applying
the full stale and force-close lifecycle rather than holding selectively, retaining
all protected baseline exits would add 24.8472 net units; for the variant it would
add 17.5215. Detailed per-position resolution and exit delays are in the generated
JSON report.

This strict result deliberately differs from earlier approximate “about +28 USD”
analysis: the earlier estimate followed selected positions to later TP and used
inconsistent last/spread economics. This replay uses executable bid/ask, current
costs, chronological capacity, stale horizons and mandatory force closes.

## Reproduction artifacts

- `scripts/replay_breakeven_profiles.py` is the executable replay entry point;
- `docs/replays/replay-plan.example.json` documents the source/run layout;
- `docs/replays/breakeven-profile-comparison-v1.json` contains full typed results,
  hashes and provenance;
- `docs/replays/breakeven-profile-comparison-v1.md` is the generated metric report.

The profile decision is not automatic. A future activation requires a larger
out-of-sample cohort showing stable EU/US and BUY/SELL improvement without relying
on an incomplete session or materially higher capacity pressure.
