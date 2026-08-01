from types import SimpleNamespace

from app.journal.journal_policy import (
    should_write_to_debug_journal,
    should_write_to_errors_journal,
    should_write_to_trade_journal,
)


def test_normal_level_does_not_write_hold_decisions_to_trades():
    payload = {
        'signal': SimpleNamespace(action='HOLD'),
        'trade_plan': SimpleNamespace(approved=False, reason='market_regime_dead_market'),
    }

    assert not should_write_to_trade_journal('decision', payload, 'normal')


def test_debug_level_routes_hold_decisions_to_debug_journal():
    payload = {
        'signal': SimpleNamespace(action='HOLD'),
        'trade_plan': SimpleNamespace(approved=False, reason='market_regime_dead_market'),
    }

    assert should_write_to_debug_journal('decision', payload, 'debug')


def test_minimal_level_keeps_order_and_position_events():
    assert should_write_to_trade_journal('order_submitted', {}, 'minimal')
    assert should_write_to_trade_journal('order_filled', {}, 'minimal')
    assert should_write_to_trade_journal('position_opened', {}, 'minimal')
    assert should_write_to_trade_journal(
        'position_close_confirmed', {}, 'minimal'
    )


def test_error_events_are_routed_to_errors_journal_not_trade_journal():
    assert should_write_to_errors_journal('candidate_execution_error')
    assert not should_write_to_trade_journal('candidate_execution_error', {}, 'normal')
