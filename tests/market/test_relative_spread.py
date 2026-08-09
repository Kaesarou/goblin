from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.instruments.models import AssetClass, MarketContextConfig
from app.market.market_context import MarketContextService
from app.market.models import MarketSnapshot
from app.market.relative_spread import (
    SPREAD_CONTEXT_VERSION,
    SPREAD_REFERENCE_MAX_OBSERVATIONS,
    build_relative_spread_context,
    compact_relative_spread_history,
)
from app.runtime.trading_session_window import TradingSessionDecision

NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)


def _snapshot(spread_percent: float, index: int) -> MarketSnapshot:
    half = spread_percent / 2
    return MarketSnapshot(
        symbol='AAPL',
        bid=100.0 - half,
        ask=100.0 + half,
        last=100.0,
        timestamp=NOW + timedelta(seconds=index),
    )


class _Registry:
    def resolve(self, symbol):
        if symbol == 'AAPL':
            return SimpleNamespace(asset_class=AssetClass.EQUITY_US)
        raise ValueError(symbol)

    def config_for(self, symbol):
        return SimpleNamespace(market_context=MarketContextConfig())


def _decision() -> TradingSessionDecision:
    return TradingSessionDecision(
        asset_class=AssetClass.EQUITY_US,
        session_active=True,
        session_24_7=False,
        collect_snapshots=True,
        new_entries_allowed=True,
        force_close_required=False,
        reason='session_tradable',
        session_start_time=NOW,
        session_end_time=NOW + timedelta(hours=6),
        time_until_session_end_minutes=360,
        session_key='EQUITY_US:2026-08-04',
    )


def test_relative_spread_uses_prior_symbol_distribution():
    prior = [_snapshot(0.10, index) for index in range(20)]
    current = _snapshot(0.20, 20)

    context = build_relative_spread_context(
        current=current,
        prior_snapshots=prior,
    )

    assert context.version == SPREAD_CONTEXT_VERSION
    assert context.available is True
    assert context.current_percent == pytest.approx(0.20)
    assert context.reference_median_percent == pytest.approx(0.10)
    assert context.relative_to_median == pytest.approx(2.0)
    assert context.reference_percentile == pytest.approx(1.0)
    assert context.reference_observations == 20


def test_insufficient_history_is_explicitly_unavailable():
    context = build_relative_spread_context(
        current=_snapshot(0.20, 19),
        prior_snapshots=[_snapshot(0.10, index) for index in range(19)],
    )

    assert context.available is False
    assert context.current_percent == pytest.approx(0.20)
    assert context.reference_median_percent is None
    assert context.relative_to_median is None
    assert context.reference_observations == 19


def test_market_context_excludes_current_quote_from_spread_reference():
    service = MarketContextService(instrument_registry=_Registry())
    for index in range(20):
        service.update(
            snapshots={'AAPL': _snapshot(0.10, index)},
            session_decisions={'AAPL': _decision()},
        )
    current = _snapshot(1.0, 20)
    service.update(
        snapshots={'AAPL': current},
        session_decisions={'AAPL': _decision()},
    )

    context = service.build_candidate_context(
        symbol='AAPL',
        side='BUY',
        as_of=current.timestamp,
    )

    assert context.spread.available is True
    assert context.spread.current_percent == pytest.approx(1.0)
    assert context.spread.reference_median_percent == pytest.approx(0.10)
    assert context.spread.relative_to_median == pytest.approx(10.0)
    assert context.spread.reference_observations == 20


def test_market_context_tracks_every_accepted_quote_before_periodic_update():
    service = MarketContextService(instrument_registry=_Registry())
    for index in range(20):
        service.observe_accepted_snapshot(_snapshot(0.10, index))
    current = _snapshot(0.20, 20)
    service.observe_accepted_snapshot(current)

    context = service.build_candidate_context(
        symbol='AAPL',
        side='BUY',
        as_of=current.timestamp,
    )

    assert context.spread.current_percent == pytest.approx(0.20)
    assert context.spread.reference_median_percent == pytest.approx(0.10)
    assert context.spread.relative_to_median == pytest.approx(2.0)
    assert context.spread.reference_observations == 20


def test_periodic_context_update_does_not_duplicate_the_latest_quote():
    service = MarketContextService(instrument_registry=_Registry())
    for index in range(20):
        service.observe_accepted_snapshot(_snapshot(0.10, index))
    current = _snapshot(0.20, 20)
    service.observe_accepted_snapshot(current)
    service.update(
        snapshots={'AAPL': current},
        session_decisions={'AAPL': _decision()},
    )

    context = service.build_candidate_context(
        symbol='AAPL',
        side='BUY',
        as_of=current.timestamp,
    )

    assert context.spread.reference_observations == 20


def test_market_context_retains_full_reference_window_before_current_quote():
    service = MarketContextService(instrument_registry=_Registry())
    for index in range(SPREAD_REFERENCE_MAX_OBSERVATIONS):
        service.observe_accepted_snapshot(_snapshot(0.10, index))
    current = _snapshot(0.20, SPREAD_REFERENCE_MAX_OBSERVATIONS)
    service.observe_accepted_snapshot(current)

    context = service.build_candidate_context(
        symbol='AAPL',
        side='BUY',
        as_of=current.timestamp,
    )

    assert context.spread.reference_observations == 512


def test_offline_history_compacts_only_after_delayed_batch_is_scored():
    history = deque(
        _snapshot(0.10, index)
        for index in range(SPREAD_REFERENCE_MAX_OBSERVATIONS)
    )
    current = _snapshot(0.20, SPREAD_REFERENCE_MAX_OBSERVATIONS)
    history.extend(
        _snapshot(0.30, SPREAD_REFERENCE_MAX_OBSERVATIONS + index)
        for index in range(1, 9)
    )

    context = build_relative_spread_context(
        current=current,
        prior_snapshots=[
            snapshot
            for snapshot in history
            if snapshot.timestamp < current.timestamp
        ],
    )
    compact_relative_spread_history(history)

    assert context.reference_observations == 512
    assert len(history) == SPREAD_REFERENCE_MAX_OBSERVATIONS
