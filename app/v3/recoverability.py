from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Mapping

from app.v3.models import RecoverabilityAssessment


@dataclass(frozen=True)
class LinearRecoverabilityArtifact:
    model_version: str
    feature_manifest_version: str
    target_recovery_pct: float
    adverse_barrier_pct: float
    horizon_minutes: int
    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    score_quantiles: tuple[float, ...]

    @classmethod
    def from_json(cls, path: str | Path) -> "LinearRecoverabilityArtifact":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "LinearRecoverabilityArtifact":
        return cls(
            model_version=str(payload["model_version"]),
            feature_manifest_version=str(payload["feature_manifest_version"]),
            target_recovery_pct=float(payload["target_recovery_pct"]),
            adverse_barrier_pct=float(payload["adverse_barrier_pct"]),
            horizon_minutes=int(payload["horizon_minutes"]),
            features=tuple(str(value) for value in payload["features"]),
            mean=tuple(float(value) for value in payload["mean"]),
            scale=tuple(float(value) for value in payload["scale"]),
            coefficients=tuple(float(value) for value in payload["coefficients"]),
            intercept=float(payload["intercept"]),
            score_quantiles=tuple(float(value) for value in payload["score_quantiles"]),
        )


class RecoverabilityScorer:
    def __init__(self, artifact: LinearRecoverabilityArtifact) -> None:
        self.artifact = artifact

    @classmethod
    def from_default_artifact(cls) -> "RecoverabilityScorer":
        resource = files("app.v3.artifacts").joinpath("recoverability_long_logit_v1.json")
        payload = json.loads(resource.read_text(encoding="utf-8"))
        return cls(LinearRecoverabilityArtifact.from_payload(payload))

    def score(self, features: Mapping[str, float], *, asof) -> RecoverabilityAssessment:
        artifact = self.artifact
        for name in artifact.features:
            value = features.get(name)
            if value is None or not math.isfinite(float(value)):
                return RecoverabilityAssessment(
                    model_version=artifact.model_version,
                    feature_manifest_version=artifact.feature_manifest_version,
                    raw_score=float("nan"),
                    rank_quantile=0.0,
                    target_recovery_pct=artifact.target_recovery_pct,
                    adverse_barrier_pct=artifact.adverse_barrier_pct,
                    horizon_minutes=artifact.horizon_minutes,
                    asof=asof,
                    valid=False,
                    invalid_reason=f"missing_or_nonfinite:{name}",
                )

        raw_score = artifact.intercept + sum(
            coefficient * ((float(features[name]) - mean) / scale)
            for name, mean, scale, coefficient in zip(
                artifact.features,
                artifact.mean,
                artifact.scale,
                artifact.coefficients,
                strict=True,
            )
        )
        index = bisect.bisect_right(artifact.score_quantiles, raw_score)
        if index <= 0:
            rank_quantile = 0.0
        elif index >= len(artifact.score_quantiles):
            rank_quantile = 1.0
        else:
            rank_quantile = (index - 1) / (len(artifact.score_quantiles) - 1)

        return RecoverabilityAssessment(
            model_version=artifact.model_version,
            feature_manifest_version=artifact.feature_manifest_version,
            raw_score=raw_score,
            rank_quantile=rank_quantile,
            target_recovery_pct=artifact.target_recovery_pct,
            adverse_barrier_pct=artifact.adverse_barrier_pct,
            horizon_minutes=artifact.horizon_minutes,
            asof=asof,
        )
