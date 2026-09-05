from datetime import UTC, datetime

from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.runtime.session_runtime import (
    SESSION_NOT_COLLECTING,
    SNAPSHOT_AT_OR_AFTER_SESSION_END,
    SNAPSHOT_BEFORE_SESSION_START,
    filter_symbols_by_trading_session,
    session_timestamp_rejection_reason,
)
from app.runtime.trading_session_window import (
    AssetTradingSessionConfig,
    TradingSessionDecision,
    TradingSessionService,
    TradingSessionState,
    parse_trading_sessions,
)


def test_filter_symbols_by_trading_session_keeps_only_active_symbols():
    settings = Settings(
        EQUITY_US_SYMBOLS='AAPL',
        EQUITY_EU_SYMBOLS='AIR.PA',
    )
    registry = InstrumentRegistry(settings)
    service = TradingSessionService(
        configs={
            AssetClass.EQUITY_US: AssetTradingSessionConfig(
                asset_class=AssetClass.EQUITY_US,
                sessions=parse_trading_sessions('15:00-22:00'),
            ),
            AssetClass.EQUITY_EU: AssetTradingSessionConfig(
                asset_class=AssetClass.EQUITY_EU,
                sessions=parse_trading_sessions('09:00-12:00'),
            ),
            AssetClass.CRYPTO: AssetTradingSessionConfig(
                asset_class=AssetClass.CRYPTO,
                sessions=(),
            ),
        },
        timezone_name='Europe/Paris',
    )

    symbols_to_fetch, decisions, _, reset_keys = filter_symbols_by_trading_session(
        symbols=['AAPL', 'AIR.PA'],
        instrument_registry=registry,
        trading_session_service=service,
        trading_session_state=TradingSessionState(),
        now=datetime(2026, 7, 6, 14, 0, tzinfo=UTC),
    )

    assert symbols_to_fetch == ['AAPL']
    assert decisions['AAPL'].collect_snapshots
    assert not decisions['AIR.PA'].collect_snapshots
    assert reset_keys == set()


def _session_decision(
    *,
    start: datetime | None,
    end: datetime | None,
    active: bool = True,
    collect: bool = True,
    session_24_7: bool = False,
) -> TradingSessionDecision:
    return TradingSessionDecision(
        asset_class=AssetClass.EQUITY_US,
        session_active=active,
        session_24_7=session_24_7,
        collect_snapshots=collect,
        new_entries_allowed=active,
        force_close_required=False,
        reason='test',
        session_start_time=start,
        session_end_time=end,
        time_until_session_end_minutes=60.0 if active else None,
        session_key='test-session' if active else None,
    )


def test_session_timestamp_uses_half_open_finite_interval():
    start = datetime(2026, 7, 23, 13, 30, tzinfo=UTC)
    end = datetime(2026, 7, 23, 20, 0, tzinfo=UTC)
    decision = _session_decision(start=start, end=end)

    assert (
        session_timestamp_rejection_reason(
            decision=decision,
            timestamp=start.replace(tzinfo=None),
        )
        is None
    )
    assert (
        session_timestamp_rejection_reason(
            decision=decision,
            timestamp=start,
        )
        is None
    )
    assert (
        session_timestamp_rejection_reason(
            decision=decision,
            timestamp=start.replace(microsecond=1),
        )
        is None
    )
    assert (
        session_timestamp_rejection_reason(
            decision=decision,
            timestamp=start.replace(second=59, minute=29),
        )
        == SNAPSHOT_BEFORE_SESSION_START
    )
    assert (
        session_timestamp_rejection_reason(
            decision=decision,
            timestamp=end,
        )
        == SNAPSHOT_AT_OR_AFTER_SESSION_END
    )


def test_session_timestamp_accepts_24_7_and_rejects_closed_session():
    timestamp = datetime(2026, 7, 23, 13, 30, tzinfo=UTC)

    assert (
        session_timestamp_rejection_reason(
            decision=_session_decision(
                start=None,
                end=None,
                session_24_7=True,
            ),
            timestamp=timestamp,
        )
        is None
    )
    assert (
        session_timestamp_rejection_reason(
            decision=_session_decision(
                start=None,
                end=None,
                active=False,
                collect=False,
            ),
            timestamp=timestamp,
        )
        == SESSION_NOT_COLLECTING
    )
