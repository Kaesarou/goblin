from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.instruments.models import AssetClass, MarketContextConfig
from app.market.market_context import (
    ContextAlignment,
    MarketContextService,
    MarketDirection,
    MarketRegime,
)
from app.market.models import MarketSnapshot
from app.runtime.trading_session_window import TradingSessionDecision

NOW = datetime(2026, 7, 14, 14, 0, tzinfo=timezone.utc)


class RegistryStub:
    def resolve(self, symbol):
        if symbol in {'AAPL', 'MSFT'}:
            return SimpleNamespace(asset_class=AssetClass.EQUITY_US)
        raise ValueError(symbol)

    def config_for(self, symbol):
        return SimpleNamespace(
            market_context=MarketContextConfig(
                minimum_breadth_sample_size=2,
                minimum_sector_sample_size=2,
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
        session_start_time=NOW,
        session_end_time=NOW + timedelta(hours=6),
        time_until_session_end_minutes=360,
        session_key='EQUITY_US:2026-07-14',
    )


def snapshot(symbol, last, timestamp=NOW):
    return MarketSnapshot(symbol, last - 0.01, last + 0.01, last, timestamp)


def test_builds_aligned_risk_on_context_from_benchmark_and_breadth():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPY',)},
    )
    decisions = {'AAPL': decision(), 'MSFT': decision()}
    context_assets = {'SPY': AssetClass.EQUITY_US}
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 100.0),
            'MSFT': snapshot('MSFT', 200.0),
            'SPY': snapshot('SPY', 500.0),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )
    later = NOW + timedelta(minutes=1)
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 101.0, later),
            'MSFT': snapshot('MSFT', 201.0, later),
            'SPY': snapshot('SPY', 502.0, later),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )

    context = service.build_candidate_context(symbol='AAPL', side='BUY', as_of=later)

    assert context.benchmark.available is True
    assert context.benchmark.direction == MarketDirection.BULLISH
    assert context.breadth.available is True
    assert context.breadth.advancing_ratio == 1.0
    assert context.regime == MarketRegime.RISK_ON
    assert context.alignment == ContextAlignment.ALIGNED
    assert context.symbol_relative_strength_percent == 0.6


def test_sell_is_opposed_in_risk_on_context():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPY',)},
    )
    decisions = {'AAPL': decision(), 'MSFT': decision()}
    service.update(
        snapshots={'AAPL': snapshot('AAPL', 100.0), 'MSFT': snapshot('MSFT', 200.0), 'SPY': snapshot('SPY', 500.0)},
        session_decisions=decisions,
        context_asset_classes={'SPY': AssetClass.EQUITY_US},
    )
    later = NOW + timedelta(minutes=1)
    service.update(
        snapshots={'AAPL': snapshot('AAPL', 101.0, later), 'MSFT': snapshot('MSFT', 201.0, later), 'SPY': snapshot('SPY', 502.0, later)},
        session_decisions=decisions,
        context_asset_classes={'SPY': AssetClass.EQUITY_US},
    )

    context = service.build_candidate_context(symbol='AAPL', side='SELL', as_of=later)

    assert context.alignment == ContextAlignment.OPPOSED


def test_side_neutral_research_context_excludes_equal_and_future_market_data():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPY',)},
    )
    decisions = {'AAPL': decision(), 'MSFT': decision()}
    context_assets = {'SPY': AssetClass.EQUITY_US}
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 100.0),
            'MSFT': snapshot('MSFT', 200.0),
            'SPY': snapshot('SPY', 500.0),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )
    cutoff = NOW + timedelta(minutes=1)
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 10_000.0, cutoff),
            'MSFT': snapshot('MSFT', 20_000.0, cutoff),
            'SPY': snapshot('SPY', 50_000.0, cutoff),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )
    future = cutoff + timedelta(seconds=1)
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 30_000.0, future),
            'MSFT': snapshot('MSFT', 40_000.0, future),
            'SPY': snapshot('SPY', 60_000.0, future),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )

    context = service.build_side_neutral_research_context(
        symbol='AAPL',
        as_of=cutoff,
    )

    assert context.latest_symbol_timestamp == NOW
    assert context.symbol_session_return_percent == 0.0
    assert context.benchmark.session_return_percent == 0.0
    assert context.breadth.median_session_return_percent == 0.0
    assert not hasattr(context, 'alignment')


def test_side_neutral_context_requires_receive_time_strictly_before_cutoff():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPY',)},
    )
    decisions = {'AAPL': decision(), 'MSFT': decision()}
    context_assets = {'SPY': AssetClass.EQUITY_US}
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 100.0),
            'MSFT': snapshot('MSFT', 200.0),
            'SPY': snapshot('SPY', 500.0),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )
    cutoff = NOW + timedelta(minutes=1)
    late_receive = MarketSnapshot(
        symbol='AAPL',
        bid=1000.0,
        ask=1000.2,
        last=1000.1,
        timestamp=NOW + timedelta(seconds=30),
        received_at=cutoff,
    )
    service.update(
        snapshots={'AAPL': late_receive},
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )

    context = service.build_side_neutral_research_context(
        symbol='AAPL',
        as_of=cutoff,
    )

    assert context.latest_symbol_timestamp == NOW
    assert context.symbol_session_return_percent == 0.0


def test_building_research_context_does_not_mutate_candidate_context():
    service = MarketContextService(
        instrument_registry=RegistryStub(),
        benchmark_symbols={AssetClass.EQUITY_US: ('SPY',)},
    )
    decisions = {'AAPL': decision(), 'MSFT': decision()}
    context_assets = {'SPY': AssetClass.EQUITY_US}
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 100.0),
            'MSFT': snapshot('MSFT', 200.0),
            'SPY': snapshot('SPY', 500.0),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )
    later = NOW + timedelta(minutes=1)
    service.update(
        snapshots={
            'AAPL': snapshot('AAPL', 101.0, later),
            'MSFT': snapshot('MSFT', 201.0, later),
            'SPY': snapshot('SPY', 502.0, later),
        },
        session_decisions=decisions,
        context_asset_classes=context_assets,
    )
    candidate_before = service.build_candidate_context(
        symbol='AAPL',
        side='BUY',
        as_of=later,
    )

    service.build_side_neutral_research_context(
        symbol='AAPL',
        as_of=later + timedelta(seconds=1),
    )
    candidate_after = service.build_candidate_context(
        symbol='AAPL',
        side='BUY',
        as_of=later,
    )

    assert candidate_after == candidate_before
