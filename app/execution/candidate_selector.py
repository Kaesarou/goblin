from dataclasses import dataclass, replace

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.candidate_ranking import rank_trade_candidates
from app.execution.candidate_readiness import CandidateReadiness
from app.execution.entry_decision import EntryAction, EntryDecisionEngine
from app.execution.scoring.managed_outcome import (
    CandidateManagedOutcomeEvaluator,
)
from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_OUTCOME_MODEL_VERSION,
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.execution.scoring.managed_v2 import CandidateManagedV2Evaluator
from app.execution.scoring.managed_v2_model_contract import (
    ACTIVE_EQUITY_SELECTION_POLICY_VERSION,
    MANAGED_V2_SELECTION_POLICY_VERSION,
)
from app.execution.scoring.managed_v2_selection import (
    ensure_managed_v2_estimate,
    is_managed_v2_equity_candidate,
    managed_v2_floor,
    managed_v2_gate_rejection,
    managed_v2_ranking_key,
)
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import EntryDecisionConfig


@dataclass(frozen=True)
class CandidateSelectionConfig:
    top_n: int


@dataclass(frozen=True)
class RejectedCandidateSelection:
    candidate: TradeCandidate
    reason: str


@dataclass(frozen=True)
class RejectedEvaluatedCandidateSelection:
    evaluated_candidate: EvaluatedTradeCandidate
    reason: str
    minimum_managed_protection_probability_used: float | None = None
    minimum_managed_positive_probability_used: float | None = None
    minimum_managed_expected_net_return_percent_used: float | None = None
    minimum_opportunity_probability_used: float | None = None
    minimum_path_probability_used: float | None = None
    minimum_economics_return_percent_used: float | None = None
    selection_threshold_source: str | None = None


@dataclass(frozen=True)
class CandidateSelectionResult:
    selected_candidates: list[TradeCandidate]
    rejected_candidates: list[RejectedCandidateSelection]


@dataclass(frozen=True)
class EvaluatedCandidateSelectionResult:
    selected_candidates: list[EvaluatedTradeCandidate]
    rejected_candidates: list[RejectedEvaluatedCandidateSelection]


def select_trade_candidates(
    candidates: list[TradeCandidate],
    config: CandidateSelectionConfig,
) -> CandidateSelectionResult:
    selected_candidates = rank_trade_candidates(candidates)
    rejected_candidates: list[RejectedCandidateSelection] = []
    if config.top_n > 0 and len(selected_candidates) > config.top_n:
        kept_candidates = selected_candidates[: config.top_n]
        overflow_candidates = selected_candidates[config.top_n :]
        rejected_candidates.extend(
            RejectedCandidateSelection(
                candidate,
                'candidate_selection_outside_top_n',
            )
            for candidate in overflow_candidates
        )
        selected_candidates = kept_candidates
    return CandidateSelectionResult(
        selected_candidates,
        rejected_candidates,
    )


def select_evaluated_trade_candidates(
    evaluated_candidates: list[EvaluatedTradeCandidate],
    config: CandidateSelectionConfig,
    *,
    selection_policy_version: str = ACTIVE_EQUITY_SELECTION_POLICY_VERSION,
) -> EvaluatedCandidateSelectionResult:
    if selection_policy_version not in {
        MANAGED_V2_SELECTION_POLICY_VERSION,
        MANAGED_SELECTION_POLICY_VERSION,
    }:
        raise ValueError(
            f'Unsupported candidate selection policy: '
            f'{selection_policy_version}.'
        )
    eligible_candidates: list[EvaluatedTradeCandidate] = []
    rejected_candidates: list[RejectedEvaluatedCandidateSelection] = []
    decision_engine = EntryDecisionEngine()
    managed_evaluator: CandidateManagedOutcomeEvaluator | None = None
    managed_v2_evaluator: CandidateManagedV2Evaluator | None = None

    for original in evaluated_candidates:
        decision_config = (
            original.candidate.entry_decision_config
            or EntryDecisionConfig()
        )
        decision = original.entry_decision or decision_engine.evaluate(
            evaluated_candidate=original,
            config=decision_config,
        )
        evaluated_candidate = replace(original, entry_decision=decision)
        candidate = evaluated_candidate.candidate
        economics = evaluated_candidate.economics

        if decision.action == EntryAction.WAIT_FOR_RETEST:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason=decision.reason,
                    selection_threshold_source='entry_wait_for_retest',
                )
            )
            continue
        if decision.action == EntryAction.SKIP:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason=decision.reason,
                    selection_threshold_source='entry_skip',
                )
            )
            continue
        if evaluated_candidate.readiness == CandidateReadiness.REJECT:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason=(
                        evaluated_candidate.readiness_reason
                        or 'candidate_readiness_reject'
                    ),
                    selection_threshold_source='candidate_readiness',
                )
            )
            continue
        if candidate.tp_feasibility_hard_rejection_reason is not None:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason=candidate.tp_feasibility_hard_rejection_reason,
                    selection_threshold_source='tp_feasibility',
                )
            )
            continue
        if candidate.segment is None:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason='candidate_selection_segment_missing',
                    selection_threshold_source='segment_contract',
                )
            )
            continue
        outcome_missing = (
            candidate.touch_probability is None
            if selection_policy_version == MANAGED_V2_SELECTION_POLICY_VERSION
            and candidate.segment.is_equity
            else any(
                value is None
                for value in (
                    candidate.tp_probability,
                    candidate.touch_probability,
                    candidate.direction_probability,
                    candidate.direction_edge,
                )
            )
        )
        if outcome_missing:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason='candidate_selection_outcome_probability_missing',
                    selection_threshold_source='outcome_probability',
                )
            )
            continue
        if (
            economics.expected_net_profit_percent
            < economics.min_expected_net_profit_percent
        ):
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason=(
                        'candidate_selection_expected_profit_too_low_after_fees'
                    ),
                    selection_threshold_source='hard_economics',
                )
            )
            continue

        if (
            selection_policy_version == MANAGED_V2_SELECTION_POLICY_VERSION
            and candidate.segment.is_equity
        ):
            if managed_v2_evaluator is None:
                managed_v2_evaluator = CandidateManagedV2Evaluator()
            evaluated_candidate = ensure_managed_v2_estimate(
                evaluated_candidate,
                managed_v2_evaluator,
            )
            gate = managed_v2_gate_rejection(evaluated_candidate)
            if gate is not None:
                rejected_candidates.append(
                    RejectedEvaluatedCandidateSelection(
                        evaluated_candidate=gate.evaluated_candidate,
                        reason=gate.reason,
                        minimum_opportunity_probability_used=(
                            gate.opportunity_floor
                        ),
                        minimum_path_probability_used=gate.path_floor,
                        minimum_economics_return_percent_used=(
                            gate.economics_floor_percent
                        ),
                        selection_threshold_source=gate.threshold_source,
                    )
                )
                continue
            eligible_candidates.append(evaluated_candidate)
            continue

        if (
            candidate.managed_outcome_model_version
            != MANAGED_OUTCOME_MODEL_VERSION
            or candidate.managed_protection_probability is None
            or candidate.managed_positive_probability is None
            or candidate.managed_expected_net_return_percent is None
            or candidate.managed_edge is None
            or not candidate.managed_outcome_metadata
        ):
            if managed_evaluator is None:
                managed_evaluator = CandidateManagedOutcomeEvaluator()
            evaluated_candidate = managed_evaluator.evaluate(
                evaluated_candidate=evaluated_candidate,
            )
            candidate = evaluated_candidate.candidate
        metadata = candidate.managed_outcome_metadata
        minimum_protection = float(
            metadata['minimum_protection_probability']
        )
        minimum_positive = float(metadata['minimum_positive_probability'])
        minimum_return = float(
            metadata['minimum_expected_net_return_percent']
        )

        if candidate.managed_protection_probability is None:
            raise RuntimeError('Managed protection probability is missing.')
        if candidate.managed_protection_probability < minimum_protection:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason=(
                        'candidate_selection_managed_protection_below_floor'
                    ),
                    minimum_managed_protection_probability_used=(
                        minimum_protection
                    ),
                    minimum_managed_positive_probability_used=(
                        minimum_positive
                    ),
                    minimum_managed_expected_net_return_percent_used=(
                        minimum_return
                    ),
                    selection_threshold_source='managed_protection_gate',
                )
            )
            continue
        if candidate.managed_positive_probability is None:
            raise RuntimeError('Managed positive probability is missing.')
        if candidate.managed_positive_probability < minimum_positive:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason='candidate_selection_managed_positive_below_floor',
                    minimum_managed_protection_probability_used=(
                        minimum_protection
                    ),
                    minimum_managed_positive_probability_used=(
                        minimum_positive
                    ),
                    minimum_managed_expected_net_return_percent_used=(
                        minimum_return
                    ),
                    selection_threshold_source='managed_positive_gate',
                )
            )
            continue
        if candidate.managed_edge is None:
            raise RuntimeError('Managed edge is missing.')
        if candidate.managed_edge < 0.0:
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated_candidate,
                    reason='candidate_selection_managed_edge_below_margin',
                    minimum_managed_protection_probability_used=(
                        minimum_protection
                    ),
                    minimum_managed_positive_probability_used=(
                        minimum_positive
                    ),
                    minimum_managed_expected_net_return_percent_used=(
                        minimum_return
                    ),
                    selection_threshold_source='managed_edge_gate',
                )
            )
            continue
        eligible_candidates.append(evaluated_candidate)

    ranked_eligible = rank_evaluated_trade_candidates(
        eligible_candidates,
        selection_policy_version=selection_policy_version,
    )
    if config.top_n > 0:
        kept_candidates = ranked_eligible[: config.top_n]
        overflow_candidates = ranked_eligible[config.top_n :]
        rejected_candidates.extend(
            RejectedEvaluatedCandidateSelection(
                evaluated_candidate=item,
                reason='candidate_selection_outside_top_n',
                minimum_managed_protection_probability_used=_metadata_float(
                    item,
                    'minimum_protection_probability',
                ),
                minimum_managed_positive_probability_used=_metadata_float(
                    item,
                    'minimum_positive_probability',
                ),
                minimum_managed_expected_net_return_percent_used=(
                    _metadata_float(
                        item,
                        'minimum_expected_net_return_percent',
                    )
                ),
                selection_threshold_source=(
                    'managed_v2_ranking_top_n'
                    if is_managed_v2_equity_candidate(
                        selection_policy_version,
                        item.candidate,
                    )
                    else 'managed_edge_top_n'
                ),
                minimum_opportunity_probability_used=managed_v2_floor(
                    item,
                    'opportunity_probability',
                ),
                minimum_path_probability_used=managed_v2_floor(
                    item,
                    'path_probability',
                ),
                minimum_economics_return_percent_used=managed_v2_floor(
                    item,
                    'expected_net_return_percent',
                ),
            )
            for item in overflow_candidates
        )
    else:
        kept_candidates = ranked_eligible

    return EvaluatedCandidateSelectionResult(
        kept_candidates,
        rejected_candidates,
    )


def rank_evaluated_trade_candidates(
    evaluated_candidates: list[EvaluatedTradeCandidate],
    *,
    selection_policy_version: str = ACTIVE_EQUITY_SELECTION_POLICY_VERSION,
) -> list[EvaluatedTradeCandidate]:
    return sorted(
        evaluated_candidates,
        key=lambda item: _evaluated_candidate_ranking_key(
            item,
            selection_policy_version=selection_policy_version,
        ),
    )


def _evaluated_candidate_ranking_key(
    evaluated_candidate: EvaluatedTradeCandidate,
    *,
    selection_policy_version: str,
) -> tuple[float, float, float, float, str]:
    candidate = evaluated_candidate.candidate
    if is_managed_v2_equity_candidate(selection_policy_version, candidate):
        return managed_v2_ranking_key(evaluated_candidate)
    return (
        -_ranking_value(candidate.managed_edge),
        -_ranking_value(candidate.managed_protection_probability),
        -_ranking_value(candidate.managed_positive_probability),
        -_ranking_value(candidate.direction_edge),
        candidate.candidate_id,
    )


def _metadata_float(
    evaluated_candidate: EvaluatedTradeCandidate,
    key: str,
) -> float | None:
    value = evaluated_candidate.candidate.managed_outcome_metadata.get(key)
    return None if value is None else float(value)


def _ranking_value(value: float | None) -> float:
    return float('-inf') if value is None else float(value)
