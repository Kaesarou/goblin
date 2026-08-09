from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.instruments.models import AssetClass, MarketContextConfig
from app.market.market_context import MARKET_CONTEXT_VERSION, MarketContextService
from app.market.models import MarketSnapshot
from app.runtime.trading_session_window import TradingSessionDecision

START = datetime(2026, 7, 14, 13, 30, tzinfo=timezone.utc)
SESSION_KEY = 'EQUITY_US:2026-07-14'


class RegistryStub:
    def resolve(self, symbol):
        if symbol == 'AAPL':
            return SimpleNamespace(asset_class=AssetClass.EQUITY_US)
        raise ValueError(symbol)

    def config_for(self, symbol):
        return SimpleNamespace(
            market_context=MarketContextConfig(
                minimum_breadth_sample_size=1,
                minimum_sector_sample_size=1,
                momentum_window_seconds=180,
                maximum_context_age_seconds=120,
            )
        )


def decision():
    return TradingSessionDecision(
        asset_class=AssetClass.EQUITY_US,
        session_active=True,
        session_24_7=False,
        collect_snapshots=True,
        new_entries_allowed=True,
        force_close_required=False,
        reason='session_tradable',
        session_start_time=START,
        session_end_time=START + timedelta(hours=6, minutes=30),
        time_until_session_end_minutes=390,
        session_key=SESSION_KEY,
    )


def snapshot(symbol, last, timestamp):
    return MarketSnapshot(
        symbol=symbol,
        bid=last - 0.01,
        ask=last + 0.01,
        last=last,
        timestamp=timestamp,
    )


def update(service, timestamp, aapl, spx500):
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', aapl, timestamp),
            'SPX500': snapshot('SPX500', spx500, timestamp),
        },
        session_decisions={'AAPL': decision()},
        context_asset_classes={'SPX500': AssetClass.EQUITY_US},
    )


def test_benchmark_momentum_uses_the_configured_rolling_window():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPX500',)},
    )
    update(service, START, aapl=100.0, spx500=500.0)
    update(service, START + timedelta(minutes=1), aapl=100.4, spx500=505.0)
    now = START + timedelta(minutes=4)
    update(service, now, aapl=101.0, spx500=510.0)

    context = service.build_candidate_context(symbol='AAPL', side='BUY', as_of=now)

    assert context.version == MARKET_CONTEXT_VERSION == 'market_context_v3'
    assert context.benchmark.session_return_percent == 2.0
    assert context.benchmark.momentum_percent == 0.9901
    assert context.benchmark.momentum_percent != context.benchmark.session_return_percent


def test_benchmark_momentum_is_unknown_when_reference_is_too_old():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPX500',)},
    )
    update(service, START, aapl=100.0, spx500=500.0)
    now = START + timedelta(minutes=10)
    update(service, now, aapl=101.0, spx500=510.0)

    context = service.build_candidate_context(symbol='AAPL', side='BUY', as_of=now)

    assert context.benchmark.available is True
    assert context.benchmark.session_return_percent == 2.0
    assert context.benchmark.momentum_percent is None


def test_session_reset_removes_the_previous_session_momentum_history():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPX500',)},
    )
    update(service, START, aapl=100.0, spx500=500.0)
    update(service, START + timedelta(minutes=3), aapl=101.0, spx500=505.0)

    service.reset_session(SESSION_KEY)
    now = START + timedelta(minutes=4)
    update(service, now, aapl=102.0, spx500=506.0)
    context = service.build_candidate_context(symbol='AAPL', side='BUY', as_of=now)

    assert context.benchmark.session_return_percent == 0.0
    assert context.benchmark.momentum_percent is None
