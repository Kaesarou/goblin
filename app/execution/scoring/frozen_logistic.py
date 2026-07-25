from __future__ import annotations

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
            logit += (
                (value - float(term['mean']))
                / float(term['scale'])
                * float(term['coefficient'])
            )
        for term in self.missing_indicator_terms:
            value = 1.0 if _is_missing(
                features.get(str(term['feature']))
            ) else 0.0
            logit += (
                (value - float(term['mean']))
                / float(term['scale'])
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
class FrozenOutcomeProbabilityModel:
    version: str
    feature_contract_version: str
    training_asset_classes: tuple[str, ...]
    activity: FrozenLogisticModel
    direction: FrozenLogisticModel
    provenance: Mapping[str, Any]
    artifact_sha256: str | None = None

    @classmethod
    def load(cls) -> FrozenOutcomeProbabilityModel:
        model_path = files('app.execution.scoring').joinpath(
            'models/pr5e_outcome_probability_v1.json'
        )
        raw = model_path.read_bytes()
        value = json.loads(raw)
        return cls(
            version=str(value['version']),
            feature_contract_version=str(
                value['feature_contract_version']
            ),
            training_asset_classes=tuple(
                str(item)
                for item in value['training_asset_classes']
            ),
            activity=FrozenLogisticModel.from_dict(value['activity']),
            direction=FrozenLogisticModel.from_dict(value['direction']),
            provenance=value['provenance'],
            artifact_sha256=sha256(raw).hexdigest(),
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
