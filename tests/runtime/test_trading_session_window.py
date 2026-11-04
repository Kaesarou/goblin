from datetime import datetime, timedelta, timezone

import pytest

from app.config.settings import Settings
from app.instruments.models import AssetClass
from app.runtime.trading_session_window import (
    AssetTradingSessionConfig,
    TradingSessionService,
    parse_trading_sessions,
    trading_session_service_from_settings,
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


@pytest.mark.parametrize(
    ('day', 'us_start_hour', 'us_end_hour', 'eu_start_hour', 'eu_end_hour'),
    [
        ('2026-03-09', 13, 20, 8, 16),
        ('2026-03-30', 13, 20, 7, 15),
        ('2026-10-26', 13, 20, 8, 16),
        ('2026-10-30', 13, 20, 8, 16),
        ('2026-11-02', 14, 21, 8, 16),
    ],
)
def test_market_windows_follow_their_own_dst(
    day, us_start_hour, us_end_hour, eu_start_hour, eu_end_hour,
):
    settings = Settings(
        _env_file=None,
        TRADING_SESSION_TIMEZONE='Europe/Paris',
        TRADING_SESSION_TIMEZONE_EQUITY_US='America/New_York',
        TRADING_SESSIONS_EQUITY_US='09:30-16:00',
        TRADING_SESSION_TIMEZONE_EQUITY_EU='Europe/Paris',
        TRADING_SESSIONS_EQUITY_EU='09:00-17:30',
    )
    session_service = trading_session_service_from_settings(settings)
    expected = {
        AssetClass.EQUITY_US: ((us_start_hour, 30), (us_end_hour, 0)),
        AssetClass.EQUITY_EU: ((eu_start_hour, 0), (eu_end_hour, 30)),
    }
    midnight = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    for asset_class, (open_time, close_time) in expected.items():
        start = midnight + timedelta(hours=open_time[0], minutes=open_time[1])
        end = midnight + timedelta(hours=close_time[0], minutes=close_time[1])
        evaluate = session_service.evaluate
        assert not evaluate(asset_class=asset_class, now=start - timedelta(seconds=1)).collect_snapshots
        opening = evaluate(asset_class=asset_class, now=start)
        assert opening.collect_snapshots and opening.new_entries_allowed
        assert opening.session_start_time.astimezone(timezone.utc) == start
        assert opening.session_end_time.astimezone(timezone.utc) == end
        cutoff = evaluate(asset_class=asset_class, now=end - timedelta(hours=1))
        assert cutoff.collect_snapshots and not cutoff.new_entries_allowed
        assert not evaluate(asset_class=asset_class, now=end).collect_snapshots


@pytest.mark.parametrize(
    ('timezone_name', 'sessions', 'opening_hour'),
    [('America/New_York', '09:30-16:00', 13), ('Europe/Paris', '15:30-22:00', 14)],
)
def test_market_without_override_uses_configured_legacy_timezone(
    timezone_name, sessions, opening_hour,
):
    settings = Settings(
        _env_file=None,
        TRADING_SESSION_TIMEZONE=timezone_name,
        TRADING_SESSION_TIMEZONE_EQUITY_US=None,
        TRADING_SESSIONS_EQUITY_US=sessions,
    )
    opening = datetime(2026, 10, 26, opening_hour, 30, tzinfo=timezone.utc)
    decision = trading_session_service_from_settings(settings).evaluate(
        asset_class=AssetClass.EQUITY_US, now=opening,
    )
    assert decision.session_start_time == opening
