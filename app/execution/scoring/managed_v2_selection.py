from __future__ import annotations

import math
from dataclasses import dataclass

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.scoring.managed_v2 import CandidateManagedV2Evaluator
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_DEPLOYMENT_STATUS,
    MANAGED_V2_ECONOMICS_MODEL_VERSION,
    MANAGED_V2_FEATURE_CONTRACT_VERSION,
    MANAGED_V2_FLOOR_POLICY_VERSION,
    MANAGED_V2_LABEL_CONTRACT_VERSION,
    MANAGED_V2_MODEL_VERSION,
    MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
    MANAGED_V2_PATH_MODEL_VERSION,
    MANAGED_V2_SELECTION_POLICY_VERSION,
)
from app.execution.trade_candidate import TradeCandidate


@dataclass(frozen=True)
class ManagedV2GateRejection:
    evaluated_candidate: EvaluatedTradeCandidate
    reason: str
    threshold_source: str
    opportunity_floor: float
    path_floor: float
    economics_floor_percent: float


def is_managed_v2_equity_candidate(
    selection_policy_version: str,
    candidate: TradeCandidate,
) -> bool:
    return (
        selection_policy_version == MANAGED_V2_SELECTION_POLICY_VERSION
        and candidate.segment is not None
        and candidate.segment.is_equity
    )


def ensure_managed_v2_estimate(
    evaluated_candidate: EvaluatedTradeCandidate,
    evaluator: CandidateManagedV2Evaluator,
) -> EvaluatedTradeCandidate:
    candidate = evaluated_candidate.candidate
    metadata = candidate.managed_v2_metadata
    any_existing = any(
        value is not None
        for value in (
            candidate.managed_v2_model_version,
            candidate.managed_v2_opportunity_probability,
            candidate.managed_v2_path_probability,
            candidate.managed_v2_expected_net_return_percent,
            candidate.managed_v2_ranking_score,
        )
    ) or bool(metadata)
    if not any_existing:
        return evaluator.evaluate(evaluated_candidate=evaluated_candidate)

    if candidate.managed_v2_model_version != MANAGED_V2_MODEL_VERSION:
        raise RuntimeError('MANAGED V2 candidate model version mismatch.')
    if (
        metadata.get('feature_contract_version')
        != MANAGED_V2_FEATURE_CONTRACT_VERSION
    ):
        raise RuntimeError('MANAGED V2 candidate feature contract mismatch.')
    if (
        metadata.get('label_contract_version')
        != MANAGED_V2_LABEL_CONTRACT_VERSION
    ):
        raise RuntimeError('MANAGED V2 candidate label contract mismatch.')
    expected_metadata = {
        'selection_policy_version': MANAGED_V2_SELECTION_POLICY_VERSION,
        'aggregate_model_version': MANAGED_V2_MODEL_VERSION,
        'opportunity_model_version': MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
        'path_model_version': MANAGED_V2_PATH_MODEL_VERSION,
        'economics_model_version': MANAGED_V2_ECONOMICS_MODEL_VERSION,
        'artifact_sha256': evaluator.estimator.model.artifact_sha256,
        'deployment_status': MANAGED_V2_DEPLOYMENT_STATUS,
    }
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            raise RuntimeError(f'MANAGED V2 candidate {key} mismatch.')
    if candidate.segment is None:
        raise RuntimeError('MANAGED V2 candidate segment is missing.')
    if metadata.get('segment') != candidate.segment.value:
        raise RuntimeError('MANAGED V2 candidate segment mismatch.')
    floors = metadata.get('floors')
    if not isinstance(floors, dict):
        raise TypeError('MANAGED V2 candidate floors are missing.')
    segment_model = evaluator.estimator.model.segment_for(candidate.segment)
    expected_floors = {
        'policy_version': MANAGED_V2_FLOOR_POLICY_VERSION,
        'opportunity_probability': segment_model.opportunity_floor,
        'path_probability': segment_model.path_floor,
        'expected_net_return_percent': (
            segment_model.economics_floor_percent
        ),
    }
    if floors != expected_floors:
        raise RuntimeError('MANAGED V2 candidate floor contract mismatch.')
    if any(
        value is None
        for value in (
            candidate.managed_v2_opportunity_probability,
            candidate.managed_v2_path_probability,
            candidate.managed_v2_expected_net_return_percent,
            candidate.managed_v2_ranking_score,
        )
    ):
        raise RuntimeError('MANAGED V2 candidate estimate is incomplete.')
    _validate_estimate_values(candidate)
    return evaluated_candidate


def managed_v2_gate_rejection(
    evaluated_candidate: EvaluatedTradeCandidate,
) -> ManagedV2GateRejection | None:
    candidate = evaluated_candidate.candidate
    floors = candidate.managed_v2_metadata.get('floors')
    if not isinstance(floors, dict):
        raise TypeError('MANAGED V2 candidate floors are missing.')
    opportunity_floor = float(floors['opportunity_probability'])
    path_floor = float(floors['path_probability'])
    economics_floor = float(floors['expected_net_return_percent'])
    values = (
        candidate.managed_v2_opportunity_probability,
        candidate.managed_v2_path_probability,
        candidate.managed_v2_expected_net_return_percent,
    )
    if any(value is None for value in values):
        raise RuntimeError('MANAGED V2 candidate estimate is incomplete.')
    _validate_estimate_values(candidate)
    opportunity, path, economics = values
    reason_and_source = None
    if opportunity < opportunity_floor:
        reason_and_source = (
            'candidate_selection_opportunity_below_floor',
            'managed_v2_opportunity_gate',
        )
    elif path < path_floor:
        reason_and_source = (
            'candidate_selection_path_below_floor',
            'managed_v2_path_gate',
        )
    elif economics < economics_floor:
        reason_and_source = (
            'candidate_selection_economics_below_floor',
            'managed_v2_economics_gate',
        )
    if reason_and_source is None:
        return None
    reason, threshold_source = reason_and_source
    return ManagedV2GateRejection(
        evaluated_candidate=evaluated_candidate,
        reason=reason,
        threshold_source=threshold_source,
        opportunity_floor=opportunity_floor,
        path_floor=path_floor,
        economics_floor_percent=economics_floor,
    )


def managed_v2_floor(
    evaluated_candidate: EvaluatedTradeCandidate,
    key: str,
) -> float | None:
    floors = evaluated_candidate.candidate.managed_v2_metadata.get('floors')
    if not isinstance(floors, dict):
        return None
    value = floors.get(key)
    return None if value is None else float(value)


def managed_v2_ranking_key(
    evaluated_candidate: EvaluatedTradeCandidate,
) -> tuple[float, float, float, float, str]:
    candidate = evaluated_candidate.candidate
    return (
        -_ranking_value(candidate.managed_v2_ranking_score),
        -_ranking_value(candidate.managed_v2_opportunity_probability),
        -_ranking_value(candidate.managed_v2_path_probability),
        -_ranking_value(candidate.managed_v2_expected_net_return_percent),
        candidate.candidate_id,
    )


def _ranking_value(value: float | None) -> float:
    return float('-inf') if value is None else float(value)


def _validate_estimate_values(candidate: TradeCandidate) -> None:
    probabilities = {
        'opportunity': candidate.managed_v2_opportunity_probability,
        'path': candidate.managed_v2_path_probability,
    }
    for name, value in probabilities.items():
        if value is None or not math.isfinite(float(value)):
            raise RuntimeError(
                f'MANAGED V2 candidate {name} probability is non-finite.'
            )
        if not 0.0 <= float(value) <= 1.0:
            raise RuntimeError(
                f'MANAGED V2 candidate {name} probability is invalid.'
            )
    economics = candidate.managed_v2_expected_net_return_percent
    ranking = candidate.managed_v2_ranking_score
    if economics is None or not math.isfinite(float(economics)):
        raise RuntimeError('MANAGED V2 candidate economics is non-finite.')
    if ranking is None or not math.isfinite(float(ranking)) or ranking < 0:
        raise RuntimeError('MANAGED V2 candidate ranking score is invalid.')
