from datetime import UTC, datetime, timedelta

from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.market.market_context import MarketContextService
from app.market.models import Candle, MarketSnapshot
from app.market.session_timeframe_service import FullSessionMultiTimeframeService
from app.research.pipeline import SideNeutralResearchPipeline
from app.runtime.trading_session_window import TradingSessionDecision
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


class RecordingJournal:
    def __init__(self) -> None:
        self.events = []
        self.open_count = 0

    def write(self, event_type, payload):
        self.events.append((event_type, payload))
        return True

    def write_many(self, events):
        batch = list(events)
        self.open_count += 1
        self.events.extend(batch)
        return len(batch)


class NoOpPayloadObserver:
    failure_count = 0

    def flush(self, *, force):
        return True


def test_production_context_and_mtf_services_build_a_causal_flat_state():
    settings = Settings(
        WATCHLIST='AAPL',
        EQUITY_US_SYMBOLS='AAPL',
    )
    profile = BalancedStrategyConfig()
    registry = InstrumentRegistry(
        settings,
        instrument_configs=profile.instrument_configs,
    )
    market_context = MarketContextService(
        instrument_registry=registry,
        benchmark_symbols={},
    )
    multi_timeframe = FullSessionMultiTimeframeService(
        {'AAPL': registry.config_for('AAPL').multi_timeframe}
    )
    journal = RecordingJournal()
    pipeline = SideNeutralResearchPipeline(
        research_symbols={'AAPL': AssetClass.EQUITY_US},
        asset_class_by_symbol={'AAPL': AssetClass.EQUITY_US},
        journal=journal,
        payload_schema_observer=NoOpPayloadObserver(),
        market_context_service=market_context,
        multi_timeframe_service=multi_timeframe,
    )
    session_start = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    session_end = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
    decision = TradingSessionDecision(
        asset_class=AssetClass.EQUITY_US,
        session_active=True,
        session_24_7=False,
        collect_snapshots=True,
        new_entries_allowed=True,
        force_close_required=False,
        reason='session_tradable',
        session_start_time=session_start,
        session_end_time=session_end,
        time_until_session_end_minutes=325.0,
        session_key='EQUITY_US:integration',
    )
    latest_candle = None
    for minute in range(65):
        closed_at = session_start + timedelta(minutes=minute + 1)
        price = 100.0 + minute * 0.01
        snapshot = MarketSnapshot(
            symbol='AAPL',
            bid=price - 0.01,
            ask=price + 0.01,
            last=price,
            timestamp=closed_at - timedelta(seconds=1),
            received_at=closed_at - timedelta(milliseconds=500),
        )
        pipeline.observe_accepted_snapshot(snapshot)
        market_context.observe_accepted_snapshot(snapshot)
        market_context.update(
            snapshots={'AAPL': snapshot},
            session_decisions={'AAPL': decision},
            context_asset_classes={},
        )
        latest_candle = Candle(
            symbol='AAPL',
            timeframe_seconds=60,
            open=price - 0.005,
            high=price + 0.02,
            low=price - 0.02,
            close=price,
            volume=None,
            opened_at=closed_at - timedelta(minutes=1),
            closed_at=closed_at,
            sample_count=10,
        )
        multi_timeframe.on_base_candle(
            symbol='AAPL',
            candle=latest_candle,
            session_decision=decision,
        )

    state_at = session_start + timedelta(minutes=65)
    result = pipeline.emit_boundary(
        symbols=['AAPL'],
        state_at=state_at,
        session_decisions={'AAPL': decision},
    )
    assert result.emitted_state_count == 1
    record = journal.events[0][1]

    assert record['state_at'] == state_at
    assert record['feature_cutoff_at'] == state_at
    assert record['latest_market_timestamp'] == state_at - timedelta(seconds=1)
    assert record['latest_market_received_at'] == state_at - timedelta(
        milliseconds=500
    )
    assert record['latest_closed_candle_timestamp'] == state_at
    assert record['boundary_candle_available'] is True
    assert record['context_latest_symbol_timestamp'] < state_at
    assert record['candle_coverage_60m_ratio'] == 1.0
    assert record['return_60m_percent'] is not None
    assert record['quote_available'] is True
    assert all(
        not isinstance(value, (dict, list, tuple, set))
        for value in record.values()
    )
