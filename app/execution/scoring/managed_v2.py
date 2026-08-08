from __future__ import annotations

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
from app.execution.scoring.managed_v2_features import (
    extract_managed_v2_features,
    missing_feature_names,
)
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_ARTIFACT_SCHEMA_VERSION,
    MANAGED_V2_DEPLOYMENT_STATUS,
    MANAGED_V2_ECONOMICS_MODEL_VERSION,
    MANAGED_V2_FEATURE_CONTRACT_VERSION,
    MANAGED_V2_FLOOR_POLICY_VERSION,
    MANAGED_V2_LABEL_CONTRACT_VERSION,
    MANAGED_V2_MODEL_VERSION,
    MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
    MANAGED_V2_PATH_MODEL_VERSION,
    MANAGED_V2_SELECTION_POLICY_VERSION,
    MANAGED_V2_SUPPORTED_SEGMENTS,
    MANAGED_V2_TRAINING_ASSET_CLASSES,
    feature_names_for,
)
from app.execution.strategy_segment import StrategySegment
from app.market.data_quality import quote_quality_contract_metadata


@dataclass(frozen=True)
class FrozenLinearModel:
    intercept: float
    numeric_terms: tuple[Mapping[str, Any], ...]
    missing_indicator_terms: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FrozenLinearModel:
        if value.get('categorical'):
            raise RuntimeError(
                'MANAGED V2 linear models do not support categorical terms.'
            )
        result = cls(
            intercept=_finite_float(
                value['intercept'],
                field='linear model intercept',
            ),
            numeric_terms=tuple(value.get('numeric', ())),
            missing_indicator_terms=tuple(value.get('missing_indicators', ())),
        )
        return result

    def predict(self, features: Mapping[str, Any]) -> float:
        value = self.intercept
        for term in self.numeric_terms:
            value += _numeric_term_score(term, features)
        for term in self.missing_indicator_terms:
            value += _missing_term_score(term, features)
        return value


@dataclass(frozen=True)
class ManagedV2ProbabilityComponent:
    version: str
    training_rows: int
    positive_rows: int
    prior: float
    model_weight: float
    prior_weight: float
    model: FrozenLogisticModel

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        expected_version: str,
        segment: StrategySegment,
        component: str,
    ) -> ManagedV2ProbabilityComponent:
        version = str(value['version'])
        if version != expected_version:
            raise RuntimeError(
                f'MANAGED V2 {component} model version mismatch for '
                f'{segment.value}.'
            )
        training_rows = _row_count(
            value['training_rows'],
            field=f'{segment.value} {component} training_rows',
            positive=True,
        )
        positive_rows = _row_count(
            value['positive_rows'],
            field=f'{segment.value} {component} positive_rows',
        )
        if positive_rows > training_rows:
            raise RuntimeError(
                f'Invalid MANAGED V2 {component} positive row count for '
                f'{segment.value}.'
            )
        prior = _probability(
            value['prior'],
            field=f'{segment.value} {component} prior',
        )
        model_weight = _probability(
            value['model_weight'],
            field=f'{segment.value} {component} model_weight',
        )
        prior_weight = _probability(
            value['prior_weight'],
            field=f'{segment.value} {component} prior_weight',
        )
        if not math.isclose(model_weight + prior_weight, 1.0, abs_tol=1e-12):
            raise RuntimeError(
                f'Invalid MANAGED V2 {component} calibration weights for '
                f'{segment.value}.'
            )
        return cls(
            version=version,
            training_rows=training_rows,
            positive_rows=positive_rows,
            prior=prior,
            model_weight=model_weight,
            prior_weight=prior_weight,
            model=FrozenLogisticModel.from_dict(value['model']),
        )

    def predict(self, features: Mapping[str, Any]) -> tuple[float, float]:
        raw = self.model.predict(features)
        calibrated = self.model_weight * raw + self.prior_weight * self.prior
        return raw, min(1.0, max(0.0, calibrated))


@dataclass(frozen=True)
class ManagedV2EconomicsComponent:
    version: str
    training_rows: int
    model: FrozenLinearModel

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        segment: StrategySegment,
    ) -> ManagedV2EconomicsComponent:
        version = str(value['version'])
        if version != MANAGED_V2_ECONOMICS_MODEL_VERSION:
            raise RuntimeError(
                'MANAGED V2 economics model version mismatch for '
                f'{segment.value}.'
            )
        return cls(
            version=version,
            training_rows=_row_count(
                value['training_rows'],
                field=f'{segment.value} economics training_rows',
                positive=True,
            ),
            model=FrozenLinearModel.from_dict(value['model']),
        )


@dataclass(frozen=True)
class ManagedV2SegmentModel:
    segment: StrategySegment
    training_status: str
    opportunity_floor: float
    path_floor: float
    economics_floor_percent: float
    opportunity_features: tuple[str, ...]
    path_features: tuple[str, ...]
    economics_features: tuple[str, ...]
    opportunity: ManagedV2ProbabilityComponent
    path: ManagedV2ProbabilityComponent
    economics: ManagedV2EconomicsComponent

    @classmethod
    def from_dict(
        cls,
        segment: StrategySegment,
        value: Mapping[str, Any],
    ) -> ManagedV2SegmentModel:
        artifact_segment = StrategySegment(str(value['segment']))
        if artifact_segment is not segment:
            raise RuntimeError(
                f'MANAGED V2 segment artifact mismatch for {segment.value}.'
            )
        feature_contract = value.get('features')
        if not isinstance(feature_contract, Mapping):
            raise TypeError(
                f'MANAGED V2 feature map missing for {segment.value}.'
            )
        opportunity_features = tuple(feature_contract.get('opportunity', ()))
        path_features = tuple(feature_contract.get('path', ()))
        economics_features = tuple(feature_contract.get('economics', ()))
        for component, names in (
            ('opportunity', opportunity_features),
            ('path', path_features),
            ('economics', economics_features),
        ):
            if names != feature_names_for(segment, component):
                raise RuntimeError(
                    f'MANAGED V2 {component} feature contract mismatch for '
                    f'{segment.value}.'
                )
        floors = value.get('floors')
        if not isinstance(floors, Mapping):
            raise TypeError(f'MANAGED V2 floors missing for {segment.value}.')
        if str(floors.get('policy_version')) != MANAGED_V2_FLOOR_POLICY_VERSION:
            raise RuntimeError(
                f'MANAGED V2 floor policy mismatch for {segment.value}.'
            )
        training_status = str(value['training_status'])
        if training_status != 'trained':
            raise RuntimeError(
                f'MANAGED V2 training status mismatch for {segment.value}.'
            )
        result = cls(
            segment=segment,
            training_status=training_status,
            opportunity_floor=_probability(
                floors['opportunity_probability'],
                field=f'{segment.value} opportunity floor',
            ),
            path_floor=_probability(
                floors['path_probability'],
                field=f'{segment.value} path floor',
            ),
            economics_floor_percent=_finite_float(
                floors['expected_net_return_percent'],
                field=f'{segment.value} economics floor',
            ),
            opportunity_features=opportunity_features,
            path_features=path_features,
            economics_features=economics_features,
            opportunity=ManagedV2ProbabilityComponent.from_dict(
                value['opportunity'],
                expected_version=MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
                segment=segment,
                component='opportunity',
            ),
            path=ManagedV2ProbabilityComponent.from_dict(
                value['path'],
                expected_version=MANAGED_V2_PATH_MODEL_VERSION,
                segment=segment,
                component='path',
            ),
            economics=ManagedV2EconomicsComponent.from_dict(
                value['economics'],
                segment=segment,
            ),
        )
        _validate_model_feature_terms(
            result.opportunity.model,
            allowed=opportunity_features,
            segment=segment,
            component='opportunity',
        )
        _validate_model_feature_terms(
            result.path.model,
            allowed=path_features,
            segment=segment,
            component='path',
        )
        _validate_model_feature_terms(
            result.economics.model,
            allowed=economics_features,
            segment=segment,
            component='economics',
        )
        return result


@dataclass(frozen=True)
class FrozenManagedV2Model:
    version: str
    feature_contract_version: str
    label_contract_version: str
    selection_policy_version: str
    training_asset_classes: tuple[str, ...]
    supported_segments: tuple[StrategySegment, ...]
    segments: Mapping[StrategySegment, ManagedV2SegmentModel]
    provenance: Mapping[str, Any]
    artifact_sha256: str

    @classmethod
    def load(cls) -> FrozenManagedV2Model:
        model_root = files('app.execution.scoring').joinpath(
            'models/managed_v2'
        )
        manifest_raw = model_root.joinpath('manifest.json').read_bytes()
        manifest = json.loads(manifest_raw)
        if manifest.get('schema_version') != MANAGED_V2_ARTIFACT_SCHEMA_VERSION:
            raise RuntimeError('MANAGED V2 artifact schema version mismatch.')
        version = str(manifest['version'])
        feature_contract = str(manifest['feature_contract_version'])
        label_contract = str(manifest['label_contract_version'])
        selection_policy = str(manifest['selection_policy_version'])
        training_assets = tuple(manifest['training_asset_classes'])
        supported = tuple(
            StrategySegment(str(item))
            for item in manifest['supported_segments']
        )
        if version != MANAGED_V2_MODEL_VERSION:
            raise RuntimeError('MANAGED V2 aggregate model version mismatch.')
        if feature_contract != MANAGED_V2_FEATURE_CONTRACT_VERSION:
            raise RuntimeError('MANAGED V2 feature contract mismatch.')
        if label_contract != MANAGED_V2_LABEL_CONTRACT_VERSION:
            raise RuntimeError('MANAGED V2 label contract mismatch.')
        if selection_policy != MANAGED_V2_SELECTION_POLICY_VERSION:
            raise RuntimeError('MANAGED V2 selection policy mismatch.')
        if training_assets != MANAGED_V2_TRAINING_ASSET_CLASSES:
            raise RuntimeError('MANAGED V2 training domain mismatch.')
        if supported != MANAGED_V2_SUPPORTED_SEGMENTS:
            raise RuntimeError('MANAGED V2 supported segment mismatch.')
        segment_files = manifest.get('segment_files')
        if not isinstance(segment_files, Mapping):
            raise TypeError('MANAGED V2 segment file map is missing.')
        if set(segment_files) != {item.value for item in supported}:
            raise RuntimeError('MANAGED V2 segment file map is incomplete.')

        digest = sha256()
        digest.update(manifest_raw)
        segments: dict[StrategySegment, ManagedV2SegmentModel] = {}
        for segment in supported:
            segment_raw = model_root.joinpath(
                str(segment_files[segment.value])
            ).read_bytes()
            digest.update(segment_raw)
            segments[segment] = ManagedV2SegmentModel.from_dict(
                segment,
                json.loads(segment_raw),
            )
        provenance = manifest.get('provenance')
        _validate_provenance(provenance)
        return cls(
            version=version,
            feature_contract_version=feature_contract,
            label_contract_version=label_contract,
            selection_policy_version=selection_policy,
            training_asset_classes=training_assets,
            supported_segments=supported,
            segments=segments,
            provenance=provenance,
            artifact_sha256=digest.hexdigest(),
        )

    def segment_for(self, segment: StrategySegment) -> ManagedV2SegmentModel:
        try:
            return self.segments[segment]
        except KeyError as exc:
            raise RuntimeError(
                f'No MANAGED V2 model for {segment.value}.'
            ) from exc


@dataclass(frozen=True)
class ManagedV2Estimate:
    opportunity_probability: float
    path_probability: float
    expected_net_return_percent: float
    ranking_score: float
    metadata: Mapping[str, Any]


class ManagedV2Estimator:
    def __init__(self, model: FrozenManagedV2Model | None = None) -> None:
        self.model = model or _default_model()

    def estimate(
        self,
        evaluated_candidate: EvaluatedTradeCandidate,
    ) -> ManagedV2Estimate:
        feature_set = extract_managed_v2_features(evaluated_candidate)
        segment = self.model.segment_for(feature_set.segment)
        opportunity_raw, opportunity = segment.opportunity.predict(
            feature_set.opportunity
        )
        path_raw, path = segment.path.predict(feature_set.path)
        economics_features = feature_set.economics(
            opportunity_probability=opportunity,
            path_probability=path,
        )
        economics_raw = segment.economics.model.predict(economics_features)
        economics = _clamp_economics(economics_raw, evaluated_candidate)
        economics_margin = economics - segment.economics_floor_percent
        ranking_score = opportunity * path * max(0.0, economics_margin)
        shadow_rejection_reason = _shadow_rejection_reason(
            opportunity=opportunity,
            path=path,
            economics=economics,
            segment=segment,
        )
        metadata = {
            'segment': segment.segment.value,
            'training_status': segment.training_status,
            'selection_policy_version': self.model.selection_policy_version,
            'feature_contract_version': self.model.feature_contract_version,
            'label_contract_version': self.model.label_contract_version,
            'aggregate_model_version': self.model.version,
            'opportunity_model_version': segment.opportunity.version,
            'path_model_version': segment.path.version,
            'economics_model_version': segment.economics.version,
            'artifact_sha256': self.model.artifact_sha256,
            'deployment_status': MANAGED_V2_DEPLOYMENT_STATUS,
            'gate_outcome': (
                'eligible' if shadow_rejection_reason is None else 'rejected'
            ),
            'gate_rejection_reason': shadow_rejection_reason,
            'shadow_selection_outcome': (
                'selected' if shadow_rejection_reason is None else 'rejected'
            ),
            'shadow_selection_reason': shadow_rejection_reason,
            'floors': {
                'policy_version': MANAGED_V2_FLOOR_POLICY_VERSION,
                'opportunity_probability': segment.opportunity_floor,
                'path_probability': segment.path_floor,
                'expected_net_return_percent': (
                    segment.economics_floor_percent
                ),
            },
            'opportunity': {
                'raw_probability': opportunity_raw,
                'probability': opportunity,
                'prior': segment.opportunity.prior,
                'features': dict(feature_set.opportunity),
                'missing_features': missing_feature_names(
                    feature_set.opportunity
                ),
            },
            'path': {
                'raw_probability': path_raw,
                'probability': path,
                'prior': segment.path.prior,
                'features': dict(feature_set.path),
                'missing_features': missing_feature_names(feature_set.path),
            },
            'economics': {
                'raw_expected_net_return_percent': economics_raw,
                'expected_net_return_percent': economics,
                'features': economics_features,
                'missing_features': missing_feature_names(
                    economics_features
                ),
            },
            'ranking_score': ranking_score,
        }
        return ManagedV2Estimate(
            opportunity_probability=opportunity,
            path_probability=path,
            expected_net_return_percent=economics,
            ranking_score=ranking_score,
            metadata=metadata,
        )


class CandidateManagedV2Evaluator:
    def __init__(self, estimator: ManagedV2Estimator | None = None) -> None:
        self.estimator = estimator or ManagedV2Estimator()

    def evaluate(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
    ) -> EvaluatedTradeCandidate:
        estimate = self.estimator.estimate(evaluated_candidate)
        candidate = replace(
            evaluated_candidate.candidate,
            managed_v2_opportunity_probability=(
                estimate.opportunity_probability
            ),
            managed_v2_path_probability=estimate.path_probability,
            managed_v2_expected_net_return_percent=(
                estimate.expected_net_return_percent
            ),
            managed_v2_ranking_score=estimate.ranking_score,
            managed_v2_model_version=self.estimator.model.version,
            managed_v2_metadata=dict(estimate.metadata),
        )
        return replace(evaluated_candidate, candidate=candidate)


def _clamp_economics(
    value: float,
    evaluated: EvaluatedTradeCandidate,
) -> float:
    economics = evaluated.economics
    lower = -(
        max(0.0, economics.effective_stop_loss_percent)
        + max(0.0, economics.estimated_total_cost_percent)
    )
    upper = max(
        lower,
        economics.effective_take_profit_percent
        - max(0.0, economics.estimated_total_cost_percent),
    )
    return min(upper, max(lower, value))


def _shadow_rejection_reason(
    *,
    opportunity: float,
    path: float,
    economics: float,
    segment: ManagedV2SegmentModel,
) -> str | None:
    if opportunity < segment.opportunity_floor:
        return 'candidate_selection_opportunity_below_floor'
    if path < segment.path_floor:
        return 'candidate_selection_path_below_floor'
    if economics < segment.economics_floor_percent:
        return 'candidate_selection_economics_below_floor'
    return None


def _validate_model_feature_terms(
    model: FrozenLogisticModel | FrozenLinearModel,
    *,
    allowed: tuple[str, ...],
    segment: StrategySegment,
    component: str,
) -> None:
    _finite_float(
        model.intercept,
        field=f'{segment.value} {component} intercept',
    )
    if not all(isinstance(term, Mapping) for term in model.numeric_terms):
        raise RuntimeError(
            f'MANAGED V2 {component} numeric terms are invalid for '
            f'{segment.value}.'
        )
    if not all(
        isinstance(term, Mapping)
        for term in model.missing_indicator_terms
    ):
        raise RuntimeError(
            f'MANAGED V2 {component} missing-indicator terms are invalid for '
            f'{segment.value}.'
        )
    numeric_names = tuple(
        str(term.get('feature')) for term in model.numeric_terms
    )
    if numeric_names != allowed:
        raise RuntimeError(
            f'MANAGED V2 {component} model feature terms do not match the '
            f'{segment.value} contract.'
        )
    missing_names = tuple(
        str(term.get('feature')) for term in model.missing_indicator_terms
    )
    if len(missing_names) != len(set(missing_names)) or any(
        name not in allowed for name in missing_names
    ):
        raise RuntimeError(
            f'MANAGED V2 {component} missing-indicator terms do not match the '
            f'{segment.value} contract.'
        )
    if getattr(model, 'categorical_terms', ()):
        raise RuntimeError(
            f'MANAGED V2 {component} categorical terms are unsupported for '
            f'{segment.value}.'
        )
    for term in model.numeric_terms:
        _validate_numeric_term(
            term,
            segment=segment,
            component=component,
            missing_indicator=False,
        )
    for term in model.missing_indicator_terms:
        _validate_numeric_term(
            term,
            segment=segment,
            component=component,
            missing_indicator=True,
        )


def _validate_provenance(value: object) -> None:
    if not isinstance(value, Mapping):
        raise TypeError('MANAGED V2 artifact provenance is missing.')
    required = {
        'training_dates',
        'training_rows',
        'dataset_sha256',
        'source_provenance_sha256',
        'labels',
        'regularization',
        'quote_quality_contract',
        'automatic_retuning',
        'continuous_training',
    }
    missing = sorted(required - set(value))
    if missing:
        raise RuntimeError(
            f'MANAGED V2 artifact provenance is incomplete: {missing}.'
        )
    if value['automatic_retuning'] is not False:
        raise RuntimeError('MANAGED V2 automatic retuning must be disabled.')
    if value['continuous_training'] is not False:
        raise RuntimeError('MANAGED V2 continuous training must be disabled.')
    _row_count(
        value['training_rows'],
        field='artifact provenance training_rows',
        positive=True,
    )
    if value['quote_quality_contract'] != quote_quality_contract_metadata():
        raise RuntimeError(
            'MANAGED V2 artifact quote quality contract mismatch.'
        )


def _validate_numeric_term(
    term: Mapping[str, Any],
    *,
    segment: StrategySegment,
    component: str,
    missing_indicator: bool,
) -> None:
    term_kind = 'missing-indicator' if missing_indicator else 'numeric'
    required = {'feature', 'mean', 'scale', 'coefficient'}
    if not missing_indicator:
        required.add('impute')
    missing = sorted(required - set(term))
    if missing:
        raise RuntimeError(
            f'MANAGED V2 {component} {term_kind} term is incomplete for '
            f'{segment.value}: {missing}.'
        )
    for name in sorted(required - {'feature'}):
        value = _finite_float(
            term[name],
            field=(
                f'{segment.value} {component} {term_kind} '
                f'{term["feature"]} {name}'
            ),
        )
        if name == 'scale' and value <= 0:
            raise RuntimeError(
                f'Invalid MANAGED V2 {component} {term_kind} scale for '
                f'{segment.value}:{term["feature"]}.'
            )


def _finite_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f'Invalid MANAGED V2 numeric value for {field}.')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f'Invalid MANAGED V2 numeric value for {field}.'
        ) from exc
    if not math.isfinite(result):
        raise RuntimeError(
            f'Non-finite MANAGED V2 numeric value for {field}.'
        )
    return result


def _probability(value: Any, *, field: str) -> float:
    result = _finite_float(value, field=field)
    if not 0.0 <= result <= 1.0:
        raise RuntimeError(f'Invalid MANAGED V2 probability for {field}.')
    return result


def _row_count(value: Any, *, field: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'Invalid MANAGED V2 row count for {field}.')
    if value < (1 if positive else 0):
        raise RuntimeError(f'Invalid MANAGED V2 row count for {field}.')
    return value


def _numeric_term_score(
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


def _missing_term_score(
    term: Mapping[str, Any],
    features: Mapping[str, Any],
) -> float:
    value = 1.0 if _number(features.get(str(term['feature']))) is None else 0.0
    scale = float(term['scale'])
    if scale <= 0:
        raise RuntimeError(
            f"Invalid missing-indicator scale for {term['feature']}."
        )
    return (
        (value - float(term['mean']))
        / scale
        * float(term['coefficient'])
    )


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@lru_cache(maxsize=1)
def _default_model() -> FrozenManagedV2Model:
    return FrozenManagedV2Model.load()
