import logging
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone

from app.execution.candidate_economics import (
    EvaluatedTradeCandidate,
)
from app.execution.candidate_ranking import rank_trade_candidates
from app.execution.candidate_selector import (
    CandidateSelectionResult,
    EvaluatedCandidateSelectionResult,
    RejectedCandidateSelection,
    RejectedEvaluatedCandidateSelection,
    rank_evaluated_trade_candidates,
    select_evaluated_trade_candidates,
    select_trade_candidates,
)
from app.execution.entry_decision import EntryDecisionEngine
from app.execution.scoring.outcome_probability import (
    CandidateOutcomeProbabilityEvaluator,
)
from app.execution.scoring.tp_feasibility import (
    CandidateTpFeasibilityEvaluator,
)
from app.execution.sl_tp_profile import EffectiveSlTp, EffectiveSlTpResolver
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import AssetClass, EntryDecisionConfig
from app.journal.jsonl_journal import JsonlJournal
from app.risk.models import TradePlan
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.strategies.models import StrategyProfileConfig

logger = logging.getLogger(__name__)


def select_trade_candidates_with_strategy_profile(
    candidates: list[TradeCandidate],
    risk_manager: RiskManager,
    strategy_profile: StrategyProfileConfig,
) -> CandidateSelectionResult:
    grouped: dict[AssetClass, list[TradeCandidate]] = defaultdict(list)
    for candidate in candidates:
        asset_class = risk_manager.instrument_profile_for(
            candidate.symbol
        ).asset_class
        grouped[asset_class].append(candidate)

    selected: list[TradeCandidate] = []
    rejected: list[RejectedCandidateSelection] = []
    for asset_class, asset_candidates in grouped.items():
        result = select_trade_candidates(
            asset_candidates,
            strategy_profile.candidate_selection_config_for_asset_class(
                asset_class
            ),
        )
        selected.extend(result.selected_candidates)
        rejected.extend(result.rejected_candidates)
    return CandidateSelectionResult(
        rank_trade_candidates(selected),
        rejected,
    )


def select_evaluated_trade_candidates_with_strategy_profile(
    evaluated_candidates: list[EvaluatedTradeCandidate],
    risk_manager: RiskManager,
    strategy_profile: StrategyProfileConfig,
) -> EvaluatedCandidateSelectionResult:
    grouped: dict[
        AssetClass,
        list[EvaluatedTradeCandidate],
    ] = defaultdict(list)
    for evaluated_candidate in evaluated_candidates:
        asset_class = risk_manager.instrument_profile_for(
            evaluated_candidate.candidate.symbol
        ).asset_class
        grouped[asset_class].append(evaluated_candidate)

    selected: list[EvaluatedTradeCandidate] = []
    rejected: list[RejectedEvaluatedCandidateSelection] = []
    for asset_class, asset_candidates in grouped.items():
        result = select_evaluated_trade_candidates(
            asset_candidates,
            strategy_profile.candidate_selection_config_for_asset_class(
                asset_class
            ),
        )
        selected.extend(result.selected_candidates)
        rejected.extend(result.rejected_candidates)
    return EvaluatedCandidateSelectionResult(
        rank_evaluated_trade_candidates(selected),
        rejected,
    )


def _candidate_selection_result_from_evaluated(
    selection_result: EvaluatedCandidateSelectionResult,
) -> CandidateSelectionResult:
    return CandidateSelectionResult(
        selected_candidates=[
            item.candidate
            for item in selection_result.selected_candidates
        ],
        rejected_candidates=[
            RejectedCandidateSelection(
                candidate=item.evaluated_candidate.candidate,
                reason=item.reason,
            )
            for item in selection_result.rejected_candidates
        ],
    )


def apply_trade_cooldown_guard(
    *,
    candidates: list[TradeCandidate],
    risk_manager: RiskManager,
    cooldown_guard: TradeCooldownGuard,
    trade_journal: JsonlJournal,
) -> list[TradeCandidate]:
    now = datetime.now(timezone.utc)
    cooldown_guard.store.delete_expired(now)
    result = cooldown_guard.filter_candidates(
        candidates=candidates,
        risk_manager=risk_manager,
        now=now,
    )
    for rejected in result.rejected_candidates:
        candidate = rejected.candidate
        decision = rejected.decision
        plan = TradePlan(
            approved=False,
            reason=decision.reason or 'trade_cooldown_active',
            symbol=candidate.symbol,
            side=candidate.signal.action,
        )
        trade_journal.write(
            'cooldown_blocked',
            {
                'symbol': candidate.symbol,
                'snapshot': candidate.snapshot,
                'candle': candidate.candle,
                'signal': candidate.signal,
                'candidate': candidate,
                'trade_plan': plan,
                'cooldown': decision.active_cooldown,
                'cooldown_remaining_seconds': (
                    decision.remaining_seconds
                ),
                'lock_scope': decision.lock_scope,
                'blocked_sides': list(decision.blocked_sides),
                'instrument_profile': (
                    risk_manager.instrument_profile_for(candidate.symbol)
                ),
                'risk_profile': risk_manager.risk_profile_for(
                    candidate.symbol
                ),
            },
        )
    return result.selected_candidates


def apply_tp_feasibility_to_evaluated_candidates(
    *,
    evaluated_candidates: list[EvaluatedTradeCandidate],
    risk_manager: RiskManager,
    evaluator: CandidateTpFeasibilityEvaluator | None = None,
) -> list[EvaluatedTradeCandidate]:
    actual_evaluator = evaluator or CandidateTpFeasibilityEvaluator()
    return [
        actual_evaluator.evaluate(
            evaluated_candidate=item,
            risk_profile=risk_manager.risk_profile_for(
                item.candidate.symbol
            ),
        )
        for item in evaluated_candidates
    ]


def attach_entry_decisions(
    evaluated_candidates: list[EvaluatedTradeCandidate],
) -> list[EvaluatedTradeCandidate]:
    engine = EntryDecisionEngine()
    result: list[EvaluatedTradeCandidate] = []
    for item in evaluated_candidates:
        decision = item.entry_decision or engine.evaluate(
            evaluated_candidate=item,
            config=(
                item.candidate.entry_decision_config
                or EntryDecisionConfig()
            ),
        )
        result.append(replace(item, entry_decision=decision))
    return result


def apply_outcome_probability_to_evaluated_candidates(
    *,
    evaluated_candidates: list[EvaluatedTradeCandidate],
    risk_manager: RiskManager,
    evaluator: CandidateOutcomeProbabilityEvaluator | None = None,
) -> list[EvaluatedTradeCandidate]:
    actual_evaluator = evaluator or CandidateOutcomeProbabilityEvaluator()
    return [
        actual_evaluator.evaluate(
            evaluated_candidate=item,
            risk_profile=risk_manager.risk_profile_for(
                item.candidate.symbol
            ),
        )
        for item in evaluated_candidates
    ]


def _slippage_percent(
    *,
    planned_entry_price: float,
    effective_entry_price: float,
) -> float | None:
    if planned_entry_price <= 0:
        return None
    return round(
        (
            (effective_entry_price - planned_entry_price)
            / planned_entry_price
        )
        * 100,
        4,
    )


def _resolve_runtime_effective_sl_tp(
    *,
    candidate: TradeCandidate,
    risk_profile: object,
    resolver: EffectiveSlTpResolver,
) -> EffectiveSlTp | None:
    if not hasattr(risk_profile, 'dynamic_sl_tp_enabled'):
        return None
    return resolver.resolve(
        candidate=candidate,
        risk_profile=risk_profile,
    )


def _evaluate_risk_manager(
    *,
    risk_manager: RiskManager,
    candidate: TradeCandidate,
    equity: float,
    effective_sl_tp: EffectiveSlTp | None,
) -> TradePlan:
    if effective_sl_tp is None:
        return risk_manager.evaluate(
            signal=candidate.signal,
            snapshot=candidate.snapshot,
            account_equity=equity,
            session_key=candidate.session_key,
        )
    return risk_manager.evaluate(
        signal=candidate.signal,
        snapshot=candidate.snapshot,
        account_equity=equity,
        session_key=candidate.session_key,
        effective_sl_tp=effective_sl_tp,
    )


def _candidate_log_item(
    candidate: TradeCandidate,
    economics_by_id: dict[int, object],
    effective_sl_tp_by_id: dict[int, EffectiveSlTp | None],
) -> dict:
    object_id = id(candidate)
    economics = economics_by_id.get(object_id)
    effective_sl_tp = effective_sl_tp_by_id.get(object_id)
    return {
        'candidate_id': candidate.candidate_id,
        'symbol': candidate.symbol,
        'action': candidate.signal.action,
        'probability_score': candidate.probability_score,
        'directional_score': candidate.directional_score,
        'market_context_score': candidate.market_context_score,
        'multi_timeframe_score': candidate.multi_timeframe_score,
        'tp_feasibility_score': candidate.tp_feasibility_score,
        'tp_feasibility_contribution': (
            candidate.tp_feasibility_contribution
        ),
        'expected_net_profit': (
            round(economics.expected_net_profit, 4)
            if economics
            else None
        ),
        'expected_net_profit_percent': (
            round(economics.expected_net_profit_percent, 4)
            if economics
            else None
        ),
        'effective_take_profit_percent': (
            effective_sl_tp.take_profit_percent
            if effective_sl_tp
            else None
        ),
        'effective_stop_loss_percent': (
            effective_sl_tp.stop_loss_percent
            if effective_sl_tp
            else None
        ),
        'sl_tp_mode': (
            effective_sl_tp.mode if effective_sl_tp else None
        ),
        'sl_tp_source': (
            effective_sl_tp.source if effective_sl_tp else None
        ),
        'touch_probability': candidate.touch_probability,
        'direction_probability': candidate.direction_probability,
        'tp_probability': candidate.tp_probability,
        'sl_probability': candidate.sl_probability,
        'neither_probability': candidate.neither_probability,
        'direction_break_even_probability': (
            candidate.direction_break_even_probability
        ),
        'direction_edge': candidate.direction_edge,
        'reason': candidate.rank_reason,
    }
