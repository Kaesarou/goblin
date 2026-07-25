from dataclasses import dataclass, replace
from typing import Any

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.candidate_readiness import (
    CandidateReadiness,
    evaluate_candidate_readiness,
)
from app.execution.scoring.market_context_scorer import score_market_context
from app.execution.sl_tp_profile import EffectiveSlTpResolver
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import RiskProfile, TpFeasibilityConfig


TP_FEASIBILITY_MODEL_VERSION = 'tp_feasibility_score_v4'
TP_FEASIBILITY_HARD_REJECTION_PREFIX = 'candidate_selection_tp_feasibility_'


@dataclass(frozen=True)
class TpFeasibilityAnalysis:
    effective_take_profit_percent: float
    effective_stop_loss_percent: float
    atr_percent: float | None
    snapshot_momentum_percent: float | None
    directional_snapshot_momentum_percent: float | None
    session_move_percent: float | None
    directional_session_move_percent: float | None
    tp_to_atr_ratio: float | None
    tp_to_snapshot_momentum_ratio: float | None
    required_net_move_percent: float
    cost_to_tp_ratio: float
    reward_to_risk_ratio: float
    net_reward_to_risk_ratio: float
    sl_tp_mode: str
    sl_tp_source: str
    distance_to_trade_extreme_percent: float | None
    movement_consumed_percent: float | None
    movement_consumed_to_tp_ratio: float | None
    entry_freshness_score: float
    feasibility_score: float
    component_scores: dict[str, float]
    score_contribution: float
    tp_feasibility_hard_rejection_reason: str | None
    readiness: CandidateReadiness
    readiness_reason: str
    hard_rejection_components: tuple[str, ...]
    reason_components: tuple[str, ...]
    model_version: str = TP_FEASIBILITY_MODEL_VERSION


class TpFeasibilityAnalyzer:
    def __init__(self, sl_tp_resolver: EffectiveSlTpResolver | None = None):
        self.sl_tp_resolver = sl_tp_resolver or EffectiveSlTpResolver()

    def analyze(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
        risk_profile: RiskProfile,
    ) -> TpFeasibilityAnalysis:
        candidate = evaluated_candidate.candidate
        config = risk_profile.tp_feasibility
        effective_sl_tp = (
            evaluated_candidate.effective_sl_tp
            or self.sl_tp_resolver.resolve_for_signal(
                signal=candidate.signal,
                risk_profile=risk_profile,
            )
        )
        if not config.enabled:
            return self._disabled_analysis(
                evaluated_candidate=evaluated_candidate,
                effective_sl_tp=effective_sl_tp,
            )

        metadata = candidate.signal.metadata or {}
        side = candidate.signal.action
        effective_take_profit_percent = effective_sl_tp.take_profit_percent
        effective_stop_loss_percent = effective_sl_tp.stop_loss_percent
        atr_percent = effective_sl_tp.atr_percent
        snapshot_momentum_percent = _optional_float(
            metadata.get('snapshot_momentum_percent')
        )
        session_move_percent = _optional_float(
            metadata.get('session_move_percent')
        )
        directional_snapshot_momentum_percent = _directional_value(
            side,
            snapshot_momentum_percent,
        )
        directional_session_move_percent = _directional_value(
            side,
            session_move_percent,
        )
        tp_to_atr_ratio = _ratio(effective_take_profit_percent, atr_percent)
        tp_to_snapshot_momentum_ratio = _ratio(
            effective_take_profit_percent,
            _positive_float(directional_snapshot_momentum_percent),
        )
        required_net_move_percent = (
            evaluated_candidate.economics.estimated_total_cost_percent
            + evaluated_candidate.economics.min_expected_net_profit_percent
            + config.feasibility_buffer_percent
        )
        cost_to_tp_ratio = (
            evaluated_candidate.economics.cost_to_tp_ratio
            or _safe_ratio(
                evaluated_candidate.economics.estimated_total_cost_percent,
                effective_take_profit_percent,
            )
        )
        reward_to_risk_ratio = (
            evaluated_candidate.economics.reward_to_risk_ratio
            or _safe_ratio(
                effective_take_profit_percent,
                effective_stop_loss_percent,
            )
        )
        net_reward_to_risk_ratio = (
            evaluated_candidate.economics.net_reward_to_risk_ratio
            or _safe_ratio(
                evaluated_candidate.economics.expected_net_profit_percent,
                effective_stop_loss_percent
                + evaluated_candidate.economics.estimated_total_cost_percent,
            )
        )
        distance_to_trade_extreme_percent = _distance_to_trade_extreme(
            candidate
        )
        movement_consumed_percent = (
            max(directional_session_move_percent, 0.0)
            if directional_session_move_percent is not None
            else None
        )
        movement_consumed_to_tp_ratio = _ratio(
            movement_consumed_percent,
            effective_take_profit_percent,
        )
        entry_freshness_score = _score_high_value_is_bad(
            movement_consumed_to_tp_ratio,
            good=config.good_movement_consumed_to_tp_ratio,
            bad=config.bad_movement_consumed_to_tp_ratio,
            missing=config.missing_component_score,
        )

        component_scores = {
            'tp_vs_atr': _score_high_value_is_bad(
                tp_to_atr_ratio,
                good=config.good_tp_to_atr_ratio,
                bad=config.bad_tp_to_atr_ratio,
                missing=config.missing_component_score,
            ),
            'tp_vs_momentum': _momentum_score(
                directional_momentum=directional_snapshot_momentum_percent,
                tp_to_momentum_ratio=tp_to_snapshot_momentum_ratio,
                config=config,
            ),
            'cost_vs_tp': _score_high_value_is_bad(
                cost_to_tp_ratio,
                good=config.good_cost_to_tp_ratio,
                bad=config.bad_cost_to_tp_ratio,
                missing=config.missing_component_score,
            ),
            'entry_freshness': entry_freshness_score,
        }
        feasibility_score = _weighted_feasibility_score(
            component_scores,
            config,
        )
        contribution = _score_contribution(feasibility_score, config)
        hard_rejection_reason = None
        hard_rejection_components: tuple[str, ...] = ()
        if cost_to_tp_ratio >= config.cost_to_tp_hard_reject_ratio:
            hard_rejection_reason = _reason('cost_to_tp_absurd')
            hard_rejection_components = (
                'cost_to_tp_absurd_hard_reject',
            )
        readiness = evaluate_candidate_readiness(
            hard_rejection_reason=hard_rejection_reason,
        )
        reasons = _reason_components(
            component_scores=component_scores,
            atr_missing=tp_to_atr_ratio is None,
            momentum_missing=directional_snapshot_momentum_percent is None,
            session_move_missing=movement_consumed_percent is None,
        )

        return TpFeasibilityAnalysis(
            effective_take_profit_percent=round(
                effective_take_profit_percent,
                4,
            ),
            effective_stop_loss_percent=round(
                effective_stop_loss_percent,
                4,
            ),
            atr_percent=_round_optional(atr_percent),
            snapshot_momentum_percent=_round_optional(
                snapshot_momentum_percent
            ),
            directional_snapshot_momentum_percent=_round_optional(
                directional_snapshot_momentum_percent
            ),
            session_move_percent=_round_optional(session_move_percent),
            directional_session_move_percent=_round_optional(
                directional_session_move_percent
            ),
            tp_to_atr_ratio=_round_optional(tp_to_atr_ratio),
            tp_to_snapshot_momentum_ratio=_round_optional(
                tp_to_snapshot_momentum_ratio
            ),
            required_net_move_percent=round(required_net_move_percent, 4),
            cost_to_tp_ratio=round(cost_to_tp_ratio, 4),
            reward_to_risk_ratio=round(reward_to_risk_ratio, 4),
            net_reward_to_risk_ratio=round(net_reward_to_risk_ratio, 4),
            sl_tp_mode=effective_sl_tp.mode,
            sl_tp_source=effective_sl_tp.source,
            distance_to_trade_extreme_percent=_round_optional(
                distance_to_trade_extreme_percent
            ),
            movement_consumed_percent=_round_optional(
                movement_consumed_percent
            ),
            movement_consumed_to_tp_ratio=_round_optional(
                movement_consumed_to_tp_ratio
            ),
            entry_freshness_score=round(entry_freshness_score, 4),
            feasibility_score=round(feasibility_score, 4),
            component_scores={
                name: round(value, 4)
                for name, value in component_scores.items()
            },
            score_contribution=round(contribution, 4),
            tp_feasibility_hard_rejection_reason=hard_rejection_reason,
            readiness=readiness.readiness,
            readiness_reason=readiness.reason,
            hard_rejection_components=hard_rejection_components,
            reason_components=reasons,
        )

    def _disabled_analysis(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
        effective_sl_tp,
    ) -> TpFeasibilityAnalysis:
        return TpFeasibilityAnalysis(
            effective_take_profit_percent=round(
                effective_sl_tp.take_profit_percent,
                4,
            ),
            effective_stop_loss_percent=round(
                effective_sl_tp.stop_loss_percent,
                4,
            ),
            atr_percent=_round_optional(effective_sl_tp.atr_percent),
            snapshot_momentum_percent=None,
            directional_snapshot_momentum_percent=None,
            session_move_percent=None,
            directional_session_move_percent=None,
            tp_to_atr_ratio=None,
            tp_to_snapshot_momentum_ratio=None,
            required_net_move_percent=0.0,
            cost_to_tp_ratio=0.0,
            reward_to_risk_ratio=(
                evaluated_candidate.economics.reward_to_risk_ratio
            ),
            net_reward_to_risk_ratio=(
                evaluated_candidate.economics.net_reward_to_risk_ratio
            ),
            sl_tp_mode=effective_sl_tp.mode,
            sl_tp_source=effective_sl_tp.source,
            distance_to_trade_extreme_percent=None,
            movement_consumed_percent=None,
            movement_consumed_to_tp_ratio=None,
            entry_freshness_score=50.0,
            feasibility_score=50.0,
            component_scores={},
            score_contribution=0.0,
            tp_feasibility_hard_rejection_reason=None,
            readiness=CandidateReadiness.TRADABLE_NOW,
            readiness_reason='tp_feasibility_disabled',
            hard_rejection_components=(),
            reason_components=('disabled',),
        )


class CandidateTpFeasibilityEvaluator:
    def __init__(self, analyzer: TpFeasibilityAnalyzer | None = None):
        self.analyzer = analyzer or TpFeasibilityAnalyzer()

    def evaluate(
        self,
        *,
        evaluated_candidate: EvaluatedTradeCandidate,
        risk_profile: RiskProfile,
    ) -> EvaluatedTradeCandidate:
        analysis = self.analyzer.analyze(
            evaluated_candidate=evaluated_candidate,
            risk_profile=risk_profile,
        )
        candidate = evaluated_candidate.candidate
        context_score = score_market_context(
            context=candidate.market_context,
            side=candidate.signal.action,
            entry_freshness_score=analysis.entry_freshness_score,
        )
        updated_candidate = replace(
            candidate,
            rank_reason=_append_rank_reason(
                candidate.rank_reason,
                analysis,
                context_score.score,
            ),
            market_context_score=context_score.score,
            market_context_components=context_score.components,
            market_context_score_metadata={
                **context_score.diagnostics,
                'model_version': context_score.model_version,
            },
            tp_feasibility_metadata=analysis_to_metadata(analysis),
            tp_feasibility_score=analysis.feasibility_score,
            tp_feasibility_contribution=analysis.score_contribution,
            tp_feasibility_hard_rejection_reason=(
                analysis.tp_feasibility_hard_rejection_reason
            ),
        )
        evaluated = replace(
            evaluated_candidate,
            candidate=updated_candidate,
            tp_feasibility=analysis,
            readiness=analysis.readiness,
            readiness_reason=analysis.readiness_reason,
        )
        return evaluated


def analysis_to_metadata(
    analysis: TpFeasibilityAnalysis,
) -> dict[str, Any]:
    return {
        'model_version': analysis.model_version,
        'effective_take_profit_percent': analysis.effective_take_profit_percent,
        'effective_stop_loss_percent': analysis.effective_stop_loss_percent,
        'atr_percent': analysis.atr_percent,
        'snapshot_momentum_percent': analysis.snapshot_momentum_percent,
        'directional_snapshot_momentum_percent': (
            analysis.directional_snapshot_momentum_percent
        ),
        'session_move_percent': analysis.session_move_percent,
        'directional_session_move_percent': (
            analysis.directional_session_move_percent
        ),
        'tp_to_atr_ratio': analysis.tp_to_atr_ratio,
        'tp_to_snapshot_momentum_ratio': (
            analysis.tp_to_snapshot_momentum_ratio
        ),
        'required_net_move_percent': analysis.required_net_move_percent,
        'cost_to_tp_ratio': analysis.cost_to_tp_ratio,
        'reward_to_risk_ratio': analysis.reward_to_risk_ratio,
        'net_reward_to_risk_ratio': analysis.net_reward_to_risk_ratio,
        'sl_tp_mode': analysis.sl_tp_mode,
        'sl_tp_source': analysis.sl_tp_source,
        'distance_to_trade_extreme_percent': (
            analysis.distance_to_trade_extreme_percent
        ),
        'movement_consumed_percent': analysis.movement_consumed_percent,
        'movement_consumed_to_tp_ratio': (
            analysis.movement_consumed_to_tp_ratio
        ),
        'entry_freshness_score': analysis.entry_freshness_score,
        'feasibility_score': analysis.feasibility_score,
        'component_scores': analysis.component_scores,
        'score_contribution': analysis.score_contribution,
        'tp_feasibility_hard_rejection_reason': (
            analysis.tp_feasibility_hard_rejection_reason
        ),
        'readiness': analysis.readiness.value,
        'readiness_reason': analysis.readiness_reason,
        'hard_rejection_components': list(
            analysis.hard_rejection_components
        ),
        'reason_components': list(analysis.reason_components),
    }


def _append_rank_reason(
    rank_reason: str,
    analysis: TpFeasibilityAnalysis,
    final_market_context_score: float,
) -> str:
    suffix = (
        f'final_market_context_score={final_market_context_score:.2f},'
        f'tp_feasibility_score={analysis.feasibility_score:.2f},'
        f'tp_feasibility_contribution={analysis.score_contribution:.2f},'
        f'entry_freshness_score={analysis.entry_freshness_score:.2f},'
        f'movement_consumed_to_tp_ratio='
        f'{analysis.movement_consumed_to_tp_ratio},'
        f'tp_feasibility_components={analysis.component_scores},'
        f'readiness={analysis.readiness.value},'
        f'readiness_reason={analysis.readiness_reason},'
        f'sl_tp_mode={analysis.sl_tp_mode},'
        f'sl_tp_source={analysis.sl_tp_source}'
    )
    if analysis.tp_feasibility_hard_rejection_reason:
        suffix += (
            f',hard_reject='
            f'{analysis.tp_feasibility_hard_rejection_reason}'
        )
    return f'{rank_reason};{suffix}' if rank_reason else suffix


def _weighted_feasibility_score(
    component_scores: dict[str, float],
    config: TpFeasibilityConfig,
) -> float:
    return _clamp(
        component_scores['tp_vs_atr'] * config.tp_vs_atr_weight
        + component_scores['tp_vs_momentum']
        * config.tp_vs_momentum_weight
        + component_scores['cost_vs_tp'] * config.cost_vs_tp_weight
        + component_scores['entry_freshness']
        * config.entry_freshness_weight,
        0.0,
        100.0,
    )


def _score_contribution(
    feasibility_score: float,
    config: TpFeasibilityConfig,
) -> float:
    normalized = (feasibility_score - 50.0) / 50.0
    return _clamp(
        normalized * config.maximum_score_contribution,
        -config.maximum_score_contribution,
        config.maximum_score_contribution,
    )


def _momentum_score(
    *,
    directional_momentum: float | None,
    tp_to_momentum_ratio: float | None,
    config: TpFeasibilityConfig,
) -> float:
    if directional_momentum is None:
        return config.missing_component_score
    if directional_momentum <= 0:
        return 0.0
    return _score_high_value_is_bad(
        tp_to_momentum_ratio,
        good=config.good_tp_to_momentum_ratio,
        bad=config.bad_tp_to_momentum_ratio,
        missing=config.missing_component_score,
    )


def _score_high_value_is_bad(
    value: float | None,
    *,
    good: float,
    bad: float,
    missing: float,
) -> float:
    if value is None:
        return _clamp(missing, 0.0, 100.0)
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (1.0 - ((value - good) / (bad - good)))


def _reason_components(
    *,
    component_scores: dict[str, float],
    atr_missing: bool,
    momentum_missing: bool,
    session_move_missing: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if atr_missing:
        reasons.append('missing_atr')
    if momentum_missing:
        reasons.append('missing_snapshot_momentum')
    if session_move_missing:
        reasons.append('missing_session_move')
    for name, score in component_scores.items():
        if score < 35.0:
            reasons.append(f'{name}_weak')
        elif score >= 75.0:
            reasons.append(f'{name}_strong')
        else:
            reasons.append(f'{name}_neutral')
    return tuple(reasons)


def _distance_to_trade_extreme(candidate: TradeCandidate) -> float | None:
    if candidate.signal.action == 'BUY':
        return _optional_float(
            candidate.entry_quality_metadata.get(
                'distance_to_recent_high_percent'
            )
        )
    if candidate.signal.action == 'SELL':
        return _optional_float(
            candidate.entry_quality_metadata.get(
                'distance_to_recent_low_percent'
            )
        )
    return None


def _directional_value(side: str, value: float | None) -> float | None:
    if value is None:
        return None
    if side == 'BUY':
        return value
    if side == 'SELL':
        return -value
    return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return float('inf')
    return numerator / denominator


def _positive_float(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _reason(value: str) -> str:
    return f'{TP_FEASIBILITY_HARD_REJECTION_PREFIX}{value}'


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
