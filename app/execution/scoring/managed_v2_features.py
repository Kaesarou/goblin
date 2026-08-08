from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.scoring.managed_v2_model_contract import feature_names_for
from app.execution.strategy_segment import StrategySegment
from app.market.relative_spread import SPREAD_CONTEXT_VERSION
from app.utils.commons import spread_percent


@dataclass(frozen=True)
class ManagedV2FeatureSet:
    segment: StrategySegment
    opportunity: Mapping[str, float | None]
    path: Mapping[str, float | None]
    raw: Mapping[str, float | None]

    def economics(
        self,
        *,
        opportunity_probability: float,
        path_probability: float,
    ) -> dict[str, float | None]:
        values = {
            **self.raw,
            'opportunity_probability': opportunity_probability,
            'path_probability': path_probability,
        }
        return _contract_values(
            values,
            feature_names_for(self.segment, 'economics'),
        )


def extract_managed_v2_features(
    evaluated: EvaluatedTradeCandidate,
) -> ManagedV2FeatureSet:
    candidate = evaluated.candidate
    segment = candidate.segment
    if segment is None:
        raise RuntimeError('MANAGED V2 requires an explicit strategy segment.')
    if not segment.is_equity:
        raise RuntimeError(
            f'MANAGED V2 equity feature contract does not support {segment.value}.'
        )
    side_sign = 1.0 if segment.side == 'BUY' else -1.0
    signal = candidate.signal.metadata or {}
    context = candidate.market_context
    spread_context = context.spread if context is not None else None
    if (
        spread_context is not None
        and spread_context.version != SPREAD_CONTEXT_VERSION
    ):
        raise RuntimeError('MANAGED V2 relative spread contract mismatch.')
    benchmark_momentum = (
        None
        if context is None or not context.benchmark.available
        else _number(context.benchmark.momentum_percent)
    )
    relative_strength = (
        None
        if context is None
        else _number(context.symbol_relative_strength_percent)
    )
    tp = evaluated.tp_feasibility
    probability = candidate.outcome_probability_metadata or {}
    activity = _mapping(probability.get('activity_features'))

    values: dict[str, float | None] = {
        'touch_probability': _number(candidate.touch_probability),
        'entry_freshness_score': _attribute_number(
            tp,
            'entry_freshness_score',
        ),
        'movement_consumed_to_tp_ratio': _attribute_number(
            tp,
            'movement_consumed_to_tp_ratio',
        ),
        'spread_percent': _number(spread_percent(candidate.snapshot)),
        'relative_spread_ratio': (
            None
            if spread_context is None or not spread_context.available
            else _number(spread_context.relative_to_median)
        ),
        'relative_spread_percentile': (
            None
            if spread_context is None or not spread_context.available
            else _number(spread_context.reference_percentile)
        ),
        'relative_spread_recent_change': (
            None
            if spread_context is None or not spread_context.available
            else _number(spread_context.recent_change_ratio)
        ),
        'estimated_total_cost_percent': _number(
            evaluated.economics.estimated_total_cost_percent
        ),
        'expected_net_profit_percent': _number(
            evaluated.economics.expected_net_profit_percent
        ),
        'effective_take_profit_percent': _number(
            evaluated.economics.effective_take_profit_percent
        ),
        'effective_stop_loss_percent': _number(
            evaluated.economics.effective_stop_loss_percent
        ),
        'aligned_session_move': _aligned(
            signal.get('session_move_percent'),
            side_sign,
        ),
        'aligned_snapshot_momentum': _aligned(
            signal.get('snapshot_momentum_percent'),
            side_sign,
        ),
        'aligned_symbol_relative_strength': _aligned(
            relative_strength,
            side_sign,
        ),
        'aligned_benchmark_momentum': _aligned(
            benchmark_momentum,
            side_sign,
        ),
        'atr_percent': _first(
            _attribute_number(tp, 'atr_percent'),
            _number(signal.get('atr_percent')),
        ),
        'regime_noise_ratio': _number(signal.get('regime_noise_ratio')),
        'session_progress': _number(activity.get('session_progress')),
    }
    values.update(_multi_timeframe_values(evaluated, side_sign=side_sign))
    return ManagedV2FeatureSet(
        segment=segment,
        opportunity=_contract_values(
            values,
            feature_names_for(segment, 'opportunity'),
        ),
        path=_contract_values(
            values,
            feature_names_for(segment, 'path'),
        ),
        raw=values,
    )


def missing_feature_names(
    features: Mapping[str, float | None],
) -> list[str]:
    return [name for name, value in features.items() if value is None]


def _multi_timeframe_values(
    evaluated: EvaluatedTradeCandidate,
    *,
    side_sign: float,
) -> dict[str, float | None]:
    candidate = evaluated.candidate
    context = candidate.multi_timeframe_context
    result: dict[str, float | None] = {}
    for timeframe in ('m15', 'm30', 'h1'):
        features = (
            None
            if context is None
            else context.features_by_timeframe.get(timeframe)
        )
        if (
            features is not None
            and features.latest_bar_closed_at > candidate.candle.closed_at
        ):
            raise RuntimeError(
                f'MANAGED V2 {timeframe} feature uses future information.'
            )
        for output, attribute in (
            ('return_sample_aligned', 'return_sample_percent'),
            ('close_vs_fast_ema_aligned', 'close_vs_fast_ema_percent'),
            ('fast_vs_slow_ema_aligned', 'fast_vs_slow_ema_percent'),
            ('velocity_aligned', 'velocity_percent_per_bar'),
            ('acceleration_aligned', 'acceleration_percent_per_bar'),
        ):
            result[f'{timeframe}_{output}'] = _aligned(
                None if features is None else getattr(features, attribute),
                side_sign,
            )
    return result


def _contract_values(
    values: Mapping[str, float | None],
    feature_names: tuple[str, ...],
) -> dict[str, float | None]:
    missing_contract_keys = [name for name in feature_names if name not in values]
    if missing_contract_keys:
        raise RuntimeError(
            'MANAGED V2 runtime feature implementation is incomplete: '
            + ', '.join(missing_contract_keys)
        )
    return {name: values[name] for name in feature_names}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _attribute_number(value: Any, name: str) -> float | None:
    return _number(None if value is None else getattr(value, name, None))


def _aligned(value: Any, sign: float) -> float | None:
    number = _number(value)
    return None if number is None else number * sign


def _first(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(number) else number
