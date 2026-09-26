from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.instruments.models import AssetClass
from app.market.market_context import CandidateMarketContext

MARKET_CONTEXT_SCORER_VERSION = 'market_context_score_v3'


@dataclass(frozen=True)
class MarketContextScoreConfig:
    benchmark_session_weight: float
    benchmark_momentum_weight: float
    breadth_weight: float
    sector_weight: float
    relative_strength_weight: float
    benchmark_session_scale_percent: float
    benchmark_momentum_scale_percent: float
    relative_strength_scale_percent: float
    maximum_absolute_raw_score: float = 15.0
    effective_score_scale: float = 0.25
    maximum_absolute_effective_score: float = 4.0
    favorable_background_relative_bonus: float = 4.0
    opposed_background_excess_bonus: float = 2.0
    maximum_negative_relative_adjustment: float = 8.0


@dataclass(frozen=True)
class MarketContextScore:
    score: float
    components: dict[str, float]
    diagnostics: dict[str, Any]
    model_version: str = MARKET_CONTEXT_SCORER_VERSION


_CONFIG_BY_ASSET_CLASS = {
    AssetClass.EQUITY_US: MarketContextScoreConfig(
        benchmark_session_weight=3.0,
        benchmark_momentum_weight=2.0,
        breadth_weight=3.0,
        sector_weight=2.0,
        relative_strength_weight=12.0,
        benchmark_session_scale_percent=1.0,
        benchmark_momentum_scale_percent=0.50,
        relative_strength_scale_percent=2.0,
    ),
    AssetClass.EQUITY_EU: MarketContextScoreConfig(
        benchmark_session_weight=2.0,
        benchmark_momentum_weight=1.0,
        breadth_weight=3.0,
        sector_weight=2.0,
        relative_strength_weight=14.0,
        benchmark_session_scale_percent=1.0,
        benchmark_momentum_scale_percent=0.50,
        relative_strength_scale_percent=3.0,
    ),
    AssetClass.CRYPTO: MarketContextScoreConfig(
        benchmark_session_weight=4.0,
        benchmark_momentum_weight=2.0,
        breadth_weight=2.0,
        sector_weight=0.0,
        relative_strength_weight=12.0,
        benchmark_session_scale_percent=2.0,
        benchmark_momentum_scale_percent=1.0,
        relative_strength_scale_percent=4.0,
    ),
}


def score_market_context(
    *,
    context: CandidateMarketContext | None,
    side: str,
    entry_freshness_score: float | None = None,
) -> MarketContextScore:
    direction = _side_direction(side)
    if context is None or direction == 0.0:
        return MarketContextScore(
            score=0.0,
            components=_empty_components(),
            diagnostics={
                'available': False,
                'side': side,
                'finalized_with_entry_freshness': False,
            },
        )

    config = _CONFIG_BY_ASSET_CLASS[context.asset_class]
    benchmark_session = _scaled_component(
        value=_directional(context.benchmark.session_return_percent, direction),
        scale=config.benchmark_session_scale_percent,
        weight=config.benchmark_session_weight,
    )
    benchmark_momentum = _scaled_component(
        value=_directional(context.benchmark.momentum_percent, direction),
        scale=config.benchmark_momentum_scale_percent,
        weight=config.benchmark_momentum_weight,
    )
    breadth = _participation_component(
        available=context.breadth.available,
        advancing_ratio=context.breadth.advancing_ratio,
        direction=direction,
        weight=config.breadth_weight,
    )
    sector = _participation_component(
        available=context.sector.available,
        advancing_ratio=context.sector.advancing_ratio,
        direction=direction,
        weight=config.sector_weight,
    )
    market_background = benchmark_session + benchmark_momentum + breadth + sector
    directional_relative_strength = _directional(
        context.symbol_relative_strength_percent,
        direction,
    )
    relative_strength_raw = _scaled_component(
        value=directional_relative_strength,
        scale=config.relative_strength_scale_percent,
        weight=config.relative_strength_weight,
    )
    relative_strength_adjustment = _relative_strength_adjustment(
        raw_adjustment=relative_strength_raw,
        market_background=market_background,
        config=config,
    )
    raw_score = _clamp(
        market_background + relative_strength_adjustment,
        -config.maximum_absolute_raw_score,
        config.maximum_absolute_raw_score,
    )
    score = _clamp(
        raw_score * config.effective_score_scale,
        -config.maximum_absolute_effective_score,
        config.maximum_absolute_effective_score,
    )
    components = {
        'benchmark_session': round(benchmark_session, 4),
        'benchmark_momentum': round(benchmark_momentum, 4),
        'breadth': round(breadth, 4),
        'sector': round(sector, 4),
        'market_background': round(market_background, 4),
        'relative_strength_raw': round(relative_strength_raw, 4),
        'relative_strength_adjustment': round(relative_strength_adjustment, 4),
        'raw_market_context_score': round(raw_score, 4),
        'effective_market_context_contribution': round(score, 4),
    }
    return MarketContextScore(
        score=round(score, 4),
        components=components,
        diagnostics={
            'available': True,
            'asset_class': context.asset_class.value,
            'side': side.strip().upper(),
            'context_alignment': context.alignment.value,
            'market_regime': context.regime.value,
            'benchmark_symbol': context.benchmark.symbol,
            'benchmark_session_return_percent': (
                context.benchmark.session_return_percent
            ),
            'benchmark_momentum_percent': context.benchmark.momentum_percent,
            'breadth_advancing_ratio': context.breadth.advancing_ratio,
            'sector_advancing_ratio': context.sector.advancing_ratio,
            'symbol_relative_strength_percent': (
                context.symbol_relative_strength_percent
            ),
            'directional_relative_strength_percent': (
                directional_relative_strength
            ),
            'entry_freshness_score': entry_freshness_score,
            'entry_freshness_factor': _freshness_factor(
                entry_freshness_score
            ),
            'raw_market_context_score': round(raw_score, 4),
            'effective_market_context_contribution': round(score, 4),
            'effective_score_scale': config.effective_score_scale,
            'entry_freshness_used_in_live_score': False,
        },
    )


def _relative_strength_adjustment(
    *,
    raw_adjustment: float,
    market_background: float,
    config: MarketContextScoreConfig,
) -> float:
    if raw_adjustment >= 0:
        maximum_positive = (
            abs(market_background) + config.opposed_background_excess_bonus
            if market_background < 0
            else config.favorable_background_relative_bonus
        )
        return min(raw_adjustment, maximum_positive)
    return max(
        raw_adjustment,
        -config.maximum_negative_relative_adjustment,
    )


def _freshness_factor(entry_freshness_score: float | None) -> float:
    if entry_freshness_score is None:
        return 0.0
    return _clamp(float(entry_freshness_score) / 100.0, 0.0, 1.0)


def _empty_components() -> dict[str, float]:
    return {
        'benchmark_session': 0.0,
        'benchmark_momentum': 0.0,
        'breadth': 0.0,
        'sector': 0.0,
        'market_background': 0.0,
        'relative_strength_raw': 0.0,
        'relative_strength_adjustment': 0.0,
        'raw_market_context_score': 0.0,
        'effective_market_context_contribution': 0.0,
    }


def _side_direction(side: str) -> float:
    normalized = side.strip().upper()
    if normalized == 'BUY':
        return 1.0
    if normalized == 'SELL':
        return -1.0
    return 0.0


def _directional(value: float | None, direction: float) -> float | None:
    if value is None:
        return None
    return float(value) * direction


def _scaled_component(
    *,
    value: float | None,
    scale: float,
    weight: float,
) -> float:
    if value is None or scale <= 0 or weight == 0:
        return 0.0
    return weight * math.tanh(float(value) / scale)


def _participation_component(
    *,
    available: bool,
    advancing_ratio: float | None,
    direction: float,
    weight: float,
) -> float:
    if not available or advancing_ratio is None or weight == 0:
        return 0.0
    market_pressure = _clamp(
        (2.0 * float(advancing_ratio)) - 1.0,
        -1.0,
        1.0,
    )
    return weight * market_pressure * direction


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
