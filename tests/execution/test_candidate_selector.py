from dataclasses import replace
from datetime import datetime, timezone

import pytest

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
    now = datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc)
    return MarketSnapshot(symbol, 99.9, 100.1, 100.0, now)


def candle(symbol: str) -> Candle:
    now = datetime(2026, 6, 26, 15, 30, tzinfo=timezone.utc)
    return Candle(
        symbol,
        60,
        99.0,
        101.0,
        98.5,
        100.0,
        None,
        now,
        now,
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
    edge: float = 0.10,
    protection: float = 0.60,
    positive: float = 0.50,
    direction_edge: float = 0.05,
) -> EvaluatedTradeCandidate:
    item = replace(
        candidate(symbol),
        probability_score=20.0,
        touch_probability=0.40,
        direction_probability=0.50,
        tp_probability=0.20,
        sl_probability=0.20,
        neither_probability=0.60,
        direction_break_even_probability=0.50 - direction_edge,
        direction_edge=direction_edge,
        outcome_probability_model_version='outcome_probability_v2',
        managed_protection_probability=protection,
        managed_positive_probability=positive,
        managed_expected_net_return_percent=edge + 0.05,
        managed_edge=edge,
        managed_outcome_model_version='managed_outcome_v1',
        managed_outcome_metadata={
            'minimum_protection_probability': 0.40,
            'minimum_positive_probability': 0.30,
            'minimum_expected_net_return_percent': 0.05,
        },
    )
    return EvaluatedTradeCandidate(
        candidate=item,
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


def evaluated_candidate_with_profit(
    item: TradeCandidate,
) -> EvaluatedTradeCandidate:
    result = evaluated(item.symbol)
    return replace(
        result,
        candidate=replace(
            result.candidate,
            snapshot=item.snapshot,
            candle=item.candle,
            signal=item.signal,
            rank_reason=item.rank_reason,
            session_key=item.session_key,
            candidate_id=item.candidate_id,
        ),
    )


def test_raw_selector_keeps_top_n():
    result = select_trade_candidates(
        [candidate('ONE', 180), candidate('TWO', 120), candidate('THREE', 80)],
        CandidateSelectionConfig(top_n=2),
    )

    assert len(result.selected_candidates) == 2
    assert result.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )


def test_direction_edge_is_retained_but_is_not_a_gate():
    result = select_evaluated_trade_candidates(
        [evaluated('MANAGED', direction_edge=-0.20)],
        CandidateSelectionConfig(top_n=1),
    )

    assert [item.candidate.symbol for item in result.selected_candidates] == [
        'MANAGED'
    ]


@pytest.mark.parametrize(
    ('protection', 'positive', 'edge', 'reason'),
    [
        (0.39, 0.50, 0.10, 'candidate_selection_managed_protection_below_floor'),
        (0.60, 0.29, 0.10, 'candidate_selection_managed_positive_below_floor'),
        (0.60, 0.50, -0.001, 'candidate_selection_managed_edge_below_margin'),
    ],
)
def test_managed_gates_run_before_top_n(protection, positive, edge, reason):
    result = select_evaluated_trade_candidates(
        [
            evaluated(
                'GATED',
                protection=protection,
                positive=positive,
                edge=edge,
            )
        ],
        CandidateSelectionConfig(top_n=1),
    )

    assert result.selected_candidates == []
    assert result.rejected_candidates[0].reason == reason


def test_rank_uses_managed_edge_then_protection():
    ranked = rank_evaluated_trade_candidates(
        [
            evaluated('FIRST', edge=0.10, protection=0.60),
            evaluated('PROTECTED', edge=0.10, protection=0.70),
            evaluated('HIGHER_EDGE', edge=0.11),
        ]
    )

    assert [item.candidate.symbol for item in ranked] == [
        'HIGHER_EDGE',
        'PROTECTED',
        'FIRST',
    ]


def test_hard_economics_precedes_managed_gate():
    item = evaluated('LOW', edge=-1.0)
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
        CandidateSelectionConfig(top_n=1),
    )

    assert result.rejected_candidates[0].reason == (
        'candidate_selection_expected_profit_too_low_after_fees'
    )


def test_overflow_is_reported_after_managed_ranking():
    result = select_evaluated_trade_candidates(
        [evaluated('SECOND', edge=0.10), evaluated('BEST', edge=0.20)],
        CandidateSelectionConfig(top_n=1),
    )

    assert [item.candidate.symbol for item in result.selected_candidates] == [
        'BEST'
    ]
    assert result.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )
    assert result.rejected_candidates[0].selection_threshold_source == (
        'managed_edge_top_n'
    )
