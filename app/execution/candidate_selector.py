from dataclasses import dataclass, replace

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.candidate_ranking import rank_trade_candidates
from app.execution.candidate_readiness import CandidateReadiness
from app.execution.entry_decision import EntryAction, EntryDecisionEngine
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import EntryDecisionConfig


@dataclass(frozen=True)
class CandidateSelectionConfig:
    top_n: int
    minimum_tp_probability: float = 0.0
    maximum_touch_probability: float | None = None


@dataclass(frozen=True)
class RejectedCandidateSelection:
    candidate: TradeCandidate
    reason: str


@dataclass(frozen=True)
class RejectedEvaluatedCandidateSelection:
    evaluated_candidate: EvaluatedTradeCandidate
    reason: str
    minimum_tp_probability_used: float | None = None
    maximum_touch_probability_used: float | None = None
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
) -> EvaluatedCandidateSelectionResult:
    eligible_candidates: list[EvaluatedTradeCandidate] = []
    rejected_candidates: list[RejectedEvaluatedCandidateSelection] = []
    decision_engine = EntryDecisionEngine()

    for original in rank_evaluated_trade_candidates(evaluated_candidates):
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
                    evaluated_candidate,
                    candidate.tp_feasibility_hard_rejection_reason,
                    selection_threshold_source='tp_feasibility',
                )
            )
            continue
        if (
            candidate.tp_probability is None
            or candidate.touch_probability is None
            or candidate.direction_edge is None
        ):
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate,
                    'candidate_selection_outcome_probability_missing',
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
                    evaluated_candidate,
                    'candidate_selection_expected_profit_too_low_after_fees',
                    selection_threshold_source='hard_economics',
                )
            )
            continue
        eligible_candidates.append(evaluated_candidate)

    ranked_eligible = rank_evaluated_trade_candidates(
        eligible_candidates
    )
    if config.top_n > 0:
        kept_candidates = ranked_eligible[: config.top_n]
        overflow_candidates = ranked_eligible[config.top_n :]
        rejected_candidates.extend(
            RejectedEvaluatedCandidateSelection(
                evaluated_candidate=item,
                reason='candidate_selection_outside_top_n',
                minimum_tp_probability_used=(
                    config.minimum_tp_probability
                ),
                maximum_touch_probability_used=(
                    config.maximum_touch_probability
                ),
                selection_threshold_source='direction_edge_top_n',
            )
            for item in overflow_candidates
        )
    else:
        kept_candidates = ranked_eligible

    selected_candidates: list[EvaluatedTradeCandidate] = []
    for item in kept_candidates:
        candidate = item.candidate
        if (
            candidate.tp_probability is None
            or candidate.tp_probability
            < config.minimum_tp_probability
        ):
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=item,
                    reason=(
                        'candidate_selection_tp_probability_too_low'
                    ),
                    minimum_tp_probability_used=(
                        config.minimum_tp_probability
                    ),
                    maximum_touch_probability_used=(
                        config.maximum_touch_probability
                    ),
                    selection_threshold_source='tp_probability_gate',
                )
            )
            continue
        if (
            config.maximum_touch_probability is not None
            and (
                candidate.touch_probability is None
                or candidate.touch_probability
                >= config.maximum_touch_probability
            )
        ):
            rejected_candidates.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=item,
                    reason=(
                        'candidate_selection_touch_probability_too_high'
                    ),
                    minimum_tp_probability_used=(
                        config.minimum_tp_probability
                    ),
                    maximum_touch_probability_used=(
                        config.maximum_touch_probability
                    ),
                    selection_threshold_source='touch_probability_gate',
                )
            )
            continue
        selected_candidates.append(item)

    return EvaluatedCandidateSelectionResult(
        selected_candidates,
        rejected_candidates,
    )


def rank_evaluated_trade_candidates(
    evaluated_candidates: list[EvaluatedTradeCandidate],
) -> list[EvaluatedTradeCandidate]:
    return sorted(
        evaluated_candidates,
        key=_evaluated_candidate_ranking_key,
    )


def _evaluated_candidate_ranking_key(
    evaluated_candidate: EvaluatedTradeCandidate,
) -> tuple[float, float, str]:
    candidate = evaluated_candidate.candidate
    return (
        -_ranking_value(candidate.direction_edge),
        -candidate.directional_score,
        candidate.candidate_id,
    )


def _ranking_value(value: float | None) -> float:
    return float('-inf') if value is None else float(value)
