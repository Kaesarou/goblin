import json
from datetime import UTC, datetime, timedelta
from statistics import median
from time import perf_counter
from types import SimpleNamespace

from app.instruments.models import AssetClass
from app.journal.jsonl_journal import JsonlJournal
from app.market.market_context import MarketRegime
from app.market.models import MarketSnapshot
from app.research.pipeline import SideNeutralResearchPipeline

FIRST_BOUNDARY = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)
UNIVERSE_SIZE = 110
BOUNDARY_RUNS = 5
CATASTROPHIC_BOUNDARY_LIMIT_MS = 5_000


class EmptyMultiTimeframeService:
    def bars(self, _symbol, _timeframe, *, as_of, complete_only):
        assert as_of.tzinfo is not None
        assert complete_only is True
        return []


class ConstantMarketContextService:
    def build_side_neutral_research_context(self, *, symbol, as_of):
        return SimpleNamespace(
            regime=MarketRegime.MIXED,
            latest_symbol_timestamp=None,
            symbol_session_return_percent=None,
            symbol_relative_strength_percent=None,
            benchmark=SimpleNamespace(
                symbol='SPX500',
                available=False,
                session_return_percent=None,
                momentum_percent=None,
                spread_percent=None,
                snapshot_age_seconds=None,
            ),
            breadth=SimpleNamespace(
                available=False,
                valid_symbols=0,
                coverage_ratio=0.0,
                advancing_ratio=None,
                median_session_return_percent=None,
            ),
            sector=SimpleNamespace(
                sector=None,
                available=False,
                valid_member_count=0,
                advancing_ratio=None,
                median_session_return_percent=None,
            ),
            spread=SimpleNamespace(
                available=False,
                relative_to_median=None,
                reference_percentile=None,
                recent_change_ratio=None,
            ),
        )


class NoOpPayloadObserver:
    failure_count = 0

    def flush(self, *, force):
        return force


def _universe():
    symbols = [f'SYMBOL{index:03d}' for index in range(UNIVERSE_SIZE)]
    research_symbols = {
        symbol: (
            AssetClass.EQUITY_EU
            if index < UNIVERSE_SIZE // 2
            else AssetClass.EQUITY_US
        )
        for index, symbol in enumerate(symbols)
    }
    decisions = {
        symbol: SimpleNamespace(
            session_active=True,
            collect_snapshots=True,
            new_entries_allowed=True,
            session_key=f'{asset_class.value}:benchmark',
            session_start_time=FIRST_BOUNDARY - timedelta(hours=1),
            session_end_time=FIRST_BOUNDARY + timedelta(hours=8),
        )
        for symbol, asset_class in research_symbols.items()
    }
    return symbols, research_symbols, decisions


def test_production_shaped_boundary_reports_timing_and_single_open(tmp_path):
    symbols, research_symbols, decisions = _universe()
    journal = JsonlJournal(
        str(tmp_path / 'data' / 'logs' / 'runs' / 'run-benchmark' / 'research.jsonl.gz'),
        run_id='run-benchmark',
        stream_name='research',
        compact=True,
    )
    pipeline = SideNeutralResearchPipeline(
        research_symbols=research_symbols,
        asset_class_by_symbol=research_symbols,
        journal=journal,
        payload_schema_observer=NoOpPayloadObserver(),
        market_context_service=ConstantMarketContextService(),
        multi_timeframe_service=EmptyMultiTimeframeService(),
        collection_started_at=FIRST_BOUNDARY - timedelta(seconds=1),
        run_id='run-benchmark',
        summary_path=str(tmp_path / 'research_summary.json'),
    )
    for index, symbol in enumerate(symbols):
        observed_at = FIRST_BOUNDARY - timedelta(seconds=1)
        price = 100.0 + index / 100
        pipeline.observe_accepted_snapshot(
            MarketSnapshot(
                symbol=symbol,
                bid=price - 0.01,
                ask=price + 0.01,
                last=price,
                timestamp=observed_at,
                received_at=observed_at,
            ),
            source='websocket',
        )
    assert journal.open_count == 0

    results = []
    wall_durations = []
    for index in range(BOUNDARY_RUNS):
        started = perf_counter()
        results.append(
            pipeline.emit_boundary(
                symbols=symbols,
                state_at=FIRST_BOUNDARY + timedelta(minutes=5 * index),
                session_decisions=decisions,
            )
        )
        wall_durations.append((perf_counter() - started) * 1000)
    durations = [result.total_duration_ms for result in results]
    calculation = [result.calculation_duration_ms for result in results]
    persistence = [result.persistence_duration_ms for result in results]
    benchmark = {
        'universe_size': UNIVERSE_SIZE,
        'boundary_runs': BOUNDARY_RUNS,
        'boundary_wall_total_median_ms': round(median(wall_durations), 3),
        'boundary_wall_total_max_ms': round(max(wall_durations), 3),
        'boundary_total_median_ms': median(durations),
        'boundary_total_max_ms': max(durations),
        'calculation_median_ms': median(calculation),
        'calculation_max_ms': max(calculation),
        'persistence_median_ms': median(persistence),
        'persistence_max_ms': max(persistence),
        'research_journal_open_count': journal.open_count,
    }
    print('RESEARCH_BOUNDARY_BENCHMARK=' + json.dumps(benchmark, sort_keys=True))

    assert all(result.expected_state_count == UNIVERSE_SIZE for result in results)
    assert all(result.emitted_state_count == UNIVERSE_SIZE for result in results)
    assert journal.written_count == UNIVERSE_SIZE * BOUNDARY_RUNS
    assert journal.open_count == BOUNDARY_RUNS
    assert max(wall_durations) < CATASTROPHIC_BOUNDARY_LIMIT_MS


def test_batch_persistence_reports_before_after_open_count(tmp_path):
    events = [
        (
            'research_state',
            {
                'research_state_id': f'rs1_{index:024d}',
                'symbol': f'SYMBOL{index:03d}',
                'state_at': FIRST_BOUNDARY,
                'features': [index / 1000 for _ in range(72)],
            },
        )
        for index in range(UNIVERSE_SIZE)
    ]
    legacy_durations = []
    batch_durations = []
    legacy_opens = 0
    batch_opens = 0
    for run in range(BOUNDARY_RUNS):
        legacy = JsonlJournal(
            str(tmp_path / f'legacy-{run}.jsonl.gz'),
            run_id='run-benchmark',
            compact=True,
        )
        started = perf_counter()
        for event_type, payload in events:
            assert legacy.write(event_type, payload)
        legacy_durations.append((perf_counter() - started) * 1000)
        legacy_opens += legacy.open_count

        batch = JsonlJournal(
            str(tmp_path / f'batch-{run}.jsonl.gz'),
            run_id='run-benchmark',
            compact=True,
        )
        started = perf_counter()
        assert batch.write_many(events) == UNIVERSE_SIZE
        batch_durations.append((perf_counter() - started) * 1000)
        batch_opens += batch.open_count

    benchmark = {
        'universe_size': UNIVERSE_SIZE,
        'runs': BOUNDARY_RUNS,
        'legacy_median_ms': round(median(legacy_durations), 3),
        'legacy_max_ms': round(max(legacy_durations), 3),
        'batch_median_ms': round(median(batch_durations), 3),
        'batch_max_ms': round(max(batch_durations), 3),
        'legacy_open_count': legacy_opens,
        'batch_open_count': batch_opens,
    }
    print('RESEARCH_PERSISTENCE_BENCHMARK=' + json.dumps(benchmark, sort_keys=True))

    assert legacy_opens == UNIVERSE_SIZE * BOUNDARY_RUNS
    assert batch_opens == BOUNDARY_RUNS
