from types import SimpleNamespace

from app.execution.entry_decision import EntryAction
from app.journal.daily_summary import DailySummaryAggregator
from app.market.timeframes import (
    MultiTimeframeAlignment,
    OpeningRangeStatus,
    TimeframeMaturity,
)


def evaluated_candidate(
    *,
    action: EntryAction,
    candidate_id: str,
    score: float = 120.0,
):
    candidate = SimpleNamespace(
        candidate_id=candidate_id,
        origin_candidate_id=candidate_id,
        pending_entry_id=None,
        symbol='AMZN',
        signal=SimpleNamespace(action='BUY'),
        probability_score=score,
        rank_reason='bullish_breakout',
    )
    return SimpleNamespace(
        candidate=candidate,
        entry_decision=SimpleNamespace(
            action=action,
            reason='entry_conditions_satisfied',
        ),
    )


def test_summary_separates_entry_route_selection_and_risk():
    summary = DailySummaryAggregator(journal_detail_level='normal')
    selected = evaluated_candidate(
        action=EntryAction.READY_FOR_SELECTION,
        candidate_id='candidate-1',
    )
    rejected = SimpleNamespace(
        evaluated_candidate=evaluated_candidate(
            action=EntryAction.WAIT_FOR_RETEST,
            candidate_id='candidate-2',
        ),
        reason='better_entry_required_at_structure',
    )

    summary.record(
        'candidate_detected',
        {'candidate': selected.candidate},
    )
    summary.record(
        'candidate_detected',
        {'candidate': rejected.evaluated_candidate.candidate},
    )
    summary.record(
        'candidate_selection',
        {
            'selected_evaluated_candidates': [selected],
            'rejected_evaluated_candidates': [rejected],
        },
    )
    summary.record(
        'decision',
        {'trade_plan': SimpleNamespace(approved=True, reason='approved')},
    )

    data = summary.to_dict()
    assert data['schema_version'] == 9
    assert data['entry_routing']['ready_for_selection'] == 1
    assert data['entry_routing']['wait_for_retest'] == 1
    assert data['decision_pipeline']['unique_candidates'] == 2
    assert data['decision_pipeline']['selection_selected'] == 1
    assert data['decision_pipeline']['selection_rejected'] == 1
    assert data['decision_pipeline']['risk_approved'] == 1
    assert data['decision_pipeline']['risk_rejected'] == 0
    assert data['selected_candidates'][0]['score'] == 120.0
    assert 'entry_decisions' not in data
    assert 'decisions' not in data


def test_summary_counts_maturity_and_both_alignment_views():
    summary = DailySummaryAggregator(journal_detail_level='normal')
    context = SimpleNamespace(
        maturity_by_timeframe={
            'm1': TimeframeMaturity.READY,
            'm5': TimeframeMaturity.PROVISIONAL,
            'm15': TimeframeMaturity.UNAVAILABLE,
        },
        ready_alignment=MultiTimeframeAlignment.ALIGNED,
        alignment_including_provisional=MultiTimeframeAlignment.MIXED,
        opening_ranges=SimpleNamespace(
            windows={
                '15': SimpleNamespace(
                    status=OpeningRangeStatus.READY
                ),
            }
        ),
    )
    summary.record(
        'multi_timeframe_context_built',
        {'multi_timeframe_context': context},
    )

    mtf = summary.to_dict()['multi_timeframe']
    assert mtf['maturity_by_timeframe']['m1'] == {'ready': 1}
    assert mtf['maturity_by_timeframe']['m5'] == {
        'provisional': 1
    }
    assert mtf['maturity_by_timeframe']['m15'] == {
        'unavailable': 1
    }
    assert mtf['ready_alignment'] == {'aligned': 1}
    assert mtf['alignment_including_provisional'] == {'mixed': 1}


def test_summary_distinguishes_pending_blocks_from_invalidations():
    summary = DailySummaryAggregator(journal_detail_level='normal')
    base = {
        'pending_entry_id': 'pending-1',
        'origin_candidate_id': 'candidate-1',
        'symbol': 'AMD',
        'observed_candles': 1,
    }
    summary.record('pending_entry_registered', base)
    summary.record('pending_entry_retest_detected', base)
    summary.record('pending_entry_retest_detected', base)
    summary.record(
        'pending_entry_confirmation_blocked',
        {
            **base,
            'reason': 'spread_too_high',
            'spread_percent': 0.2,
            'maximum_allowed_spread_percent': 0.1,
        },
    )
    summary.record(
        'pending_entry_invalidated',
        {
            **base,
            'reason': 'structure_invalidated',
        },
    )

    pending = summary.to_dict()['pending_entries']
    assert pending['unique'] == 1
    assert pending['registered_events'] == 1
    assert pending['retest_unique'] == 1
    assert pending['retest_events'] == 2
    assert pending['confirmation_blocked_events'] == 1
    assert pending['confirmation_blocks_by_reason'] == {
        'spread_too_high': 1
    }
    assert pending['confirmation_blocks_by_symbol'] == {'AMD': 1}
    assert pending['confirmation_blocks_before_first_candle'] == 1
    assert pending['blocked_spread_observations'] == {
        'count': 1,
        'average': 0.2,
        'maximum': 0.2,
    }
    assert pending['invalidations_by_reason'] == {
        'structure_invalidated': 1
    }
    assert pending['invalidations_by_symbol'] == {'AMD': 1}
    assert 'spread_invalidations_before_first_candle' not in pending
    assert 'spread_observations' not in pending


def test_summary_tracks_orders_and_pnl_from_pending():
    summary = DailySummaryAggregator(journal_detail_level='normal')
    candidate = SimpleNamespace(pending_entry_id='pending-1')

    summary.record(
        'order_submitted',
        {'symbol': 'AMAT', 'candidate': candidate},
    )
    summary.record(
        'position_opened',
        {
            'symbol': 'AMAT',
            'position_id': 'position-1',
            'candidate': candidate,
        },
    )
    summary.record(
        'position_closed',
        {
            'closed_position': SimpleNamespace(
                position_id='position-1',
                amount=1_000.0,
                gross_pnl=12.0,
                estimated_total_cost=3.5,
                net_pnl_estimated=8.5,
            )
        },
    )

    data = summary.to_dict()
    assert data['orders']['from_pending'] == 1
    assert data['pnl']['from_pending'] == 8.5


def test_summary_does_not_fake_net_pnl_when_costs_are_unavailable():
    summary = DailySummaryAggregator(journal_detail_level='normal')
    summary.record(
        'position_closed',
        {
            'closed_position': SimpleNamespace(
                amount=1_000.0,
                gross_pnl=12.0,
            )
        },
    )
    pnl = summary.to_dict()['pnl']
    assert pnl['estimated_costs'] is None
    assert pnl['net_estimated'] is None
    assert pnl['net_estimated_available'] is False
