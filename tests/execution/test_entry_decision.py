from datetime import datetime, timezone
from types import SimpleNamespace

from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.entry_decision import EntryAction, EntryDecisionEngine
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import AssetClass, EntryDecisionConfig
from app.market.market_context import (
    BenchmarkContext,
    BreadthContext,
    CandidateMarketContext,
    ContextAlignment,
    MarketDirection,
    MarketRegime,
    SectorContext,
)
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def market_context(alignment: ContextAlignment) -> CandidateMarketContext:
    return CandidateMarketContext(
        version='market_context_v2',
        as_of=NOW,
        asset_class=AssetClass.EQUITY_US,
        regime=(
            MarketRegime.RISK_OFF
            if alignment == ContextAlignment.OPPOSED
            else MarketRegime.RISK_ON
        ),
        alignment=alignment,
        benchmark=BenchmarkContext(
            'SPX500',
            True,
            MarketDirection.BEARISH,
            -0.5,
            -0.1,
            0.02,
            0.0,
        ),
        breadth=BreadthContext(
            True,
            MarketDirection.BEARISH,
            4,
            4,
            1.0,
            1,
            3,
            0,
            0.25,
            -0.2,
        ),
        sector=SectorContext(
            'TECHNOLOGY',
            True,
            MarketDirection.BEARISH,
            3,
            3,
            0.33,
            -0.2,
        ),
        symbol_session_return_percent=0.5,
        symbol_relative_strength_percent=1.0,
        reasons=('test',),
    )


def evaluated(
    *,
    last: float,
    alignment=ContextAlignment.ALIGNED,
    remaining_move_quality='GOOD',
    confirmation_satisfied=False,
    expected_net_profit_percent=0.5,
    hard_rejection_reason=None,
):
    signal = Signal(
        action='BUY',
        setup_quality=0.8,
        reason='test',
        metadata={
            'range_high': 100.0,
            'snapshot_momentum_percent': 0.2,
            'entry_origin': (
                'pending_confirmation'
                if confirmation_satisfied
                else 'signal'
            ),
            'structural_confirmation_satisfied': confirmation_satisfied,
        },
    )
    candidate = TradeCandidate(
        symbol='TEST',
        snapshot=MarketSnapshot(
            'TEST', last - 0.05, last + 0.05, last, NOW
        ),
        candle=Candle(
            'TEST', 60, 99.8, last, 99.7, last, None, NOW, NOW
        ),
        signal=signal,
        rank_reason='test',
        entry_quality_metadata={
            'remaining_move_quality': remaining_move_quality
        },
        market_context=market_context(alignment),
        market_context_score=(
            -8.0 if alignment == ContextAlignment.OPPOSED else 8.0
        ),
    )
    return EvaluatedTradeCandidate(
        candidate=candidate,
        economics=CandidateEconomics(
            position_value=100.0,
            expected_gross_profit=1.0,
            expected_net_profit=expected_net_profit_percent,
            expected_net_profit_percent=expected_net_profit_percent,
            estimated_total_cost=0.5,
            estimated_total_cost_percent=0.5,
            min_expected_net_profit_percent=0.1,
            required_min_expected_net_profit_amount=0.1,
        ),
        tp_feasibility=SimpleNamespace(
            tp_feasibility_hard_rejection_reason=(
                hard_rejection_reason
            ),
            effective_take_profit_percent=1.0,
        ),
    )


def decide(item):
    return EntryDecisionEngine().evaluate(
        evaluated_candidate=item,
        config=EntryDecisionConfig(),
    )


def test_routes_to_selection_when_price_is_not_extended():
    decision = decide(evaluated(last=100.05))
    assert decision.action == EntryAction.READY_FOR_SELECTION
    assert decision.reason == 'entry_conditions_satisfied'


def test_waits_for_retest_when_structure_is_extended_and_usable():
    decision = decide(evaluated(last=100.50))
    assert decision.action == EntryAction.WAIT_FOR_RETEST
    assert decision.retest_eligible is True
    assert decision.reason == 'better_entry_required_at_structure'


def test_opposed_context_never_changes_router_action():
    aligned = decide(
        evaluated(last=100.05, alignment=ContextAlignment.ALIGNED)
    )
    opposed = decide(
        evaluated(last=100.05, alignment=ContextAlignment.OPPOSED)
    )

    assert opposed.action == aligned.action == EntryAction.READY_FOR_SELECTION
    assert opposed.reason == aligned.reason == 'entry_conditions_satisfied'
    assert opposed.diagnostics['market_context_score'] == -8.0


def test_retest_reason_is_structural_not_contextual():
    decision = decide(
        evaluated(last=100.20, alignment=ContextAlignment.OPPOSED)
    )
    assert decision.action == EntryAction.WAIT_FOR_RETEST
    assert decision.reason == 'better_entry_required_at_structure'
    assert 'opposed' not in decision.reason


def test_poor_structure_does_not_create_retest_or_context_veto():
    decision = decide(
        evaluated(
            last=100.20,
            alignment=ContextAlignment.OPPOSED,
            remaining_move_quality='POOR',
        )
    )
    assert decision.action == EntryAction.READY_FOR_SELECTION
    assert decision.diagnostics['structural_retest_score'] == 0.0


def test_confirmed_pending_bypasses_same_retest_request():
    decision = decide(
        evaluated(
            last=100.50,
            confirmation_satisfied=True,
            alignment=ContextAlignment.OPPOSED,
        )
    )
    assert decision.action == EntryAction.READY_FOR_SELECTION
    assert decision.reason == 'pending_structural_confirmation_satisfied'
    assert decision.retest_eligible is False


def test_economic_constraint_still_skips():
    decision = decide(
        evaluated(last=100.05, expected_net_profit_percent=0.05)
    )
    assert decision.action == EntryAction.SKIP
    assert decision.reason == (
        'candidate_selection_expected_profit_too_low_after_fees'
    )


def test_tp_hard_rejection_still_skips():
    decision = decide(
        evaluated(
            last=100.05,
            hard_rejection_reason=(
                'candidate_selection_tp_feasibility_cost_to_tp_absurd'
            ),
        )
    )
    assert decision.action == EntryAction.SKIP
    assert decision.reason == (
        'candidate_selection_tp_feasibility_cost_to_tp_absurd'
    )
