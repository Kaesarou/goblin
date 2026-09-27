from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.market.multi_timeframe import MultiTimeframeContext
from app.market.timeframes import TimeframeDirection, TimeframeMaturity

MULTI_TIMEFRAME_SCORER_VERSION = 'multi_timeframe_score_v2'


@dataclass(frozen=True)
class MultiTimeframeScore:
    score: float
    components: dict[str, float]
    diagnostics: dict[str, Any]
    model_version: str = MULTI_TIMEFRAME_SCORER_VERSION


_READY_WEIGHTS = {
    'm5': 3.0,
    'm15': 0.0,
    'm30': 0.0,
}
_MAXIMUM_ABSOLUTE_SCORE = 3.0


def score_multi_timeframe(
    *,
    context: MultiTimeframeContext | None,
    side: str,
) -> MultiTimeframeScore:
    direction = _side_direction(side)
    components = {timeframe: 0.0 for timeframe in _READY_WEIGHTS}
    if context is None or direction == 0:
        return MultiTimeframeScore(
            score=0.0,
            components=components,
            diagnostics={'available': False, 'side': side},
        )

    ready_timeframes: list[str] = []
    for timeframe, weight in _READY_WEIGHTS.items():
        feature = context.features_by_timeframe.get(timeframe)
        maturity = context.maturity_by_timeframe.get(timeframe)
        if feature is None or maturity != TimeframeMaturity.READY:
            continue
        ready_timeframes.append(timeframe)
        if weight == 0.0:
            continue
        components[timeframe] = _direction_component(
            feature.direction,
            direction,
            weight,
        )

    total = _clamp(
        sum(components.values()),
        -_MAXIMUM_ABSOLUTE_SCORE,
        _MAXIMUM_ABSOLUTE_SCORE,
    )
    return MultiTimeframeScore(
        score=round(total, 4),
        components={
            timeframe: round(value, 4)
            for timeframe, value in components.items()
        },
        diagnostics={
            'available': bool(ready_timeframes),
            'side': side.strip().upper(),
            'ready_timeframes': ready_timeframes,
            'ready_alignment': context.ready_alignment.value,
            'live_timeframes': ['m5'],
            'diagnostic_timeframes': ['m15', 'm30', 'h1'],
            'provisional_timeframes_ignored': [
                timeframe
                for timeframe, maturity in context.maturity_by_timeframe.items()
                if maturity == TimeframeMaturity.PROVISIONAL
            ],
        },
    )


def _side_direction(side: str) -> int:
    normalized = side.strip().upper()
    if normalized == 'BUY':
        return 1
    if normalized == 'SELL':
        return -1
    return 0


def _direction_component(
    timeframe_direction: TimeframeDirection,
    trade_direction: int,
    weight: float,
) -> float:
    if timeframe_direction == TimeframeDirection.UP:
        observed_direction = 1
    elif timeframe_direction == TimeframeDirection.DOWN:
        observed_direction = -1
    else:
        return 0.0
    return weight if observed_direction == trade_direction else -weight


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
