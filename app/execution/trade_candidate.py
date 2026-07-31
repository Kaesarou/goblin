from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal


if TYPE_CHECKING:
    from app.instruments.models import EntryDecisionConfig
    from app.market.market_context import CandidateMarketContext
    from app.market.multi_timeframe import MultiTimeframeContext


@dataclass(frozen=True)
class TradeCandidate:
    symbol: str
    snapshot: MarketSnapshot
    candle: Candle
    signal: Signal
    rank_reason: str
    session_key: str = ''
    base_score: float = 0.0
    directional_score: float = 0.0
    exhaustion_penalty: float = 0.0
    late_entry_risk: float = 0.0
    late_entry_severity: str = 'LOW'
    entry_quality_metadata: dict[str, Any] = field(default_factory=dict)
    sell_score_metadata: dict[str, Any] = field(default_factory=dict)
    sell_specific_penalty: float = 0.0
    market_context_score: float = 0.0
    market_context_components: dict[str, float] = field(default_factory=dict)
    market_context_score_metadata: dict[str, Any] = field(default_factory=dict)
    multi_timeframe_score: float = 0.0
    multi_timeframe_components: dict[str, float] = field(default_factory=dict)
    multi_timeframe_score_metadata: dict[str, Any] = field(default_factory=dict)
    tp_feasibility_metadata: dict[str, Any] = field(default_factory=dict)
    tp_feasibility_score: float | None = None
    tp_feasibility_contribution: float = 0.0
    tp_feasibility_hard_rejection_reason: str | None = None
    probability_score: float | None = None
    touch_probability: float | None = None
    direction_probability: float | None = None
    tp_probability: float | None = None
    sl_probability: float | None = None
    neither_probability: float | None = None
    direction_break_even_probability: float | None = None
    direction_edge: float | None = None
    outcome_probability_model_version: str | None = None
    outcome_probability_metadata: dict[str, Any] = field(
        default_factory=dict
    )
    managed_protection_probability: float | None = None
    managed_positive_probability: float | None = None
    managed_expected_net_return_percent: float | None = None
    managed_edge: float | None = None
    managed_outcome_model_version: str | None = None
    managed_outcome_metadata: dict[str, Any] = field(default_factory=dict)
    candidate_id: str = ''
    origin_candidate_id: str = ''
    pending_entry_id: str | None = None
    market_context: CandidateMarketContext | None = None
    multi_timeframe_context: MultiTimeframeContext | None = None
    entry_decision_config: EntryDecisionConfig | None = None
