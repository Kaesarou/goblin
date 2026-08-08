import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.execution.strategy_segment import StrategySegment
from app.journal.analysis_journal import AnalysisJournal, _entry_decision_record
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
            'selection_policy_version': 'managed_edge_v1',
            'managed_v2_shadow_policy_version': None,
        },
    )

    records = [
        json.loads(line)
        for line in trades_path.read_text(encoding='utf-8').splitlines()
    ]
    record = next(record for record in records if record['event_type'] == 'entry_decision')
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
    assert payload['managed_v2_shadow_policy_version'] is None
    assert payload['feature_contract_version'] is None
    assert payload['label_contract_version'] is None
    assert payload['segment'] is None
    assert 'entry_action' not in payload
    assert 'entry_reason' not in payload


def test_managed_v2_shadow_fields_are_versioned_without_repurposing_canonical_fields():
    timestamp = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)
    managed_v2 = {
        'selection_policy_version': 'managed_v2_segment_first_v1',
        'feature_contract_version': 'managed_v2_features_v1',
        'label_contract_version': 'managed_v2_labels_v1',
        'opportunity_model_version': 'managed_v2_opportunity_20260808',
        'path_model_version': 'managed_v2_path_20260808',
        'economics_model_version': 'managed_v2_economics_20260808',
        'artifact_sha256': 'artifact-sha',
        'deployment_status': 'shadow_not_approved',
        'gate_outcome': 'rejected',
        'gate_rejection_reason': 'candidate_selection_path_below_floor',
        'shadow_selection_outcome': 'rejected',
        'shadow_selection_reason': 'candidate_selection_path_below_floor',
        'floors': {
            'opportunity_probability': 0.45,
            'path_probability': 0.60,
            'expected_net_return_percent': 0.0,
        },
    }
    candidate = SimpleNamespace(
        candidate_id='candidate-v2',
        origin_candidate_id='candidate-v2',
        pending_entry_id=None,
        symbol='GOOGL',
        segment=StrategySegment.EQUITY_US_BUY,
        signal=SimpleNamespace(action='BUY', metadata={}),
        snapshot=SimpleNamespace(
            bid=376.62,
            ask=376.70,
            last=376.66,
            timestamp=timestamp,
        ),
        base_score=120.0,
        probability_score=40.0,
        touch_probability=0.70,
        managed_v2_opportunity_probability=0.65,
        managed_v2_path_probability=0.55,
        managed_v2_expected_net_return_percent=0.12,
        managed_v2_ranking_score=0.0429,
        managed_v2_metadata=managed_v2,
        market_context=SimpleNamespace(
            version='market_context_v3',
            spread=SimpleNamespace(
                available=True,
                relative_to_median=1.20,
                reference_percentile=0.80,
                recent_change_ratio=0.10,
            ),
        ),
        multi_timeframe_context=None,
    )
    evaluated = SimpleNamespace(
        candidate=candidate,
        entry_decision=SimpleNamespace(
            action='ready_for_selection',
            reason='entry_conditions_satisfied',
            model_version='entry_router_v3',
        ),
        economics=SimpleNamespace(
            expected_net_profit_percent=0.64,
            estimated_total_cost_percent=0.36,
        ),
        effective_sl_tp=SimpleNamespace(
            take_profit_percent=1.0,
            stop_loss_percent=0.70,
            source='us_intraday_fixed_v1',
        ),
        tp_feasibility=None,
        outcome_probability=None,
    )

    payload = _entry_decision_record(
        evaluated=evaluated,
        selection_outcome='selected',
        selection_reason=None,
        strategy_profile='balanced',
        selection_policy_version='managed_edge_v1',
        shadow_policy_version='managed_v2_segment_first_v1',
        deployment_status='shadow_not_approved',
    )

    assert payload['entry_reference_price'] == 376.66
    assert payload['selection_outcome'] == 'selected'
    assert payload['selection_policy_version'] == 'managed_edge_v1'
    assert payload['managed_v2_shadow_policy_version'] == (
        'managed_v2_segment_first_v1'
    )
    assert payload['managed_v2_shadow_selection_outcome'] == 'rejected'
    assert payload['managed_v2_shadow_selection_reason'] == (
        'candidate_selection_path_below_floor'
    )
    assert payload['managed_v2_deployment_status'] == 'shadow_not_approved'
    assert payload['managed_v2_gate_outcome'] == 'rejected'
    assert payload['managed_v2_gate_rejection_reason'] == (
        'candidate_selection_path_below_floor'
    )
    assert payload['segment'] == 'EQUITY_US_BUY'
    assert payload['feature_contract_version'] == 'managed_v2_features_v1'
    assert payload['label_contract_version'] == 'managed_v2_labels_v1'
    assert payload['relative_spread_ratio'] == 1.20
