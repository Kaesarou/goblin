from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.market.models import MarketSnapshot

SPREAD_CONTEXT_VERSION = 'relative_spread_context_v1'
SPREAD_REFERENCE_MAX_OBSERVATIONS = 512
SPREAD_REFERENCE_MIN_OBSERVATIONS = 20
SPREAD_RECENT_OBSERVATIONS = 20


@dataclass(frozen=True)
class SpreadContext:
    version: str
    available: bool
    current_percent: float | None
    reference_median_percent: float | None
    relative_to_median: float | None
    reference_percentile: float | None
    recent_change_ratio: float | None
    reference_observations: int


def build_relative_spread_context(
    *,
    current: MarketSnapshot | None,
    prior_snapshots: list[MarketSnapshot],
) -> SpreadContext:
    current_spread = _spread_percent(current) if current is not None else None
    prior_spreads = [
        spread
        for snapshot in prior_snapshots[-SPREAD_REFERENCE_MAX_OBSERVATIONS:]
        for spread in [_spread_percent(snapshot)]
        if spread is not None
    ]
    observations = len(prior_spreads)
    if (
        current_spread is None
        or observations < SPREAD_REFERENCE_MIN_OBSERVATIONS
    ):
        return _unavailable(current_spread, observations)
    reference_median = _median(prior_spreads)
    if reference_median is None or reference_median <= 0:
        return _unavailable(current_spread, observations)
    recent_median = _median(prior_spreads[-SPREAD_RECENT_OBSERVATIONS:])
    return SpreadContext(
        version=SPREAD_CONTEXT_VERSION,
        available=True,
        current_percent=_round_optional(current_spread),
        reference_median_percent=_round_optional(reference_median),
        relative_to_median=round(current_spread / reference_median, 4),
        reference_percentile=round(
            sum(value <= current_spread for value in prior_spreads)
            / observations,
            4,
        ),
        recent_change_ratio=(
            None
            if recent_median is None
            else round(recent_median / reference_median - 1.0, 4)
        ),
        reference_observations=observations,
    )


def compact_relative_spread_history(
    history: deque[MarketSnapshot],
) -> None:
    """Keep the bounded baseline after a decision window is evaluated.

    Offline candidate batches are journalled after their individual decision
    snapshots.  Quotes received during that delay must remain in the buffer
    without evicting the strictly-prior reference sample.  Callers therefore
    compact only after every candidate in the batch has been evaluated.
    """

    while len(history) > SPREAD_REFERENCE_MAX_OBSERVATIONS:
        history.popleft()


def _unavailable(
    current_spread: float | None,
    observations: int,
) -> SpreadContext:
    return SpreadContext(
        version=SPREAD_CONTEXT_VERSION,
        available=False,
        current_percent=_round_optional(current_spread),
        reference_median_percent=None,
        relative_to_median=None,
        reference_percentile=None,
        recent_change_ratio=None,
        reference_observations=observations,
    )


def _spread_percent(snapshot: MarketSnapshot) -> float | None:
    midpoint = (snapshot.bid + snapshot.ask) / 2
    if midpoint <= 0:
        return None
    return ((snapshot.ask - snapshot.bid) / midpoint) * 100


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
