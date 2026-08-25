from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from app.v3.features import OnlineFeatureSnapshot
from app.v3.models import MarketState


@dataclass(frozen=True)
class ForagerCandidate:
    market: MarketState
    forager_volatility: float
    ema_readiness: float
    score: float = 0.0


class NoVolumeForager:
    """Frozen Point-M no-volume selector.

    The volume/activity term is intentionally absent. Volatility keeps 61/82 of
    the normalized weight and EMA readiness 21/82, exactly matching the screening
    ablation that underpins RR5.
    """

    volatility_weight = 0.61 / 0.82
    readiness_weight = 0.21 / 0.82

    def rank(
        self,
        candidates: Iterable[ForagerCandidate],
        *,
        limit: int,
    ) -> tuple[ForagerCandidate, ...]:
        eligible = [
            candidate
            for candidate in candidates
            if candidate.market.entry_allowed
            and candidate.market.quality_ok
            and math.isfinite(candidate.forager_volatility)
            and math.isfinite(candidate.ema_readiness)
        ]
        if limit <= 0 or not eligible:
            return ()
        # Stable symbol order makes ties deterministic and matches Point-M's
        # timestamp+symbol ordering before stable argsort.
        eligible.sort(key=lambda item: item.market.symbol)
        vol_scores = _normalize_high([item.forager_volatility for item in eligible])
        readiness_scores = _normalize_low([item.ema_readiness for item in eligible])
        scored = [
            ForagerCandidate(
                market=item.market,
                forager_volatility=item.forager_volatility,
                ema_readiness=item.ema_readiness,
                score=(
                    self.volatility_weight * vol_score
                    + self.readiness_weight * readiness_score
                ),
            )
            for item, vol_score, readiness_score in zip(
                eligible,
                vol_scores,
                readiness_scores,
                strict=True,
            )
        ]
        scored.sort(key=lambda item: (-item.score, item.market.symbol))
        return tuple(scored[:limit])


def _normalize_high(values: list[float]) -> list[float]:
    minimum = min(values)
    maximum = max(values)
    if maximum - minimum <= 1e-15:
        return [1.0] * len(values)
    return [(value - minimum) / (maximum - minimum) for value in values]


def _normalize_low(values: list[float]) -> list[float]:
    minimum = min(values)
    maximum = max(values)
    if maximum - minimum <= 1e-15:
        return [1.0] * len(values)
    return [(maximum - value) / (maximum - minimum) for value in values]
