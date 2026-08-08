from __future__ import annotations

from dataclasses import replace

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.candidate_selector import (
    EvaluatedCandidateSelectionResult,
    RejectedEvaluatedCandidateSelection,
)
from app.execution.scoring.managed_v2_model_contract import (
    ACTIVE_EQUITY_SELECTION_POLICY_VERSION,
    MANAGED_V2_DEPLOYMENT_STATUS,
    MANAGED_V2_SELECTION_POLICY_VERSION,
)
from app.risk.risk_manager import RiskManager
from app.runtime.candidate_flow import (
    select_evaluated_trade_candidates_with_strategy_profile,
)
from app.strategies.models import StrategyProfileConfig


def apply_managed_v2_shadow_selection(
    *,
    evaluated_candidates: list[EvaluatedTradeCandidate],
    risk_manager: RiskManager,
    strategy_profile: StrategyProfileConfig,
) -> tuple[
    list[EvaluatedTradeCandidate],
    EvaluatedCandidateSelectionResult,
]:
    equity_candidates = [
        item
        for item in evaluated_candidates
        if item.candidate.segment is not None
        and item.candidate.segment.is_equity
    ]
    candidate_ids = [item.candidate.candidate_id for item in equity_candidates]
    if any(not candidate_id for candidate_id in candidate_ids):
        raise RuntimeError(
            'MANAGED V2 shadow selection requires candidate IDs.'
        )
    if len(candidate_ids) != len(set(candidate_ids)):
        raise RuntimeError(
            'MANAGED V2 shadow selection candidate IDs must be unique.'
        )
    selection = select_evaluated_trade_candidates_with_strategy_profile(
        equity_candidates,
        risk_manager,
        strategy_profile,
        selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
    )
    outcomes = {
        item.candidate.candidate_id: ('selected', None)
        for item in selection.selected_candidates
    }
    outcomes.update(
        {
            item.evaluated_candidate.candidate.candidate_id: (
                'rejected',
                item.reason,
            )
            for item in selection.rejected_candidates
        }
    )
    if set(outcomes) != set(candidate_ids):
        raise RuntimeError(
            'MANAGED V2 shadow selection did not resolve every equity candidate.'
        )
    annotated = [
        _with_managed_v2_shadow_outcome(
            item,
            *outcomes[item.candidate.candidate_id],
        )
        if item.candidate.candidate_id in outcomes
        else item
        for item in evaluated_candidates
    ]
    by_id = {item.candidate.candidate_id: item for item in annotated}
    return (
        annotated,
        EvaluatedCandidateSelectionResult(
            selected_candidates=[
                by_id[item.candidate.candidate_id]
                for item in selection.selected_candidates
            ],
            rejected_candidates=[
                replace(
                    item,
                    evaluated_candidate=by_id[
                        item.evaluated_candidate.candidate.candidate_id
                    ],
                )
                for item in selection.rejected_candidates
            ],
        ),
    )


def annotate_managed_v2_shadow_shared_rejections(
    rejected_candidates: list[RejectedEvaluatedCandidateSelection],
) -> list[RejectedEvaluatedCandidateSelection]:
    return [
        replace(
            item,
            evaluated_candidate=_with_managed_v2_shadow_outcome(
                item.evaluated_candidate,
                'rejected',
                item.reason,
            ),
        )
        if item.evaluated_candidate.candidate.segment is not None
        and item.evaluated_candidate.candidate.segment.is_equity
        else item
        for item in rejected_candidates
    ]


def candidate_selection_contract_metadata(
    *,
    managed_evaluation_enabled: bool,
) -> dict[str, str | None]:
    if not managed_evaluation_enabled:
        return {
            'selection_policy_version': None,
            'managed_v2_shadow_policy_version': None,
            'managed_v2_deployment_status': None,
        }
    return {
        'selection_policy_version': ACTIVE_EQUITY_SELECTION_POLICY_VERSION,
        'managed_v2_shadow_policy_version': (
            MANAGED_V2_SELECTION_POLICY_VERSION
        ),
        'managed_v2_deployment_status': MANAGED_V2_DEPLOYMENT_STATUS,
    }


def _with_managed_v2_shadow_outcome(
    evaluated_candidate: EvaluatedTradeCandidate,
    outcome: str,
    reason: str | None,
) -> EvaluatedTradeCandidate:
    metadata = dict(evaluated_candidate.candidate.managed_v2_metadata)
    metadata['shadow_selection_outcome'] = outcome
    metadata['shadow_selection_reason'] = reason
    return replace(
        evaluated_candidate,
        candidate=replace(
            evaluated_candidate.candidate,
            managed_v2_metadata=metadata,
        ),
    )
