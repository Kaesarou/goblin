from __future__ import annotations

import base64
import gzip
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from importlib.resources import files
from typing import Any

from app.execution.scoring.outcome_probability_model_contract import (
    OUTCOME_PROBABILITY_CALIBRATION_POLICY_VERSION,
)


@dataclass(frozen=True)
class FrozenLogisticModel:
    intercept: float
    numeric_terms: tuple[Mapping[str, Any], ...]
    missing_indicator_terms: tuple[Mapping[str, Any], ...]
    categorical_terms: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FrozenLogisticModel:
        return cls(
            intercept=float(value['intercept']),
            numeric_terms=tuple(value.get('numeric', ())),
            missing_indicator_terms=tuple(
                value.get('missing_indicators', ())
            ),
            categorical_terms=tuple(value.get('categorical', ())),
        )

    def predict(self, features: Mapping[str, Any]) -> float:
        logit = self.intercept
        for term in self.numeric_terms:
            raw_value = features.get(str(term['feature']))
            missing = _is_missing(raw_value)
            value = (
                float(term['impute'])
                if missing
                else float(raw_value)
            )
            scale = float(term['scale'])
            if scale <= 0:
                raise RuntimeError(
                    f"Invalid frozen scale for {term['feature']}: {scale}."
                )
            logit += (
                (value - float(term['mean']))
                / scale
                * float(term['coefficient'])
            )
        for term in self.missing_indicator_terms:
            value = 1.0 if _is_missing(
                features.get(str(term['feature']))
            ) else 0.0
            scale = float(term['scale'])
            if scale <= 0:
                raise RuntimeError(
                    f"Invalid frozen missing-indicator scale for "
                    f"{term['feature']}: {scale}."
                )
            logit += (
                (value - float(term['mean']))
                / scale
                * float(term['coefficient'])
            )
        for term in self.categorical_terms:
            raw_value = features.get(str(term['feature']))
            value = (
                str(term['impute'])
                if _is_missing(raw_value)
                else str(raw_value)
            )
            coefficients = {
                str(item['category']): float(item['coefficient'])
                for item in term.get('categories', ())
            }
            coefficient = coefficients.get(value)
            if coefficient is None and value in {
                str(item)
                for item in term.get('infrequent_categories', ())
            }:
                raw_coefficient = term.get('infrequent_coefficient')
                coefficient = (
                    None
                    if raw_coefficient is None
                    else float(raw_coefficient)
                )
            if coefficient is not None:
                logit += coefficient
        return _sigmoid(logit)


@dataclass(frozen=True)
class FrozenDirectionSegmentModel:
    segment: str
    feature_family: str
    training_status: str
    source_segment: str | None
    training_rows: int
    segment_prior: float
    model_weight: float
    segment_prior_weight: float
    model: FrozenLogisticModel

    @classmethod
    def from_dict(
        cls,
        segment: str,
        value: Mapping[str, Any],
    ) -> FrozenDirectionSegmentModel:
        model_weight = float(value['model_weight'])
        prior_weight = float(value['segment_prior_weight'])
        _validate_calibration_weights(
            segment=segment,
            model_weight=model_weight,
            prior_weight=prior_weight,
        )
        return cls(
            segment=segment,
            feature_family=str(value['feature_family']),
            training_status=str(value['training_status']),
            source_segment=(
                None
                if value.get('source_segment') is None
                else str(value['source_segment'])
            ),
            training_rows=int(value['training_rows']),
            segment_prior=float(value['segment_prior']),
            model_weight=model_weight,
            segment_prior_weight=prior_weight,
            model=FrozenLogisticModel.from_dict(value['model']),
        )

    def with_calibration(
        self,
        *,
        model_weight: float,
        segment_prior_weight: float,
    ) -> FrozenDirectionSegmentModel:
        _validate_calibration_weights(
            segment=self.segment,
            model_weight=model_weight,
            prior_weight=segment_prior_weight,
        )
        return replace(
            self,
            model_weight=model_weight,
            segment_prior_weight=segment_prior_weight,
        )

    def predict(self, features: Mapping[str, Any]) -> tuple[float, float]:
        raw = self.model.predict(features)
        calibrated = (
            self.model_weight * raw
            + self.segment_prior_weight * self.segment_prior
        )
        return raw, min(1.0, max(0.0, calibrated))


@dataclass(frozen=True)
class FrozenOutcomeProbabilityModel:
    version: str
    feature_contract_version: str
    training_asset_classes: tuple[str, ...]
    supported_segments: tuple[str, ...]
    activity: FrozenLogisticModel
    direction_segments: Mapping[str, FrozenDirectionSegmentModel]
    provenance: Mapping[str, Any]
    artifact_sha256: str | None = None
    calibration_policy_version: str | None = None
    calibration_policy_sha256: str | None = None

    @classmethod
    def load(cls) -> FrozenOutcomeProbabilityModel:
        scoring_files = files('app.execution.scoring')
        model_path = scoring_files.joinpath(
            'models/outcome_probability_v2.json'
        )
        raw = model_path.read_bytes()
        wrapper = json.loads(raw)
        if wrapper.get('encoding') == 'gzip+base64':
            decoded = gzip.decompress(
                base64.b64decode(wrapper['payload'])
            )
            value = json.loads(decoded)
        else:
            value = wrapper
        supported_segments = tuple(
            str(item) for item in value['supported_segments']
        )
        direction_segments = {
            str(segment): FrozenDirectionSegmentModel.from_dict(
                str(segment),
                segment_value,
            )
            for segment, segment_value
            in value['direction_segments'].items()
        }
        policy_version, policy_sha256, calibration = (
            _load_calibration_policy(
                scoring_files=scoring_files,
                supported_segments=supported_segments,
            )
        )
        effective_segments = {
            segment: direction_segments[segment].with_calibration(
                model_weight=calibration[segment][0],
                segment_prior_weight=calibration[segment][1],
            )
            for segment in supported_segments
        }
        return cls(
            version=str(value['version']),
            feature_contract_version=str(
                value['feature_contract_version']
            ),
            training_asset_classes=tuple(
                str(item)
                for item in value['training_asset_classes']
            ),
            supported_segments=supported_segments,
            activity=FrozenLogisticModel.from_dict(value['activity']),
            direction_segments=effective_segments,
            provenance=value['provenance'],
            artifact_sha256=sha256(raw).hexdigest(),
            calibration_policy_version=policy_version,
            calibration_policy_sha256=policy_sha256,
        )

    def direction_for(
        self,
        *,
        asset_class: str,
        side: str,
    ) -> FrozenDirectionSegmentModel:
        segment = f'{asset_class.strip().upper()}_{side.strip().upper()}'
        try:
            return self.direction_segments[segment]
        except KeyError as exc:
            raise RuntimeError(
                f'No frozen direction model for segment {segment}.'
            ) from exc


def _load_calibration_policy(
    *,
    scoring_files: Any,
    supported_segments: tuple[str, ...],
) -> tuple[str, str, dict[str, tuple[float, float]]]:
    policy_path = scoring_files.joinpath(
        'models/outcome_probability_calibration_v2.json'
    )
    raw = policy_path.read_bytes()
    value = json.loads(raw)
    version = str(value.get('version', ''))
    if version != OUTCOME_PROBABILITY_CALIBRATION_POLICY_VERSION:
        raise RuntimeError(
            'Outcome probability calibration policy version does not match '
            'code.'
        )
    raw_segments = value.get('segments')
    if not isinstance(raw_segments, Mapping):
        raise RuntimeError(
            'Outcome probability calibration policy has no segment mapping.'
        )
    configured_segments = {str(item) for item in raw_segments}
    expected_segments = set(supported_segments)
    if configured_segments != expected_segments:
        raise RuntimeError(
            'Outcome probability calibration policy segments do not match '
            'the frozen model.'
        )
    calibration: dict[str, tuple[float, float]] = {}
    for segment in supported_segments:
        segment_value = raw_segments[segment]
        if not isinstance(segment_value, Mapping):
            raise RuntimeError(
                f'Invalid calibration policy entry for {segment}.'
            )
        model_weight = float(segment_value['model_weight'])
        prior_weight = float(segment_value['segment_prior_weight'])
        _validate_calibration_weights(
            segment=segment,
            model_weight=model_weight,
            prior_weight=prior_weight,
        )
        calibration[segment] = (model_weight, prior_weight)
    return version, sha256(raw).hexdigest(), calibration


def _validate_calibration_weights(
    *,
    segment: str,
    model_weight: float,
    prior_weight: float,
) -> None:
    if model_weight < 0.0 or prior_weight < 0.0:
        raise RuntimeError(
            f'Direction calibration weights must be non-negative for '
            f'{segment}.'
        )
    if not math.isclose(
        model_weight + prior_weight,
        1.0,
        abs_tol=1e-12,
    ):
        raise RuntimeError(
            f'Direction calibration weights do not sum to one for '
            f'{segment}.'
        )


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return False


def _sigmoid(logit: float) -> float:
    if logit >= 0:
        exponential = math.exp(-logit)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(logit)
    return exponential / (1.0 + exponential)
