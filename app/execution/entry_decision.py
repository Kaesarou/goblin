from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.instruments.models import EntryDecisionConfig
from app.market.market_context import ContextAlignment


ENTRY_DECISION_MODEL_VERSION = 'entry_router_v6'


class EntryAction(StrEnum):
    READY_FOR_SELECTION = 'ready_for_selection'
    WAIT_FOR_RETEST = 'wait_for_retest'
    SKIP = 'skip'


@dataclass(frozen=True)
class EntryDecision:
    action: EntryAction
    reason: str
    model_version: str = ENTRY_DECISION_MODEL_VERSION
    context_alignment: ContextAlignment = ContextAlignment.UNKNOWN
    retest_eligible: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)


class EntryDecisionEngine:
    def evaluate(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
        config: EntryDecisionConfig,
    ) -> EntryDecision:
        candidate = evaluated_candidate.candidate
        context = candidate.market_context
        alignment = (
            context.alignment
            if context is not None
            else ContextAlignment.UNKNOWN
        )
        feasibility = evaluated_candidate.tp_feasibility
        economics = evaluated_candidate.economics
        hard_rejection = (
            getattr(
                feasibility,
                'tp_feasibility_hard_rejection_reason',
                None,
            )
            or candidate.tp_feasibility_hard_rejection_reason
        )
        diagnostics = self._diagnostics(evaluated_candidate)

        if (
            economics.expected_net_profit_percent
            < economics.min_expected_net_profit_percent
        ):
            return self._decision(
                EntryAction.SKIP,
                'candidate_selection_expected_profit_too_low_after_fees',
                alignment,
                False,
                diagnostics,
            )
        if hard_rejection is not None:
            return self._decision(
                EntryAction.SKIP,
                str(hard_rejection),
                alignment,
                False,
                diagnostics,
            )

        metadata = candidate.signal.metadata or {}
        confirmation_satisfied = bool(
            metadata.get('structural_confirmation_satisfied')
        )
        diagnostics['structural_confirmation_satisfied'] = (
            confirmation_satisfied
        )
        if confirmation_satisfied:
            return self._decision(
                EntryAction.READY_FOR_SELECTION,
                'pending_structural_confirmation_satisfied',
                alignment,
                False,
                diagnostics,
            )

        extension_percent, retest_level = _extension_from_reference(candidate)
        effective_take_profit_percent = _effective_take_profit_percent(
            evaluated_candidate
        )
        extension_to_tp_ratio = _ratio(
            extension_percent,
            effective_take_profit_percent,
        )
        structural_retest_score = _structural_retest_score(candidate)
        retest_eligible = (
            retest_level is not None
            and extension_to_tp_ratio is not None
            and extension_to_tp_ratio >= config.minimum_extension_to_tp_ratio
            and structural_retest_score
            >= config.minimum_structural_retest_score
        )
        diagnostics.update(
            {
                'extension_percent': _round_optional(extension_percent),
                'effective_take_profit_percent': _round_optional(
                    effective_take_profit_percent
                ),
                'extension_to_tp_ratio': _round_optional(extension_to_tp_ratio),
                'minimum_extension_to_tp_ratio': (
                    config.minimum_extension_to_tp_ratio
                ),
                'retest_level': _round_optional(retest_level),
                'structural_retest_score': _round_optional(
                    structural_retest_score
                ),
            }
        )

        if retest_eligible:
            return self._decision(
                EntryAction.WAIT_FOR_RETEST,
                'better_entry_required_at_structure',
                alignment,
                True,
                diagnostics,
            )

        return self._decision(
            EntryAction.READY_FOR_SELECTION,
            'entry_conditions_satisfied',
            alignment,
            False,
            diagnostics,
        )

    def _diagnostics(
        self,
        evaluated_candidate: EvaluatedTradeCandidate,
    ) -> dict[str, Any]:
        candidate = evaluated_candidate.candidate
        context = candidate.market_context
        return {
            'candidate_id': candidate.candidate_id,
            'side': candidate.signal.action,
            'market_regime': (
                context.regime.value if context is not None else None
            ),
            'context_alignment': (
                context.alignment.value if context is not None else None
            ),
            'market_context_score': candidate.market_context_score,
            'multi_timeframe_score': candidate.multi_timeframe_score,
            'tp_feasibility_score': candidate.tp_feasibility_score,
            'tp_feasibility_contribution': (
                candidate.tp_feasibility_contribution
            ),
            'expected_net_profit_percent': (
                evaluated_candidate.economics.expected_net_profit_percent
            ),
            'minimum_expected_net_profit_percent': (
                evaluated_candidate.economics.min_expected_net_profit_percent
            ),
        }

    def _decision(
        self,
        action: EntryAction,
        reason: str,
        alignment: ContextAlignment,
        retest_eligible: bool,
        diagnostics: dict[str, Any],
    ) -> EntryDecision:
        return EntryDecision(
            action=action,
            reason=reason,
            context_alignment=alignment,
            retest_eligible=retest_eligible,
            diagnostics=diagnostics,
        )


def _extension_from_reference(
    candidate,
) -> tuple[float | None, float | None]:
    metadata = candidate.signal.metadata or {}
    side = candidate.signal.action.strip().upper()
    key = 'range_high' if side == 'BUY' else 'range_low'
    try:
        level = float(metadata.get(key))
    except (TypeError, ValueError):
        return None, None
    if level <= 0:
        return None, None
    current = candidate.snapshot.last
    if side == 'BUY':
        extension = ((current - level) / level) * 100
    elif side == 'SELL':
        extension = ((level - current) / level) * 100
    else:
        return None, level
    return max(0.0, extension), level


def _effective_take_profit_percent(
    evaluated_candidate: EvaluatedTradeCandidate,
) -> float | None:
    value = getattr(
        evaluated_candidate.tp_feasibility,
        'effective_take_profit_percent',
        None,
    )
    if value is None:
        value = getattr(
            evaluated_candidate.economics,
            'effective_take_profit_percent',
            None,
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _structural_retest_score(candidate) -> float:
    quality = str(
        candidate.entry_quality_metadata.get(
            'remaining_move_quality',
            'GOOD',
        )
    ).strip().upper()
    return {
        'GOOD': 100.0,
        'ACCEPTABLE': 50.0,
        'POOR': 0.0,
    }.get(quality, 0.0)


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
