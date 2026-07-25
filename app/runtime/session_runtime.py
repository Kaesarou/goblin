from datetime import UTC, datetime

from app.instruments.instrument_registry import InstrumentRegistry
from app.runtime.trading_session_window import (
    TradingSessionDecision,
    TradingSessionService,
    TradingSessionState,
)

SNAPSHOT_BEFORE_SESSION_START = 'snapshot_before_session_start'
SNAPSHOT_AT_OR_AFTER_SESSION_END = 'snapshot_at_or_after_session_end'
SESSION_NOT_COLLECTING = 'session_not_collecting'


def session_timestamp_rejection_reason(
    *,
    decision: TradingSessionDecision,
    timestamp: datetime,
) -> str | None:
    if not decision.session_active or not decision.collect_snapshots:
        return SESSION_NOT_COLLECTING
    if decision.session_24_7:
        return None

    session_start = decision.session_start_time
    session_end = decision.session_end_time
    if session_start is None or session_end is None:
        raise ValueError(
            'A finite active trading session requires start and end timestamps.'
        )

    actual_timestamp = _as_utc(timestamp)
    if actual_timestamp < _as_utc(session_start):
        return SNAPSHOT_BEFORE_SESSION_START
    if actual_timestamp >= _as_utc(session_end):
        return SNAPSHOT_AT_OR_AFTER_SESSION_END
    return None


def filter_symbols_by_trading_session(
    *,
    symbols: list[str],
    instrument_registry: InstrumentRegistry,
    trading_session_service: TradingSessionService,
    trading_session_state: TradingSessionState,
    now: datetime,
):
    symbols_to_fetch: list[str] = []
    session_decisions = {}
    started_session_symbols: list[str] = []
    closed_session_keys: set[str] = set()

    for symbol in symbols:
        asset_class = instrument_registry.resolve(symbol).asset_class
        decision = trading_session_service.evaluate(asset_class=asset_class, now=now)
        session_decisions[symbol] = decision

        started, closed_session_key = trading_session_state.mark_and_detect_transition(
            symbol=symbol,
            decision=decision,
        )
        if started:
            started_session_symbols.append(symbol)
        if closed_session_key is not None:
            closed_session_keys.add(closed_session_key)

        if decision.collect_snapshots:
            symbols_to_fetch.append(symbol)

    return symbols_to_fetch, session_decisions, started_session_symbols, closed_session_keys


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
