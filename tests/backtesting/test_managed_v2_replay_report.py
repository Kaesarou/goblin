from scripts.replay_managed_v2 import (
    MANAGED_V1_HISTORICAL_SCENARIO,
    MANAGED_V1_LIVE_VALIDATION_SCENARIO,
    MANAGED_V1_QUALITY_MATCHED_SCENARIO,
    MANAGED_V2_REPLAY_SCENARIOS,
    MANAGED_V2_SCENARIO,
    _aggregate_fill_extraction,
    _daily_results_markdown,
    _day_comparison,
    _quote_quality_effect_markdown,
)


def _result(realized_net, candidate_ids):
    return {
        'pnl': {'realized_net': realized_net},
        'trades': {
            'count': len(candidate_ids),
            'details': [
                {'candidate_id': candidate_id}
                for candidate_id in candidate_ids
            ],
        },
    }


def test_primary_v2_comparison_uses_quote_quality_matched_v1():
    comparison = _day_comparison(
        {
            MANAGED_V1_HISTORICAL_SCENARIO: _result(1.0, ['historical']),
            MANAGED_V1_QUALITY_MATCHED_SCENARIO: _result(
                2.0,
                ['shared'],
            ),
            MANAGED_V2_SCENARIO: _result(5.0, ['shared', 'v2-only']),
        }
    )

    assert comparison['reference_scenario'] == (
        MANAGED_V1_QUALITY_MATCHED_SCENARIO
    )
    assert comparison['realized_net_delta'] == 3.0
    assert comparison['overlap'] == 1
    assert comparison['v2_added'] == ['v2-only']
    assert comparison['v2_removed'] == []
    assert comparison['historical_v1_reference'][
        'realized_net_delta'
    ] == 4.0


def test_report_keeps_live_validation_separate_and_aggregates_fill_coverage():
    assert MANAGED_V1_LIVE_VALIDATION_SCENARIO in MANAGED_V2_REPLAY_SCENARIOS
    days = [
        {
            'historical_fill_extraction': {
                'position_open_events': 3,
                'broker_entry_fills': 2,
                'broker_exit_fills': 1,
                'missing_candidate_ids': 0,
                'ambiguous_legacy_entry_prices': 1,
            }
        },
        {
            'historical_fill_extraction': {
                'position_open_events': 4,
                'broker_entry_fills': 4,
                'broker_exit_fills': 3,
                'missing_candidate_ids': 0,
                'ambiguous_legacy_entry_prices': 0,
            }
        },
    ]

    assert _aggregate_fill_extraction(days) == {
        'position_open_events': 7,
        'broker_entry_fills': 6,
        'broker_exit_fills': 4,
        'missing_candidate_ids': 0,
        'ambiguous_legacy_entry_prices': 1,
    }


def test_quote_quality_markdown_isolates_changed_trade_outcomes():
    report = {
        'aggregates': {
            MANAGED_V1_HISTORICAL_SCENARIO: {
                'realized_net': -9.3852,
                'quote_quality_quarantined': 0,
                'trade_ids': ['candidate-googl'],
            },
            MANAGED_V1_QUALITY_MATCHED_SCENARIO: {
                'realized_net': -3.3154,
                'quote_quality_quarantined': 42,
                'trade_ids': ['candidate-googl'],
            },
        },
        'days': [
            {
                'date': '2026-08-04',
                'scenarios': {
                    MANAGED_V1_HISTORICAL_SCENARIO: _quote_result(
                        net_pnl=-9.3852,
                        close_reason='initial_stop',
                    ),
                    MANAGED_V1_QUALITY_MATCHED_SCENARIO: _quote_result(
                        net_pnl=-3.3154,
                        close_reason='stale_exit',
                    ),
                },
            }
        ],
    }

    lines = _quote_quality_effect_markdown(report)

    assert 'aucun identifiant de trade ne change' in lines[0]
    assert 'net V1 varie de +6.0698' in lines[0]
    assert '`initial_stop` -9.3852 vers `stale_exit` -3.3154' in lines[4]


def test_daily_markdown_exposes_live_validation_and_incomplete_days():
    scenarios = {
        name: {'pnl': {'realized_net': float(index)}}
        for index, name in enumerate(MANAGED_V2_REPLAY_SCENARIOS, start=1)
    }
    lines = _daily_results_markdown(
        {
            'days': [
                {
                    'date': '2026-07-28',
                    'incomplete': True,
                    'scenarios': scenarios,
                    'comparison': {'realized_net_delta': -3.0},
                }
            ]
        }
    )

    assert 'V1 validation live' in lines[0]
    assert '| 2026-07-28* | 1.0000 | 2.0000 | 3.0000 | 4.0000 | -3.0000 |' in lines
    assert any('Journée incomplète' in line for line in lines)


def _quote_result(*, net_pnl, close_reason):
    return {
        'trades': {
            'details': [
                {
                    'candidate_id': 'candidate-googl',
                    'symbol': 'GOOGL',
                    'net_pnl': net_pnl,
                    'close_reason': close_reason,
                }
            ]
        }
    }
