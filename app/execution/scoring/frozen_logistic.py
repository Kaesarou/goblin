from __future__ import annotations

import base64
import gzip
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Any


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
        if not math.isclose(
            model_weight + prior_weight,
            1.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                f'Direction calibration weights do not sum to one for '
                f'{segment}.'
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

    @classmethod
    def load(cls) -> FrozenOutcomeProbabilityModel:
        model_path = files('app.execution.scoring').joinpath(
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
        direction_segments = {
            str(segment): FrozenDirectionSegmentModel.from_dict(
                str(segment),
                segment_value,
            )
            for segment, segment_value
            in value['direction_segments'].items()
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
            supported_segments=tuple(
                str(item) for item in value['supported_segments']
            ),
            activity=FrozenLogisticModel.from_dict(value['activity']),
            direction_segments=direction_segments,
            provenance=value['provenance'],
            artifact_sha256=sha256(raw).hexdigest(),
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
