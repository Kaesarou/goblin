from types import SimpleNamespace

from app.journal.analysis_ready_summary import AnalysisReadySummaryAggregator


def test_analysis_ready_summary_distinguishes_context_and_trading_snapshots():
    summary = AnalysisReadySummaryAggregator(run_id='run-test')
    summary.record(
        'market_batch_validated',
        {'batch': SimpleNamespace(accepted={
            'AAPL': object(),
            'MSFT': object(),
            'SPX500': object(),
        })},
    )
    summary.record('market_snapshot', {'symbol': 'AAPL'})
    summary.record('market_snapshot', {'symbol': 'MSFT'})

    data = summary.to_dict()
    assert data['schema_version'] == 13
    assert data['market_data']['accepted'] == 3
    assert data['market_data']['trading_snapshots_processed'] == 2
    assert data['market_data']['context_snapshots_accepted'] == 1


def test_analysis_ready_summary_exposes_pr5e_scores_profiles_and_events():
    summary = AnalysisReadySummaryAggregator(run_id='run-test')
    analysis = SimpleNamespace(
        feasibility_score=24.0,
        score_contribution=-7.8,
        entry_freshness_score=18.0,
        hard_rejection_components=('cost_to_tp_absurd_hard_reject',),
        sl_tp_source='eu_trend_buy_v1',
    )
    candidate = SimpleNamespace(
        market_context_score=6.0,
        multi_timeframe_score=4.0,
        probability_score=34.0,
        touch_probability=0.40,
        direction_probability=0.425,
        direction_edge=-0.10,
    )
    probability = SimpleNamespace(
        profile_key='eu_trend_buy_v1',
        in_training_domain=True,
    )
    evaluated = SimpleNamespace(
        tp_feasibility=analysis,
        outcome_probability=probability,
        effective_sl_tp=SimpleNamespace(source='eu_trend_buy_v1'),
        candidate=candidate,
    )
    summary.record(
        'candidate_tp_feasibility',
        {'evaluated_candidates': [evaluated]},
    )
    summary.record(
        'candidate_outcome_probability',
        {'evaluated_candidates': [evaluated]},
    )
    summary.record(
        'entry_horizon_rejected',
        {
            'reason': 'insufficient_session_time_for_trade_horizon',
            'profile_key': 'eu_trend_buy_v1',
        },
    )
    summary.record(
        'managed_stop_updated',
        {'protection_type': 'net_breakeven'},
    )
    data = summary.to_dict()

    assert data['tp_feasibility']['hard_rejection_components'] == {
        'cost_to_tp_absurd_hard_reject': 1,
    }
    assert data['effective_sl_tp']['by_source'] == {
        'eu_trend_buy_v1': 1,
    }
    assert data['outcome_probability']['by_profile'] == {
        'eu_trend_buy_v1': 1,
    }
    assert data['outcome_probability']['training_domain'] == {
        'in_training_domain': 1,
    }
    contributions = data['score_contributions']
    assert sum(contributions['market_context'].values()) == 1
    assert sum(contributions['multi_timeframe'].values()) == 1
    assert sum(contributions['tp_feasibility_score'].values()) == 1
    assert sum(contributions['entry_freshness_score'].values()) == 1
    assert sum(contributions['probability_score'].values()) == 1
    assert sum(contributions['touch_probability'].values()) == 1
    assert sum(contributions['direction_probability'].values()) == 1
    assert sum(contributions['direction_edge'].values()) == 1
    assert data['entry_horizon']['rejections_by_profile'] == {
        'eu_trend_buy_v1': 1,
    }
    assert data['managed_stops']['updates_by_type'] == {
        'net_breakeven': 1,
    }


def test_hold_decisions_do_not_count_as_risk_rejections():
    summary = AnalysisReadySummaryAggregator(run_id='run-test')
    summary.record(
        'decision',
        {
            'candidate': None,
            'trade_plan': SimpleNamespace(approved=False, reason='no_signal'),
        },
    )
    summary.record(
        'decision',
        {
            'candidate': SimpleNamespace(candidate_id='candidate-1'),
            'trade_plan': SimpleNamespace(
                approved=False,
                reason='max_open_positions_reached',
            ),
        },
    )
    data = summary.to_dict()
    assert data['decision_pipeline']['risk_rejected'] == 1
