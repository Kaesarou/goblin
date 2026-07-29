from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.scoring.frozen_logistic import (
    FrozenOutcomeProbabilityModel,
)
from app.execution.scoring.outcome_probability_model_contract import (
    ACTIVITY_CATEGORICAL_FEATURES,
    ACTIVITY_NUMERIC_FEATURES,
    DIRECTION_FEATURES_BY_SEGMENT,
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
    PROBABILITY_SCORE_SCALE,
    SUPPORTED_DIRECTION_SEGMENTS,
    TRAINING_ASSET_CLASSES,
)
from app.instruments.models import RiskProfile
from app.market.timeframes import TimeframeDirection, TimeframeMaturity

_SESSION_KEY_PATTERN = re.compile(
    r'^[^:]+:'
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}):'
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})$'
)


@dataclass(frozen=True)
class OutcomeProbabilityEstimate:
    in_training_domain: bool
    touch_probability: float | None
    direction_probability_raw: float | None
    direction_segment_prior: float | None
    direction_probability: float | None
    tp_probability: float | None
    sl_probability: float | None
    neither_probability: float | None
    probability_score: float | None
    direction_break_even_probability: float
    direction_edge: float | None
    direction_model_segment: str
    direction_feature_family: str
    direction_training_status: str
    direction_source_segment: str | None
    direction_model_weight: float
    direction_segment_prior_weight: float
    profile_key: str
    model_version: str
    feature_contract_version: str
    missing_features: tuple[str, ...]
    activity_features: Mapping[str, Any]
    direction_features: Mapping[str, Any]


class OutcomeProbabilityEstimator:
    def __init__(
        self,
        model: FrozenOutcomeProbabilityModel | None = None,
    ) -> None:
        self.model = model or _default_model()
        if self.model.version != OUTCOME_PROBABILITY_MODEL_VERSION:
            raise RuntimeError(
                'Outcome probability model version does not match code.'
            )
        if (
            self.model.feature_contract_version
            != OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
        ):
            raise RuntimeError(
                'Outcome probability feature contract does not match code.'
            )
        if self.model.training_asset_classes != TRAINING_ASSET_CLASSES:
            raise RuntimeError(
                'Outcome probability training-domain contract does not match '
                'code.'
            )
        if self.model.supported_segments != SUPPORTED_DIRECTION_SEGMENTS:
            raise RuntimeError(
                'Outcome probability segment contract does not match code.'
            )

    def estimate(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
        risk_profile: RiskProfile,
    ) -> OutcomeProbabilityEstimate:
        features = _feature_values(
            evaluated_candidate=evaluated_candidate,
            risk_profile=risk_profile,
        )
        profile_key = str(features['profile_key'])
        asset_class = str(features['asset_class'])
        side = str(features['side'])
        break_even = _direction_break_even_probability(
            evaluated_candidate
        )
        activity_features = {
            name: features.get(name)
            for name in (
                *ACTIVITY_NUMERIC_FEATURES,
                *ACTIVITY_CATEGORICAL_FEATURES,
            )
        }
        touch_probability = self.model.activity.predict(
            activity_features
        )
        features['touch_probability'] = touch_probability
        features['direction_break_even_probability'] = break_even

        segment = self.model.direction_for(
            asset_class=asset_class,
            side=side,
        )
        numeric_features, categorical_features = (
            DIRECTION_FEATURES_BY_SEGMENT[segment.segment]
        )
        direction_features = {
            name: features.get(name)
            for name in (*numeric_features, *categorical_features)
        }
        direction_raw, direction_probability = segment.predict(
            direction_features
        )
        tp_probability = touch_probability * direction_probability
        sl_probability = touch_probability * (
            1.0 - direction_probability
        )
        neither_probability = 1.0 - touch_probability
        missing = tuple(
            sorted(
                name
                for name, value in {
                    **activity_features,
                    **direction_features,
                }.items()
                if value is None
            )
        )
        return OutcomeProbabilityEstimate(
            in_training_domain=(segment.training_status == 'trained'),
            touch_probability=touch_probability,
            direction_probability_raw=direction_raw,
            direction_segment_prior=segment.segment_prior,
            direction_probability=direction_probability,
            tp_probability=tp_probability,
            sl_probability=sl_probability,
            neither_probability=neither_probability,
            probability_score=round(
                PROBABILITY_SCORE_SCALE * tp_probability,
                4,
            ),
            direction_break_even_probability=break_even,
            direction_edge=direction_probability - break_even,
            direction_model_segment=segment.segment,
            direction_feature_family=segment.feature_family,
            direction_training_status=segment.training_status,
            direction_source_segment=segment.source_segment,
            direction_model_weight=segment.model_weight,
            direction_segment_prior_weight=(
                segment.segment_prior_weight
            ),
            profile_key=profile_key,
            model_version=self.model.version,
            feature_contract_version=self.model.feature_contract_version,
            missing_features=missing,
            activity_features=activity_features,
            direction_features=direction_features,
        )


class CandidateOutcomeProbabilityEvaluator:
    def __init__(
        self,
        estimator: OutcomeProbabilityEstimator | None = None,
    ) -> None:
        self.estimator = estimator or OutcomeProbabilityEstimator()

    def evaluate(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
        risk_profile: RiskProfile,
    ) -> EvaluatedTradeCandidate:
        estimate = self.estimator.estimate(
            evaluated_candidate=evaluated_candidate,
            risk_profile=risk_profile,
        )
        candidate = evaluated_candidate.candidate
        updated = replace(
            candidate,
            rank_reason=_append_rank_reason(
                candidate.rank_reason,
                estimate,
            ),
            probability_score=estimate.probability_score,
            touch_probability=estimate.touch_probability,
            direction_probability=estimate.direction_probability,
            tp_probability=estimate.tp_probability,
            sl_probability=estimate.sl_probability,
            neither_probability=estimate.neither_probability,
            direction_break_even_probability=(
                estimate.direction_break_even_probability
            ),
            direction_edge=estimate.direction_edge,
            outcome_probability_model_version=estimate.model_version,
            outcome_probability_metadata=estimate_to_metadata(estimate),
        )
        return replace(
            evaluated_candidate,
            candidate=updated,
            outcome_probability=estimate,
        )


def estimate_to_metadata(
    estimate: OutcomeProbabilityEstimate,
) -> dict[str, Any]:
    return {
        'in_training_domain': estimate.in_training_domain,
        'touch_probability': estimate.touch_probability,
        'direction_probability_raw': (
            estimate.direction_probability_raw
        ),
        'direction_segment_prior': estimate.direction_segment_prior,
        'direction_probability': estimate.direction_probability,
        'tp_probability': estimate.tp_probability,
        'sl_probability': estimate.sl_probability,
        'neither_probability': estimate.neither_probability,
        'probability_score': estimate.probability_score,
        'direction_break_even_probability': (
            estimate.direction_break_even_probability
        ),
        'direction_edge': estimate.direction_edge,
        'direction_model_segment': estimate.direction_model_segment,
        'direction_feature_family': estimate.direction_feature_family,
        'direction_training_status': estimate.direction_training_status,
        'direction_source_segment': estimate.direction_source_segment,
        'direction_model_weight': estimate.direction_model_weight,
        'direction_segment_prior_weight': (
            estimate.direction_segment_prior_weight
        ),
        'profile_key': estimate.profile_key,
        'model_version': estimate.model_version,
        'feature_contract_version': estimate.feature_contract_version,
        'missing_features': list(estimate.missing_features),
        'activity_features': dict(estimate.activity_features),
        'direction_features': dict(estimate.direction_features),
    }


def _feature_values(
    *,
    evaluated_candidate: EvaluatedTradeCandidate,
    risk_profile: RiskProfile,
) -> dict[str, Any]:
    candidate = evaluated_candidate.candidate
    economics = evaluated_candidate.economics
    feasibility = evaluated_candidate.tp_feasibility
    decision = evaluated_candidate.entry_decision
    if feasibility is None:
        raise ValueError(
            'Outcome probability requires TP feasibility evidence.'
        )
    if decision is None:
        raise ValueError(
            'Outcome probability requires an entry decision.'
        )

    asset_class = risk_profile.asset_class.value
    side = candidate.signal.action.strip().upper()
    profile_key = _profile_key(evaluated_candidate, risk_profile)
    session_minutes, session_progress = _session_coordinates(
        timestamp=candidate.snapshot.timestamp,
        session_key=candidate.session_key,
        asset_class=asset_class,
    )
    mtf = {
        timeframe: _timeframe_values(
            evaluated_candidate,
            timeframe,
        )
        for timeframe in ('m5', 'm15', 'm30', 'h1')
    }
    consumed = feasibility.movement_consumed_to_tp_ratio
    m5_aligned_x_consumed = (
        None
        if consumed is None
        else float(consumed)
        if mtf['m5']['alignment'] == 'aligned'
        else 0.0
    )
    stale_position = risk_profile.stale_position_for(side)
    extension_to_tp_ratio = decision.diagnostics.get(
        'extension_to_tp_ratio'
    )
    metadata = candidate.signal.metadata or {}
    context = candidate.market_context
    side_sign = 1.0 if side == 'BUY' else -1.0
    close_position = _float_or_none(
        metadata.get('close_position_percent')
    )
    close_quality = (
        None
        if close_position is None
        else close_position
        if side == 'BUY'
        else 100.0 - close_position
    )
    breakout = _float_or_none(
        metadata.get(
            'breakout_percent'
            if side == 'BUY'
            else 'breakdown_percent'
        )
    )
    benchmark_momentum = (
        None
        if context is None
        else context.benchmark.momentum_percent
    )
    relative_strength = (
        None
        if context is None
        else context.symbol_relative_strength_percent
    )
    context_regime = (
        None if context is None else context.regime.value
    )
    context_alignment = (
        None if context is None else context.alignment.value
    )
    component_scores = feasibility.component_scores

    return {
        'effective_take_profit_percent': (
            feasibility.effective_take_profit_percent
        ),
        'effective_stop_loss_percent': (
            feasibility.effective_stop_loss_percent
        ),
        'estimated_total_cost_percent': (
            economics.estimated_total_cost_percent
        ),
        'expected_net_profit_percent': (
            economics.expected_net_profit_percent
        ),
        'reward_to_risk_ratio': economics.reward_to_risk_ratio,
        'net_reward_to_risk_ratio': (
            economics.net_reward_to_risk_ratio
        ),
        'horizon_minutes': stale_position.max_age_minutes,
        'session_minutes': session_minutes,
        'session_progress': session_progress,
        'base_score': candidate.base_score,
        'directional_score': candidate.directional_score,
        'tp_feasibility_score': feasibility.feasibility_score,
        'tp_feasibility_contribution': feasibility.score_contribution,
        'tp_component_atr': component_scores.get('tp_vs_atr'),
        'tp_component_momentum': component_scores.get('tp_vs_momentum'),
        'tp_component_cost': component_scores.get('cost_vs_tp'),
        'tp_to_atr_ratio': feasibility.tp_to_atr_ratio,
        'tp_to_momentum_ratio': (
            feasibility.tp_to_snapshot_momentum_ratio
        ),
        'cost_to_tp_ratio': feasibility.cost_to_tp_ratio,
        'movement_consumed_to_tp_ratio': consumed,
        'entry_freshness_score': feasibility.entry_freshness_score,
        'extension_to_tp_ratio': extension_to_tp_ratio,
        'context_relative_strength_raw': (
            candidate.market_context_components or {}
        ).get('relative_strength_raw'),
        'context_sector': (
            candidate.market_context_components or {}
        ).get('sector'),
        'm5_aligned_x_consumed': m5_aligned_x_consumed,
        'context_benchmark_momentum': (
            candidate.market_context_components or {}
        ).get('benchmark_momentum'),
        'asset_class': asset_class,
        'side': side,
        'profile_key': profile_key,
        'entry_route_action': decision.action.value,
        'market_regime_context': context_regime,
        'context_alignment': context_alignment,
        'aligned_session_move': _aligned(
            metadata.get('session_move_percent'),
            side_sign,
        ),
        'aligned_snapshot_momentum': _aligned(
            metadata.get('snapshot_momentum_percent'),
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
        'aligned_m5_return': _aligned(
            mtf['m5']['return'],
            side_sign,
        ),
        'aligned_m15_return': _aligned(
            mtf['m15']['return'],
            side_sign,
        ),
        'aligned_m5_velocity': _aligned(
            mtf['m5']['velocity'],
            side_sign,
        ),
        'aligned_m15_velocity': _aligned(
            mtf['m15']['velocity'],
            side_sign,
        ),
        'aligned_m5_acceleration': _aligned(
            mtf['m5']['acceleration'],
            side_sign,
        ),
        'aligned_m15_acceleration': _aligned(
            mtf['m15']['acceleration'],
            side_sign,
        ),
        'breakout_percent': breakout,
        'trend_strength_percent': _float_or_none(
            metadata.get('trend_strength_percent')
        ),
        'close_quality': close_quality,
        'atr_percent': _float_or_none(metadata.get('atr_percent')),
        'regime_noise_ratio': _float_or_none(
            metadata.get('regime_noise_ratio')
        ),
        'm5_maturity': mtf['m5']['maturity'],
        'm5_alignment': mtf['m5']['alignment'],
        'm5_direction': mtf['m5']['direction'],
        'm15_maturity': mtf['m15']['maturity'],
        'm15_alignment': mtf['m15']['alignment'],
        'm15_direction': mtf['m15']['direction'],
        'm30_maturity': mtf['m30']['maturity'],
        'm30_alignment': mtf['m30']['alignment'],
        'm30_direction': mtf['m30']['direction'],
        'h1_maturity': mtf['h1']['maturity'],
        'h1_alignment': mtf['h1']['alignment'],
        'h1_direction': mtf['h1']['direction'],
    }


def _timeframe_values(
    evaluated_candidate: EvaluatedTradeCandidate,
    timeframe: str,
) -> dict[str, Any]:
    candidate = evaluated_candidate.candidate
    context = candidate.multi_timeframe_context
    if context is None:
        return {
            'maturity': 'unavailable',
            'alignment': 'unavailable',
            'direction': 'unknown',
            'return': None,
            'velocity': None,
            'acceleration': None,
        }
    maturity = context.maturity_by_timeframe.get(timeframe)
    maturity_value = (
        maturity.value
        if maturity is not None
        else TimeframeMaturity.UNAVAILABLE.value
    )
    feature = context.features_by_timeframe.get(timeframe)
    if feature is None:
        return {
            'maturity': maturity_value,
            'alignment': 'unavailable',
            'direction': 'unknown',
            'return': None,
            'velocity': None,
            'acceleration': None,
        }
    direction = feature.direction
    direction_value = getattr(direction, 'value', str(direction))
    side = candidate.signal.action.strip().upper()
    if maturity != TimeframeMaturity.READY:
        alignment = 'unavailable'
    elif direction not in {
        TimeframeDirection.UP,
        TimeframeDirection.DOWN,
    }:
        alignment = 'neutral'
    elif (
        direction == TimeframeDirection.UP and side == 'BUY'
    ) or (
        direction == TimeframeDirection.DOWN and side == 'SELL'
    ):
        alignment = 'aligned'
    else:
        alignment = 'opposed'
    return {
        'maturity': maturity_value,
        'alignment': alignment,
        'direction': direction_value,
        'return': _float_or_none(
            getattr(feature, 'return_sample_percent', None)
        ),
        'velocity': _float_or_none(
            getattr(feature, 'velocity_percent_per_bar', None)
        ),
        'acceleration': _float_or_none(
            getattr(feature, 'acceleration_percent_per_bar', None)
        ),
    }


def _profile_key(
    evaluated_candidate: EvaluatedTradeCandidate,
    risk_profile: RiskProfile,
) -> str:
    effective = evaluated_candidate.effective_sl_tp
    if effective is None:
        return risk_profile.profile_key
    if effective.source == 'pending_structural':
        return str(
            effective.metadata.get('baseline_sl_tp_source')
            or risk_profile.profile_key
        )
    return str(effective.source)


def _session_coordinates(
    *,
    timestamp: datetime,
    session_key: str,
    asset_class: str,
) -> tuple[float | None, float | None]:
    match = _SESSION_KEY_PATTERN.fullmatch(session_key)
    actual_timestamp = _as_utc(timestamp)
    if match is not None:
        start = _as_utc(datetime.fromisoformat(match.group(1)))
        end = _as_utc(datetime.fromisoformat(match.group(2)))
        duration_minutes = (end - start).total_seconds() / 60.0
        if duration_minutes > 0:
            minutes = (actual_timestamp - start).total_seconds() / 60.0
            progress = max(
                -0.25,
                min(1.25, minutes / duration_minutes),
            )
            return minutes, progress
    if asset_class == 'EQUITY_US':
        start_minutes = 13 * 60 + 30
        end_minutes = 20 * 60
    elif asset_class == 'EQUITY_EU':
        start_minutes = 7 * 60
        end_minutes = 15 * 60 + 30
    elif asset_class == 'CRYPTO':
        return (
            actual_timestamp.hour * 60
            + actual_timestamp.minute
            + actual_timestamp.second / 60.0,
            (
                actual_timestamp.hour * 60
                + actual_timestamp.minute
                + actual_timestamp.second / 60.0
            )
            / (24 * 60),
        )
    else:
        return None, None
    absolute_minutes = (
        actual_timestamp.hour * 60
        + actual_timestamp.minute
        + actual_timestamp.second / 60.0
    )
    minutes = absolute_minutes - start_minutes
    duration_minutes = end_minutes - start_minutes
    progress = max(-0.25, min(1.25, minutes / duration_minutes))
    return minutes, progress


def _direction_break_even_probability(
    evaluated_candidate: EvaluatedTradeCandidate,
) -> float:
    feasibility = evaluated_candidate.tp_feasibility
    if feasibility is None:
        return 1.0
    net_gain = max(
        float(
            evaluated_candidate.economics.expected_net_profit_percent
        ),
        0.0,
    )
    net_loss = max(
        float(feasibility.effective_stop_loss_percent)
        + float(
            evaluated_candidate.economics.estimated_total_cost_percent
        ),
        0.0,
    )
    total = net_gain + net_loss
    return net_loss / total if total > 0 else 1.0


def _append_rank_reason(
    rank_reason: str,
    estimate: OutcomeProbabilityEstimate,
) -> str:
    suffix = (
        f'probability_score={estimate.probability_score},'
        f'p_touch={_format_probability(estimate.touch_probability)},'
        f'p_direction_raw='
        f'{_format_probability(estimate.direction_probability_raw)},'
        f'p_direction_prior='
        f'{_format_probability(estimate.direction_segment_prior)},'
        f'p_direction={_format_probability(estimate.direction_probability)},'
        f'p_tp={_format_probability(estimate.tp_probability)},'
        f'p_sl={_format_probability(estimate.sl_probability)},'
        f'p_neither={_format_probability(estimate.neither_probability)},'
        f'direction_break_even='
        f'{_format_probability(estimate.direction_break_even_probability)},'
        f'direction_edge={_format_probability(estimate.direction_edge)},'
        f'direction_segment={estimate.direction_model_segment},'
        f'outcome_probability_model={estimate.model_version}'
    )
    return f'{rank_reason};{suffix}' if rank_reason else suffix


def _aligned(value: Any, side_sign: float) -> float | None:
    parsed = _float_or_none(value)
    return None if parsed is None else parsed * side_sign


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_probability(value: float | None) -> str:
    return 'None' if value is None else f'{value:.6f}'


@lru_cache(maxsize=1)
def _default_model() -> FrozenOutcomeProbabilityModel:
    return FrozenOutcomeProbabilityModel.load()
