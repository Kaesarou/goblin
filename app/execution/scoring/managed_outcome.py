from __future__ import annotations

import base64
import gzip
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from typing import Any

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.scoring.frozen_logistic import FrozenLogisticModel
from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_NUMERIC_FEATURES,
    MANAGED_OUTCOME_FEATURE_CONTRACT_VERSION,
    MANAGED_OUTCOME_MODEL_VERSION,
    SUPPORTED_MANAGED_SEGMENTS,
    TRAINING_ASSET_CLASSES,
)

_TIME_FEATURES = frozenset({'session_progress', 'session_progress_sq'})


@dataclass(frozen=True)
class ManagedSegmentModel:
    segment: str
    training_status: str
    source_segment: str | None
    training_rows: int
    model_weight: float
    prior_weight: float
    protection_prior: float
    positive_prior: float
    minimum_protection: float
    minimum_positive: float
    minimum_return: float
    protection: FrozenLogisticModel
    positive: FrozenLogisticModel
    expected_return: FrozenLogisticModel

    @classmethod
    def from_dict(
        cls,
        segment: str,
        value: Mapping[str, Any],
    ) -> ManagedSegmentModel:
        model_weight = float(value['model_weight'])
        prior_weight = float(value['segment_prior_weight'])
        if not math.isclose(model_weight + prior_weight, 1.0, abs_tol=1e-12):
            raise RuntimeError(
                f'Invalid managed calibration weights for {segment}.'
            )
        return cls(
            segment=segment,
            training_status=str(value['training_status']),
            source_segment=(
                None
                if value.get('source_segment') is None
                else str(value['source_segment'])
            ),
            training_rows=int(value['training_rows']),
            model_weight=model_weight,
            prior_weight=prior_weight,
            protection_prior=float(value['protection_prior']),
            positive_prior=float(value['positive_prior']),
            minimum_protection=float(
                value['minimum_protection_probability']
            ),
            minimum_positive=float(value['minimum_positive_probability']),
            minimum_return=float(
                value['minimum_expected_net_return_percent']
            ),
            protection=FrozenLogisticModel.from_dict(
                value['protection_model']
            ),
            positive=FrozenLogisticModel.from_dict(value['positive_model']),
            expected_return=FrozenLogisticModel.from_dict(
                value['return_model']
            ),
        )

    def calibrated(self, raw: float, prior: float) -> float:
        return min(
            1.0,
            max(0.0, self.model_weight * raw + self.prior_weight * prior),
        )


@dataclass(frozen=True)
class FrozenManagedOutcomeModel:
    version: str
    feature_contract_version: str
    training_asset_classes: tuple[str, ...]
    supported_segments: tuple[str, ...]
    segments: Mapping[str, ManagedSegmentModel]
    provenance: Mapping[str, Any]
    artifact_sha256: str

    @classmethod
    def load(cls) -> FrozenManagedOutcomeModel:
        model_root = files('app.execution.scoring').joinpath(
            'models/managed_outcome_v1'
        )
        manifest_raw = model_root.joinpath('manifest.json').read_bytes()
        manifest = json.loads(manifest_raw)
        version = str(manifest['version'])
        feature_contract = str(manifest['feature_contract_version'])
        training_assets = tuple(manifest['training_asset_classes'])
        supported = tuple(manifest['supported_segments'])
        if version != MANAGED_OUTCOME_MODEL_VERSION:
            raise RuntimeError('Managed outcome model version mismatch.')
        if feature_contract != MANAGED_OUTCOME_FEATURE_CONTRACT_VERSION:
            raise RuntimeError('Managed outcome feature contract mismatch.')
        if training_assets != TRAINING_ASSET_CLASSES:
            raise RuntimeError('Managed outcome training domain mismatch.')
        if supported != SUPPORTED_MANAGED_SEGMENTS:
            raise RuntimeError('Managed outcome segment contract mismatch.')
        segment_files = manifest.get('segment_files')
        if not isinstance(segment_files, Mapping):
            raise RuntimeError('Managed outcome segment file map is missing.')
        if set(segment_files) != set(supported):
            raise RuntimeError('Managed outcome segment file map is incomplete.')

        digest = sha256()
        digest.update(manifest_raw)
        segments: dict[str, ManagedSegmentModel] = {}
        for name in supported:
            segment_raw = model_root.joinpath(
                str(segment_files[name])
            ).read_bytes()
            digest.update(segment_raw)
            wrapper = json.loads(segment_raw)
            value = (
                json.loads(
                    gzip.decompress(base64.b64decode(wrapper['payload']))
                )
                if wrapper.get('encoding') == 'gzip+base64'
                else wrapper
            )
            segments[name] = ManagedSegmentModel.from_dict(name, value)
        return cls(
            version=version,
            feature_contract_version=feature_contract,
            training_asset_classes=training_assets,
            supported_segments=supported,
            segments=segments,
            provenance=manifest['provenance'],
            artifact_sha256=digest.hexdigest(),
        )

    def segment_for(self, asset_class: str, side: str) -> ManagedSegmentModel:
        key = f'{asset_class.strip().upper()}_{side.strip().upper()}'
        try:
            return self.segments[key]
        except KeyError as exc:
            raise RuntimeError(f'No managed model for {key}.') from exc


@dataclass(frozen=True)
class ManagedOutcomeEstimate:
    protection_probability: float
    positive_probability: float
    expected_return: float
    managed_edge: float
    metadata: Mapping[str, Any]


class ManagedOutcomeEstimator:
    def __init__(
        self,
        model: FrozenManagedOutcomeModel | None = None,
    ) -> None:
        self.model = model or _default_model()

    def estimate(
        self,
        evaluated_candidate: EvaluatedTradeCandidate,
    ) -> ManagedOutcomeEstimate:
        features, asset_class, side = _features(evaluated_candidate)
        segment = self.model.segment_for(asset_class, side)
        protection_raw = segment.protection.predict(features)
        positive_raw = segment.positive.predict(features)
        protection = segment.calibrated(
            protection_raw,
            segment.protection_prior,
        )
        positive = segment.calibrated(positive_raw, segment.positive_prior)
        return_raw = _linear_score(segment.expected_return, features)
        expected_return = _clamp_return(return_raw, evaluated_candidate)
        managed_edge = expected_return - segment.minimum_return
        metadata = {
            'segment': segment.segment,
            'training_status': segment.training_status,
            'source_segment': segment.source_segment,
            'protection_probability_raw': protection_raw,
            'protection_probability': protection,
            'protection_prior': segment.protection_prior,
            'minimum_protection_probability': segment.minimum_protection,
            'positive_probability_raw': positive_raw,
            'positive_probability': positive,
            'positive_prior': segment.positive_prior,
            'minimum_positive_probability': segment.minimum_positive,
            'expected_net_return_percent_raw': return_raw,
            'expected_net_return_percent': expected_return,
            'minimum_expected_net_return_percent': segment.minimum_return,
            'managed_edge': managed_edge,
            'time_adjustment': {
                'protection_logit_contribution': _time_score(
                    segment.protection,
                    features,
                )
                * segment.model_weight,
                'positive_logit_contribution': _time_score(
                    segment.positive,
                    features,
                )
                * segment.model_weight,
                'expected_return_contribution': _time_score(
                    segment.expected_return,
                    features,
                ),
                'hard_time_route': False,
            },
            'model_version': self.model.version,
            'feature_contract_version': self.model.feature_contract_version,
            'artifact_sha256': self.model.artifact_sha256,
            'features': features,
            'missing_features': [
                name for name, value in features.items() if _number(value) is None
            ],
        }
        return ManagedOutcomeEstimate(
            protection_probability=protection,
            positive_probability=positive,
            expected_return=expected_return,
            managed_edge=managed_edge,
            metadata=metadata,
        )


class CandidateManagedOutcomeEvaluator:
    def __init__(
        self,
        estimator: ManagedOutcomeEstimator | None = None,
    ) -> None:
        self.estimator = estimator or ManagedOutcomeEstimator()

    def evaluate(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
    ) -> EvaluatedTradeCandidate:
        estimate = self.estimator.estimate(evaluated_candidate)
        candidate = replace(
            evaluated_candidate.candidate,
            managed_protection_probability=(
                estimate.protection_probability
            ),
            managed_positive_probability=estimate.positive_probability,
            managed_expected_net_return_percent=estimate.expected_return,
            managed_edge=estimate.managed_edge,
            managed_outcome_model_version=self.estimator.model.version,
            managed_outcome_metadata=dict(estimate.metadata),
        )
        return replace(evaluated_candidate, candidate=candidate)


def _features(
    evaluated: EvaluatedTradeCandidate,
) -> tuple[dict[str, float | None], str, str]:
    candidate = evaluated.candidate
    probability = candidate.outcome_probability_metadata or {}
    activity = _mapping(probability.get('activity_features'))
    direction = _mapping(probability.get('direction_features'))
    signal = candidate.signal.metadata or {}
    side = candidate.signal.action.strip().upper()
    sign = 1.0 if side == 'BUY' else -1.0
    asset_class = str(
        activity.get('asset_class')
        or _asset_class(probability.get('direction_model_segment'))
    ).upper()
    if not asset_class:
        raise RuntimeError('Managed outcome requires an asset class.')
    progress = _first(
        direction.get('session_progress'),
        activity.get('session_progress'),
    )
    close_position = _number(signal.get('close_position_percent'))
    close_quality = _first(direction.get('close_quality'))
    if close_quality is None and close_position is not None:
        close_quality = close_position if side == 'BUY' else 100 - close_position
    breakout = _first(
        direction.get('breakout_percent'),
        signal.get(
            'breakout_percent' if side == 'BUY' else 'breakdown_percent'
        ),
    )
    values = {
        'session_progress': progress,
        'session_progress_sq': None if progress is None else progress**2,
        'aligned_session_move': _first(
            direction.get('aligned_session_move'),
            _aligned(signal.get('session_move_percent'), sign),
        ),
        'aligned_snapshot_momentum': _first(
            direction.get('aligned_snapshot_momentum'),
            _aligned(signal.get('snapshot_momentum_percent'), sign),
        ),
        'aligned_symbol_relative_strength': _first(
            direction.get('aligned_symbol_relative_strength')
        ),
        'aligned_benchmark_momentum': _first(
            direction.get('aligned_benchmark_momentum')
        ),
        'breakout_percent': breakout,
        'aligned_trend_strength': _aligned(
            signal.get('trend_strength_percent'),
            sign,
        ),
        'close_quality': close_quality,
        'atr_percent': _first(
            direction.get('atr_percent'),
            signal.get('atr_percent'),
        ),
        'regime_noise_ratio': _first(
            direction.get('regime_noise_ratio'),
            signal.get('regime_noise_ratio'),
        ),
        'touch_probability': _number(candidate.touch_probability),
        'direction_probability': _number(candidate.direction_probability),
        'direction_edge': _number(candidate.direction_edge),
        'tp_to_atr_ratio': _number(activity.get('tp_to_atr_ratio')),
        'tp_to_momentum_ratio': _number(
            activity.get('tp_to_momentum_ratio')
        ),
        'cost_to_tp_ratio': _number(activity.get('cost_to_tp_ratio')),
        'movement_consumed_to_tp_ratio': _number(
            activity.get('movement_consumed_to_tp_ratio')
        ),
        'tp_feasibility_score': _first(
            activity.get('tp_feasibility_score'),
            candidate.tp_feasibility_score,
        ),
        'entry_freshness_score': _number(
            activity.get('entry_freshness_score')
        ),
        'estimated_total_cost_percent': _number(
            evaluated.economics.estimated_total_cost_percent
        ),
        'expected_net_profit_percent': _number(
            evaluated.economics.expected_net_profit_percent
        ),
        'reward_to_risk_ratio': _number(
            evaluated.economics.reward_to_risk_ratio
        ),
        'net_reward_to_risk_ratio': _number(
            evaluated.economics.net_reward_to_risk_ratio
        ),
        'base_score': _number(candidate.base_score),
        'directional_score': _number(candidate.directional_score),
    }
    if tuple(values) != MANAGED_NUMERIC_FEATURES:
        raise RuntimeError('Managed runtime feature ordering drifted.')
    return values, asset_class, side


def _linear_score(
    model: FrozenLogisticModel,
    features: Mapping[str, Any],
) -> float:
    value = model.intercept
    for term in model.numeric_terms:
        value += _term_score(term, features)
    return value


def _time_score(
    model: FrozenLogisticModel,
    features: Mapping[str, Any],
) -> float:
    return sum(
        _term_score(term, features)
        for term in model.numeric_terms
        if str(term['feature']) in _TIME_FEATURES
    )


def _term_score(
    term: Mapping[str, Any],
    features: Mapping[str, Any],
) -> float:
    raw = _number(features.get(str(term['feature'])))
    value = float(term['impute']) if raw is None else raw
    scale = float(term['scale'])
    if scale <= 0:
        raise RuntimeError(f"Invalid model scale for {term['feature']}.")
    return (
        (value - float(term['mean']))
        / scale
        * float(term['coefficient'])
    )


def _clamp_return(
    raw: float,
    evaluated: EvaluatedTradeCandidate,
) -> float:
    economics = evaluated.economics
    lower = -(
        max(0.0, economics.effective_stop_loss_percent)
        + max(0.0, economics.estimated_total_cost_percent)
    )
    return min(economics.expected_net_profit_percent, max(lower, raw))


@lru_cache(maxsize=1)
def _default_model() -> FrozenManagedOutcomeModel:
    return FrozenManagedOutcomeModel.load()


def _asset_class(segment: Any) -> str:
    value = str(segment or '')
    for suffix in ('_BUY', '_SELL'):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return ''


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
    return None if math.isnan(number) else number
