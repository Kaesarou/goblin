import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.journal.analysis_journal import AnalysisJournal
from app.journal.jsonl_journal import JsonlJournal


def test_candidate_selection_emits_standalone_entry_route_event(tmp_path):
    trades_path = tmp_path / 'trades.jsonl'
    journal = AnalysisJournal(
        trade_journal=JsonlJournal(str(trades_path)),
        errors_journal=JsonlJournal(str(tmp_path / 'errors.jsonl')),
        summary_path=str(tmp_path / 'summary.json'),
        detail_level='normal',
        write_partial_summary=False,
        profile='balanced',
    )
    context = SimpleNamespace(version='market_context_v2', regime='risk_on')
    multi_timeframe_context = SimpleNamespace(
        model_version='multi_timeframe_features_v2',
        ready_alignment='aligned',
        alignment_including_provisional='mixed',
        maturity_by_timeframe={'m1': 'ready', 'm5': 'provisional'},
        features_by_timeframe={'m1': {'direction': 'up'}},
        unavailable_timeframes=('m15', 'm30', 'h1'),
        opening_ranges=SimpleNamespace(windows={}),
    )
    timestamp = datetime(2026, 7, 14, 10, 32, 8, tzinfo=timezone.utc)
    candidate = SimpleNamespace(
        candidate_id='candidate-2',
        origin_candidate_id='candidate-origin',
        pending_entry_id='pending-1',
        symbol='AAPL',
        segment=None,
        signal=SimpleNamespace(action='BUY', metadata={}),
        snapshot=SimpleNamespace(
            bid=201.20,
            ask=201.30,
            last=201.25,
            timestamp=timestamp,
        ),
        probability_score=34.0,
        touch_probability=0.40,
        direction_probability=0.425,
        tp_probability=0.17,
        sl_probability=0.23,
        neither_probability=0.60,
        direction_break_even_probability=0.55,
        direction_edge=-0.125,
        outcome_probability_model_version='outcome_probability_v1',
        managed_protection_probability=0.60,
        managed_positive_probability=0.50,
        managed_expected_net_return_percent=0.12,
        managed_edge=0.07,
        managed_outcome_model_version='managed_outcome_v1',
        base_score=132.0,
        rank_reason='test',
        market_context=context,
        multi_timeframe_context=multi_timeframe_context,
    )
    decision = SimpleNamespace(
        action='ready_for_selection',
        reason='entry_conditions_satisfied',
        model_version='entry_router_v3',
    )
    evaluated = SimpleNamespace(
        candidate=candidate,
        entry_decision=decision,
        economics=SimpleNamespace(
            expected_net_profit_percent=0.4,
            estimated_total_cost_percent=0.35,
        ),
        tp_feasibility=SimpleNamespace(runway_score=80.0),
        outcome_probability=SimpleNamespace(
            profile_key='us_intraday_fixed_v1',
        ),
        effective_sl_tp=SimpleNamespace(
            take_profit_percent=1.6,
            stop_loss_percent=0.9,
        ),
    )

    journal.write(
        'candidate_selection',
        {
            'selected_candidates': [candidate],
            'rejected_candidates': [],
            'selected_evaluated_candidates': [evaluated],
            'rejected_evaluated_candidates': [],
        },
    )

    records = [
        json.loads(line)
        for line in trades_path.read_text(encoding='utf-8').splitlines()
    ]
    record = next(
        record for record in records if record['event_type'] == 'entry_decision'
    )
    payload = record['payload']
    assert payload['candidate_id'] == 'candidate-2'
    assert payload['origin_candidate_id'] == 'candidate-origin'
    assert payload['pending_entry_id'] == 'pending-1'
    assert payload['candidate_timestamp'] == timestamp.isoformat()
    assert payload['schema_version'] == 2
    assert payload['entry_reference_price'] == 201.25
    assert payload['bid'] == 201.20
    assert payload['ask'] == 201.30
    assert payload['last'] == 201.25
    assert payload['spread'] == 0.0497
    assert payload['executable_entry_price'] == 201.30
    assert payload['effective_stop_loss_percent'] == 0.9
    assert payload['effective_take_profit_percent'] == 1.6
    assert payload['estimated_total_cost_percent'] == 0.35
    assert payload['probability_score'] == 34.0
    assert payload['tp_probability'] == 0.17
    assert payload['direction_probability'] == 0.425
    assert payload['direction_edge'] == -0.125
    assert payload['managed_protection_probability'] == 0.60
    assert payload['managed_positive_probability'] == 0.50
    assert payload['managed_expected_net_return_percent'] == 0.12
    assert payload['managed_edge'] == 0.07
    assert payload['managed_outcome_model_version'] == 'managed_outcome_v1'
    assert payload['base_score'] == 132.0
    assert payload['entry_route_action'] == 'ready_for_selection'
    assert payload['entry_route_reason'] == 'entry_conditions_satisfied'
    assert payload['selection_outcome'] == 'selected'
    assert payload['entry_route_model_version'] == 'entry_router_v3'
    assert payload['market_context_version'] == 'market_context_v2'
    assert payload['multi_timeframe_model_version'] == 'multi_timeframe_features_v2'
    assert payload['multi_timeframe_context']['ready_alignment'] == 'aligned'
    assert payload['strategy_profile'] == 'balanced'
    assert payload['selection_policy_version'] == 'managed_edge_v1'
    assert payload['segment'] is None
    assert 'entry_action' not in payload
    assert 'entry_reason' not in payload
