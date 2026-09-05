from datetime import datetime, timezone

from app.instruments.models import AssetClass
from app.runtime.trading_session_window import (
    AssetTradingSessionConfig,
    TradingSessionService,
    parse_trading_sessions,
)


def service(raw_sessions: str) -> TradingSessionService:
    return TradingSessionService(
        configs={
            AssetClass.EQUITY_US: AssetTradingSessionConfig(
                asset_class=AssetClass.EQUITY_US,
                sessions=parse_trading_sessions(raw_sessions),
            )
        },
        timezone_name='Europe/Paris',
    )


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 6, hour, minute, tzinfo=timezone.utc)


def test_empty_session_means_24_7():
    decision = service('').evaluate(asset_class=AssetClass.EQUITY_US, now=at(2))

    assert decision.session_24_7
    assert decision.collect_snapshots
    assert decision.new_entries_allowed
    assert not decision.force_close_required
    assert decision.reason == 'session_tradable'


def test_configured_session_is_closed_before_start():
    decision = service('09:00-15:00').evaluate(asset_class=AssetClass.EQUITY_US, now=at(6))

    assert not decision.session_active
    assert not decision.collect_snapshots
    assert decision.reason == 'session_closed'


def test_configured_session_is_tradable_when_active():
    decision = service('09:00-15:00').evaluate(asset_class=AssetClass.EQUITY_US, now=at(8))

    assert decision.session_active
    assert decision.collect_snapshots
    assert decision.new_entries_allowed
    assert decision.reason == 'session_tradable'


def test_session_semantic_equality_and_inequality_ignore_countdown_only():
    session_service = service('09:00-15:00')
    first = session_service.evaluate(
        asset_class=AssetClass.EQUITY_US,
        now=at(8, 0),
    )
    second = session_service.evaluate(
        asset_class=AssetClass.EQUITY_US,
        now=at(8, 1),
    )

    assert first.time_until_session_end_minutes != second.time_until_session_end_minutes
    assert first == second
    assert not first != second
    assert hash(first) == hash(second)

    cutoff_state = session_service.evaluate(
        asset_class=AssetClass.EQUITY_US,
        now=at(12, 30),
    )
    assert first != cutoff_state


def test_new_entries_are_blocked_during_last_hour():
    decision = service('09:00-15:00').evaluate(asset_class=AssetClass.EQUITY_US, now=at(12, 30))

    assert decision.collect_snapshots
    assert not decision.new_entries_allowed
    assert not decision.force_close_required
    assert decision.reason == 'too_close_to_session_end'


def test_force_close_is_required_during_last_twenty_minutes():
    decision = service('09:00-15:00').evaluate(asset_class=AssetClass.EQUITY_US, now=at(12, 50))

    assert decision.collect_snapshots
    assert not decision.new_entries_allowed
    assert decision.force_close_required
    assert decision.reason == 'force_close_before_session_end'


def test_session_crossing_midnight_is_active_after_midnight():
    decision = service('23:00-09:00').evaluate(asset_class=AssetClass.EQUITY_US, now=datetime(2026, 7, 7, 1, tzinfo=timezone.utc))

    assert decision.session_active
    assert decision.collect_snapshots
    assert decision.reason == 'session_tradable'


def test_multiple_sessions_are_supported():
    decision = service('09:00-12:00,13:00-15:00').evaluate(
        asset_class=AssetClass.EQUITY_US,
        now=at(11, 30),
    )

    assert decision.session_active
    assert decision.reason == 'session_tradable'
