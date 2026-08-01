# Backtesting

Backtesting reuses live contracts rather than maintaining a second position
simulator.

- `position_replay.py` advances one typed position through the live
  `PositionTracker` and is the canonical label-generation entry point.
- `stateful_managed_replay.py` reconstructs chronological candidates, cooldowns,
  top-N, portfolio capacity and managed exits for a cohort.
- `journal_deserialization.py` restores current typed candidates and market
  snapshots from analysis journals.
- `scripts/replay_breakeven_profiles.py` reads standalone or multipart run
  archives and compares explicit breakeven profiles.

Historical broker close fills are never invented. When unavailable, replay uses
side-aware executable estimates and reports that provenance. See
`docs/position-lifecycle-v2.md` and `docs/breakeven-replay-v1.md`.
