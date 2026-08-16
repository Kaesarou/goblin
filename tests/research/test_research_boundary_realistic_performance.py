from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace

from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.journal.jsonl_journal import JsonlJournal
from app.market.market_context import MarketContextService
from app.market.models import Candle, MarketSnapshot
from app.market.session_timeframe_service import FullSessionMultiTimeframeService
from app.research.pipeline import SideNeutralResearchPipeline

BOUNDARY = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
UNIVERSE_SIZE = 110
CATASTROPHIC_REAL_BOUNDARY_LIMIT_MS = 5_000


class NoOpPayloadObserver:
    failure_count = 0

    def flush(self, *, force):
        return force


def _symbols():
    eu = [f'EU{index:03d}' for index in range(UNIVERSE_SIZE // 2)]
    us = [f'US{index:03d}' for index in range(UNIVERSE_SIZE // 2)]
    return eu, us, [*eu, *us]


def _decision(asset_class: AssetClass, session_start: datetime):
    return SimpleNamespace(
        asset_class=asset_class,
        session_active=True,
        collect_snapshots=True,
        new_entries_allowed=True,
        session_key=f'{asset_class.value}:performance',
        session_start_time=session_start,
        session_end_time=BOUNDARY + timedelta(hours=3),
        session_24_7=False,
    )


def _price(index: int, minute: int) -> float:
    drift = 0.0015 * minute
    cross_section = index * 0.015
    oscillation = ((minute + index) % 7 - 3) * 0.0007
    return 100.0 + cross_section + drift + oscillation


def test_real_context_and_mtf_boundary_has_bounded_latency(tmp_path):
    eu, us, symbols = _symbols()
    settings = Settings(
        EQUITY_EU_SYMBOLS=','.join(eu),
        EQUITY_US_SYMBOLS=','.join(us),
        WATCHLIST=','.join(symbols),
        MARKET_BENCHMARK_EQUITY_EU=eu[0],
        MARKET_BENCHMARK_EQUITY_US=us[0],
    )
    registry = InstrumentRegistry(settings)
    asset_class_by_symbol = {
        symbol: registry.resolve(symbol).asset_class for symbol in symbols
    }
    market_context = MarketContextService(
        instrument_registry=registry,
        benchmark_symbols={
            AssetClass.EQUITY_EU: (eu[0],),
            AssetClass.EQUITY_US: (us[0],),
        },
        sector_by_symbol={symbol: 'SYNTHETIC' for symbol in symbols},
    )
    multi_timeframe = FullSessionMultiTimeframeService(
        {
            symbol: registry.config_for(symbol).multi_timeframe
            for symbol in symbols
        }
    )
    session_start = BOUNDARY - timedelta(minutes=65)
    decisions = {
        symbol: _decision(asset_class_by_symbol[symbol], session_start)
        for symbol in symbols
    }

    latest_snapshots = {}
    for minute in range(1, 66):
        closed_at = session_start + timedelta(minutes=minute)
        observed_at = closed_at - timedelta(seconds=1)
        snapshots = {}
        for index, symbol in enumerate(symbols):
            price = _price(index, minute)
            snapshot = MarketSnapshot(
                symbol=symbol,
                bid=price - 0.01,
                ask=price + 0.01,
                last=price,
                timestamp=observed_at,
                received_at=observed_at,
            )
            snapshots[symbol] = snapshot
            latest_snapshots[symbol] = snapshot
            multi_timeframe.on_base_candle(
                symbol=symbol,
                candle=Candle(
                    symbol=symbol,
                    timeframe_seconds=60,
                    open=price - 0.005,
                    high=price + 0.02,
                    low=price - 0.02,
                    close=price,
                    volume=None,
                    opened_at=closed_at - timedelta(minutes=1),
                    closed_at=closed_at,
                    sample_count=6,
                ),
                session_decision=decisions[symbol],
            )
        market_context.update(
            snapshots=snapshots,
            session_decisions=decisions,
        )

    journal = JsonlJournal(
        str(tmp_path / 'data' / 'logs' / 'runs' / 'run-real' / 'research.jsonl.gz'),
        run_id='run-real',
        stream_name='research',
        compact=True,
    )
    pipeline = SideNeutralResearchPipeline(
        research_symbols=asset_class_by_symbol,
        asset_class_by_symbol=asset_class_by_symbol,
        journal=journal,
        payload_schema_observer=NoOpPayloadObserver(),
        market_context_service=market_context,
        multi_timeframe_service=multi_timeframe,
        collection_started_at=session_start,
        run_id='run-real',
        summary_path=str(tmp_path / 'research_summary.json'),
    )
    for snapshot in latest_snapshots.values():
        pipeline.observe_accepted_snapshot(snapshot, source='websocket')

    started = perf_counter()
    result = pipeline.emit_boundary(
        symbols=symbols,
        state_at=BOUNDARY,
        session_decisions=decisions,
    )
    wall_ms = (perf_counter() - started) * 1000

    print(
        'RESEARCH_REAL_CONTEXT_BOUNDARY_BENCHMARK='
        f'{{"universe_size":{UNIVERSE_SIZE},'
        f'"wall_ms":{wall_ms:.3f},'
        f'"calculation_ms":{result.calculation_duration_ms:.3f},'
        f'"persistence_ms":{result.persistence_duration_ms:.3f}}}'
    )
    assert result.expected_state_count == UNIVERSE_SIZE
    assert result.emitted_state_count == UNIVERSE_SIZE
    assert result.state_calculation_failure_count == 0
    assert journal.open_count == 1
    assert wall_ms < CATASTROPHIC_REAL_BOUNDARY_LIMIT_MS
