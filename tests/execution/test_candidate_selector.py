from dataclasses import replace
from datetime import datetime, timezone

from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.candidate_ranking import build_trade_candidate
from app.execution.candidate_selector import (
    CandidateSelectionConfig,
    rank_evaluated_trade_candidates,
    select_evaluated_trade_candidates,
    select_trade_candidates,
)
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal

TEST_SESSION_KEY = 'test-session'


def snapshot(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        bid=99.9,
        ask=100.1,
        last=100.0,
        timestamp=datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc),
    )


def candle(symbol: str) -> Candle:
    timestamp = datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc)
    return Candle(
        symbol=symbol,
        timeframe_seconds=60,
        open=99.0,
        high=101.0,
        low=98.5,
        close=100.0,
        volume=None,
        opened_at=timestamp,
        closed_at=timestamp,
    )


def signal(session_move_percent: float = 1.0) -> Signal:
    return Signal(
        action='BUY',
        setup_quality=0.8,
        reason='test_signal',
        metadata={
            'session_move_percent': session_move_percent,
            'trend_strength_percent': 0.3,
            'breakout_percent': 0.2,
            'candle_range_percent': 0.4,
            'close_position_percent': 90.0,
            'atr_percent': 0.8,
            'market_regime': 'TRENDING',
            'regime_noise_ratio': 0.4,
        },
    )


def candidate(symbol: str, score: float = 100.0) -> TradeCandidate:
    return build_trade_candidate(
        symbol=symbol,
        snapshot=snapshot(symbol),
        candle=candle(symbol),
        signal=signal(score / 100),
        session_key=TEST_SESSION_KEY,
    )


def evaluated(
    symbol: str,
    *,
    edge: float,
    tp_probability: float = 0.20,
    directional_score: float = 100.0,
) -> EvaluatedTradeCandidate:
    base = replace(
        candidate(symbol),
        directional_score=directional_score,
        probability_score=200 * tp_probability,
        touch_probability=0.40,
        direction_probability=0.60,
        tp_probability=tp_probability,
        sl_probability=0.40 - tp_probability,
        neither_probability=0.60,
        direction_break_even_probability=0.60 - edge,
        direction_edge=edge,
        outcome_probability_model_version='outcome_probability_v2',
    )
    return EvaluatedTradeCandidate(
        candidate=base,
        economics=CandidateEconomics(
            position_value=100.0,
            expected_gross_profit=1.0,
            expected_net_profit=0.5,
            expected_net_profit_percent=0.5,
            estimated_total_cost=0.5,
            estimated_total_cost_percent=0.5,
            min_expected_net_profit_percent=0.10,
            required_min_expected_net_profit_amount=0.10,
        ),
    )


def test_raw_candidate_selector_keeps_only_top_n_candidates():
    candidates = [
        candidate('ONE', 180),
        candidate('TWO', 120),
        candidate('THREE', 80),
    ]
    result = select_trade_candidates(
        candidates,
        CandidateSelectionConfig(top_n=2),
    )

    assert len(result.selected_candidates) == 2
    assert result.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )


def test_direction_edge_margin_is_applied_before_top_n_and_backfills_slot():
    below_margin_but_high_tp = evaluated(
        'GATED',
        edge=0.04999,
        tp_probability=0.35,
        directional_score=160.0,
    )
    admissible = evaluated(
        'ADMISSIBLE',
        edge=0.051,
        tp_probability=0.18,
        directional_score=90.0,
    )

    result = select_evaluated_trade_candidates(
        [below_margin_but_high_tp, admissible],
        CandidateSelectionConfig(top_n=1, minimum_direction_edge=0.05),
    )

    assert [item.candidate.symbol for item in result.selected_candidates] == [
        'ADMISSIBLE'
    ]
    assert {
        item.evaluated_candidate.candidate.symbol: item.reason
        for item in result.rejected_candidates
    } == {
        'GATED': 'candidate_selection_direction_edge_below_margin',
    }


def test_direction_edge_margin_boundary_is_inclusive():
    exact = evaluated('EXACT', edge=0.05)
    result = select_evaluated_trade_candidates(
        [exact],
        CandidateSelectionConfig(top_n=1, minimum_direction_edge=0.05),
    )

    assert result.selected_candidates == [exact]


def test_rank_uses_edge_then_tp_probability_then_candidate_id():
    first = evaluated('FIRST', edge=0.10, tp_probability=0.18)
    higher_tp = evaluated('HIGHER_TP', edge=0.10, tp_probability=0.22)
    higher_edge = evaluated('HIGHER_EDGE', edge=0.11, tp_probability=0.10)

    ranked = rank_evaluated_trade_candidates([first, higher_tp, higher_edge])

    assert [item.candidate.symbol for item in ranked] == [
        'HIGHER_EDGE',
        'HIGHER_TP',
        'FIRST',
    ]


def test_hard_economics_rejection_precedes_direction_margin():
    item = evaluated('LOW', edge=0.01)
    item = replace(
        item,
        economics=replace(
            item.economics,
            expected_net_profit_percent=0.05,
            min_expected_net_profit_percent=0.10,
        ),
    )

    result = select_evaluated_trade_candidates(
        [item],
        CandidateSelectionConfig(top_n=1, minimum_direction_edge=0.05),
    )

    assert result.rejected_candidates[0].reason == (
        'candidate_selection_expected_profit_too_low_after_fees'
    )


def test_outside_top_n_is_reported_after_margin_filter():
    best = evaluated('BEST', edge=0.20)
    second = evaluated('SECOND', edge=0.10)
    result = select_evaluated_trade_candidates(
        [second, best],
        CandidateSelectionConfig(top_n=1, minimum_direction_edge=0.05),
    )

    assert [item.candidate.symbol for item in result.selected_candidates] == [
        'BEST'
    ]
    assert result.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )
    assert result.rejected_candidates[0].minimum_direction_edge_used == 0.05
