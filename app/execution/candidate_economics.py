from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.execution.sl_tp_profile import EffectiveSlTp, EffectiveSlTpResolver
from app.execution.trade_candidate import TradeCandidate
from app.instruments.instrument_registry import InstrumentRegistry
from app.risk.position_sizing import PositionSizingStrategy, constant_risk_position_value
from app.risk.trade_cost_model import TradeCostModel
from app.utils.commons import spread_percent

if TYPE_CHECKING:
    from app.execution.entry_decision import EntryDecision
    from app.execution.scoring.outcome_probability import (
        OutcomeProbabilityEstimate,
    )
    from app.execution.scoring.tp_feasibility import TpFeasibilityAnalysis


@dataclass(frozen=True)
class CandidateEconomics:
    position_value: float
    expected_gross_profit: float
    expected_net_profit: float
    expected_net_profit_percent: float
    estimated_total_cost: float
    estimated_total_cost_percent: float
    min_expected_net_profit_percent: float
    required_min_expected_net_profit_amount: float
    effective_take_profit_percent: float = 0.0
    effective_stop_loss_percent: float = 0.0
    cost_to_tp_ratio: float = 0.0
    reward_to_risk_ratio: float = 0.0
    net_reward_to_risk_ratio: float = 0.0


@dataclass(frozen=True)
class EvaluatedTradeCandidate:
    candidate: TradeCandidate
    economics: CandidateEconomics
    effective_sl_tp: EffectiveSlTp | None = None
    tp_feasibility: TpFeasibilityAnalysis | None = None
    outcome_probability: OutcomeProbabilityEstimate | None = None
    entry_decision: EntryDecision | None = None
    readiness: Any = None
    readiness_reason: str | None = None


class CandidateEconomicsEstimator:
    def __init__(
        self,
        position_sizing_strategy: PositionSizingStrategy,
        instrument_registry: InstrumentRegistry,
        trade_cost_model: TradeCostModel | None = None,
        sl_tp_resolver: EffectiveSlTpResolver | None = None,
    ):
        self.position_sizing_strategy = position_sizing_strategy
        self.instrument_registry = instrument_registry
        self.trade_cost_model = trade_cost_model or TradeCostModel()
        self.sl_tp_resolver = sl_tp_resolver or EffectiveSlTpResolver()

    def evaluate(self, candidate: TradeCandidate, account_equity: float) -> EvaluatedTradeCandidate:
        risk_profile = self.instrument_registry.risk_profile_for(candidate.symbol)
        effective_sl_tp = self.sl_tp_resolver.resolve(candidate=candidate, risk_profile=risk_profile)
        position_value = self.position_sizing_strategy.calculate_amount(
            account_equity=account_equity,
            risk_profile=risk_profile,
        )
        baseline_stop_loss = effective_sl_tp.metadata.get('constant_risk_baseline_stop_loss_percent')
        if baseline_stop_loss is not None:
            position_value = constant_risk_position_value(
                baseline_position_value=position_value,
                baseline_stop_loss_percent=float(baseline_stop_loss),
                effective_stop_loss_percent=effective_sl_tp.stop_loss_percent,
            )
        estimate = self.trade_cost_model.estimate(
            position_value=position_value,
            expected_move_percent=effective_sl_tp.take_profit_percent,
            spread_percent=spread_percent(candidate.snapshot),
            config=risk_profile.trade_cost,
        )
        loss_at_sl_percent = effective_sl_tp.stop_loss_percent + estimate.total_estimated_cost_percent
        return EvaluatedTradeCandidate(
            candidate=candidate,
            economics=CandidateEconomics(
                position_value=estimate.position_value,
                expected_gross_profit=estimate.expected_gross_profit,
                expected_net_profit=estimate.expected_net_profit,
                expected_net_profit_percent=estimate.expected_net_profit_percent,
                estimated_total_cost=estimate.total_estimated_cost,
                estimated_total_cost_percent=estimate.total_estimated_cost_percent,
                min_expected_net_profit_percent=estimate.min_expected_net_profit_percent,
                required_min_expected_net_profit_amount=estimate.required_min_expected_net_profit_amount,
                effective_take_profit_percent=effective_sl_tp.take_profit_percent,
                effective_stop_loss_percent=effective_sl_tp.stop_loss_percent,
                cost_to_tp_ratio=_safe_ratio(estimate.total_estimated_cost_percent, effective_sl_tp.take_profit_percent),
                reward_to_risk_ratio=_safe_ratio(effective_sl_tp.take_profit_percent, effective_sl_tp.stop_loss_percent),
                net_reward_to_risk_ratio=_safe_ratio(estimate.expected_net_profit_percent, loss_at_sl_percent),
            ),
            effective_sl_tp=effective_sl_tp,
        )


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator
