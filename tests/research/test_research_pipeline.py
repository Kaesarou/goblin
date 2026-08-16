import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.instruments.models import AssetClass
from app.market.market_context import MarketRegime
from app.market.models import Candle, MarketSnapshot
from app.market.multi_timeframe import TimeframeBar
from app.market.timeframes import BarCompleteness, Timeframe
from app.market_data.models import MarketDataSource
from app.research.pipeline import SideNeutralResearchPipeline
from app.research.summary import empty_research_summary, write_research_summary

STATE_AT = datetime(2026, 8, 17, 14, 30, tzinfo=UTC)


class RecordingJournal:
    def __init__(self, *, result=True, raises=False) -> None:
        self.events = []
        self.result = result
        self.raises = raises
        self.open_count = 0

    def write(self, event_type, payload):
        if self.raises:
            raise OSError('research disk unavailable')
        self.events.append((event_type, payload))
        return self.result

    def write_many(self, events):
        batch = list(events)
        self.open_count += 1
        if self.raises:
            raise OSError('research disk unavailable')
        if not self.result:
            return 0
        self.events.extend(batch)
        return len(batch)


class StubPayloadObserver:
    def __init__(self) -> None:
        self.samples = []
        self.flush_count = 0
        self.failure_count = 0

    def observe(self, sample, *, asset_class):
        self.samples.append((sample, asset_class))

    def flush(self, *, force):
        self.flush_count += 1
        return True

    def observe_payload(self, **kwargs):
        self.samples.append(kwargs)


class StubMultiTimeframeService:
    def __init__(self, bars_by_symbol) -> None:
        self.bars_by_symbol = bars_by_symbol

    def bars(self, symbol, timeframe, *, as_of, complete_only):
        assert timeframe == Timeframe.M1
        assert complete_only is True
        return list(self.bars_by_symbol[symbol])


class StubMarketContextService:
    def build_side_neutral_research_context(self, *, symbol, as_of):
        return SimpleNamespace(
            regime=MarketRegime.MIXED,
            latest_symbol_timestamp=as_of - timedelta(seconds=1),
            symbol_session_return_percent=0.4,
            symbol_relative_strength_percent=0.1,
            benchmark=SimpleNamespace(
                symbol='SPX500',
                available=True,
                session_return_percent=0.3,
                momentum_percent=0.2,
                spread_percent=0.01,
                snapshot_age_seconds=1.0,
            ),
            breadth=SimpleNamespace(
                available=True,
                valid_symbols=2,
                coverage_ratio=1.0,
                advancing_ratio=0.5,
                median_session_return_percent=0.1,
            ),
            sector=SimpleNamespace(
                sector='TECHNOLOGY',
                available=True,
                valid_member_count=2,
                advancing_ratio=0.5,
                median_session_return_percent=0.2,
            ),
            spread=SimpleNamespace(
                available=True,
                relative_to_median=1.1,
                reference_percentile=0.6,
                recent_change_ratio=0.05,
            ),
        )


def _candle(symbol: str, state_at: datetime = STATE_AT) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe_seconds=60,
        open=99.9,
        high=100.2,
        low=99.8,
        close=100.1,
        volume=None,
        opened_at=state_at - timedelta(minutes=1),
        closed_at=state_at,
        sample_count=3,
    )


def _bar(symbol: str, session_key: str, state_at: datetime = STATE_AT):
    return TimeframeBar(
        candle=_candle(symbol, state_at),
        timeframe=Timeframe.M1,
        session_key=session_key,
        completeness=BarCompleteness.COMPLETE,
        source_bar_count=1,
        expected_source_bar_count=1,
        missing_source_bar_count=0,
    )


def _decision(
    asset_class: AssetClass,
    *,
    remaining: float = 120.0,
    new_entries_allowed: bool = True,
):
    start = STATE_AT - timedelta(minutes=60)
    end = STATE_AT + timedelta(minutes=remaining)
    return SimpleNamespace(
        asset_class=asset_class,
        session_active=True,
        collect_snapshots=True,
        new_entries_allowed=new_entries_allowed,
        session_key=f'{asset_class.value}:session',
        session_start_time=start,
        session_end_time=end,
        time_until_session_end_minutes=remaining,
        max_open_positions=0,
        position_already_open=True,
        cooldown_active=True,
        max_trades_reached=True,
        portfolio_capacity=0,
    )


def _pipeline(*, journal=None, summary_path=None):
    decisions = {
        'AIR.PA': _decision(AssetClass.EQUITY_EU),
        'AAPL': _decision(AssetClass.EQUITY_US),
    }
    bars = {
        symbol: [_bar(symbol, decision.session_key)]
        for symbol, decision in decisions.items()
    }
    return (
        SideNeutralResearchPipeline(
            research_symbols={
                'AIR.PA': AssetClass.EQUITY_EU,
                'AAPL': AssetClass.EQUITY_US,
                'BTC': AssetClass.CRYPTO,
            },
            asset_class_by_symbol={
                'AIR.PA': AssetClass.EQUITY_EU,
                'AAPL': AssetClass.EQUITY_US,
                'SPX500': AssetClass.EQUITY_US,
            },
            journal=journal or RecordingJournal(),
            payload_schema_observer=StubPayloadObserver(),
            market_context_service=StubMarketContextService(),
            multi_timeframe_service=StubMultiTimeframeService(bars),
            run_id='run-test',
            summary_path=(
                None if summary_path is None else str(summary_path)
            ),
        ),
        decisions,
    )


def _observe_quote(
    pipeline,
    symbol,
    *,
    seconds_before=1.0,
    market_seconds_before=None,
):
    observed_at = STATE_AT - timedelta(seconds=seconds_before)
    market_at = STATE_AT - timedelta(
        seconds=(
            seconds_before
            if market_seconds_before is None
            else market_seconds_before
        )
    )
    pipeline.observe_accepted_snapshot(
        MarketSnapshot(
            symbol=symbol,
            bid=99.9,
            ask=100.1,
            last=100.0,
            timestamp=market_at,
            received_at=observed_at,
        )
    )


def _emit_one(pipeline, *, symbol, state_at, session_decision) -> bool:
    result = pipeline.emit_boundary(
        symbols=[symbol],
        state_at=state_at,
        session_decisions={symbol: session_decision},
    )
    return result.emitted_state_count == 1


def test_eu_and_us_emit_without_candidate_or_portfolio_capacity():
    pipeline, decisions = _pipeline()
    for symbol in ('AIR.PA', 'AAPL'):
        _observe_quote(pipeline, symbol)
    result = pipeline.emit_boundary(
        symbols=['AIR.PA', 'AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )

    assert result.emitted_state_count == 2
    records = [payload for _, payload in pipeline.journal.events]
    assert {record['symbol'] for record in records} == {'AIR.PA', 'AAPL'}
    assert {record['asset_class'] for record in records} == {
        'EQUITY_EU',
        'EQUITY_US',
    }
    assert all('side' not in record for record in records)
    assert all('candidate' not in record for record in records)
    assert all(record['state_at'] == STATE_AT for record in records)
    assert all(len(record['research_feature_set_sha256']) == 64 for record in records)
    assert all(record['micro_60s_quote_count'] == 1 for record in records)
    assert all(record['boundary_candle_available'] for record in records)


def test_cadence_and_last_hour_boundaries_are_exact():
    for minute, remaining, allowed, expected in (
        (31, 120.0, True, False),
        (30, 60.0, False, False),
        (30, 60.0001, True, True),
        (30, 120.0, False, True),
    ):
        pipeline, _ = _pipeline()
        state_at = STATE_AT.replace(minute=minute)
        decision = _decision(
            AssetClass.EQUITY_US,
            remaining=remaining,
            new_entries_allowed=allowed,
        )
        decision.session_start_time = state_at - timedelta(minutes=60)
        decision.session_end_time = state_at + timedelta(minutes=remaining)
        pipeline.multi_timeframe_service.bars_by_symbol['AAPL'] = [
            _bar('AAPL', decision.session_key, state_at)
        ]
        observed_at = state_at - timedelta(seconds=1)
        pipeline.observe_accepted_snapshot(
            MarketSnapshot(
                symbol='AAPL',
                bid=99.9,
                ask=100.1,
                last=100.0,
                timestamp=observed_at,
                received_at=observed_at,
            )
        )
        assert _emit_one(
            pipeline,
            symbol='AAPL',
            state_at=state_at,
            session_decision=decision,
        ) is expected


def test_cutoff_recomputes_remaining_time_instead_of_using_stale_decision():
    pipeline, _ = _pipeline()
    decision = _decision(AssetClass.EQUITY_US, remaining=120.0)
    decision.session_end_time = STATE_AT + timedelta(minutes=60)
    _observe_quote(pipeline, 'AAPL')
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decision,
    ) is False


def test_state_at_or_before_collection_start_is_not_backfilled():
    pipeline, decisions = _pipeline()
    pipeline.collection_started_at = STATE_AT
    _observe_quote(pipeline, 'AAPL')
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    ) is False


def test_quote_cutoff_rejects_future_market_or_receive_timestamp():
    pipeline, decisions = _pipeline()
    _observe_quote(pipeline, 'AAPL', seconds_before=3)
    _observe_quote(pipeline, 'AAPL', seconds_before=2, market_seconds_before=0)
    pipeline.observe_accepted_snapshot(
        MarketSnapshot(
            symbol='AAPL',
            bid=100.2,
            ask=100.4,
            last=100.3,
            timestamp=STATE_AT - timedelta(seconds=1),
            received_at=STATE_AT,
        )
    )
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    )
    record = pipeline.journal.events[0][1]
    assert record['latest_market_timestamp'] == STATE_AT - timedelta(seconds=3)
    assert record['latest_market_received_at'] == STATE_AT - timedelta(seconds=3)
    assert record['micro_10s_quote_count'] == 1


def test_missing_causal_quote_still_records_state_and_availability():
    pipeline, decisions = _pipeline()
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    )
    record = pipeline.journal.events[0][1]
    assert record['quote_available'] is False
    assert record['bid'] is None
    assert record['ask'] is None
    assert record['last'] is None
    assert record['latest_market_timestamp'] is None
    assert record['quote_freshness_seconds'] is None
    assert record['micro_60s_quote_count'] == 0


def test_state_is_emitted_when_the_boundary_candle_is_missing():
    pipeline, decisions = _pipeline()
    pipeline.multi_timeframe_service.bars_by_symbol['AAPL'] = []
    _observe_quote(pipeline, 'AAPL')
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    )
    record = pipeline.journal.events[0][1]
    assert record['latest_candle_available'] is False
    assert record['boundary_candle_available'] is False
    assert record['latest_closed_candle_timestamp'] is None
    assert record['latest_candle_sample_count'] is None
    assert record['candle_coverage_60m_ratio'] == 0.0


def test_stale_latest_candle_is_distinct_from_boundary_candle_availability():
    pipeline, decisions = _pipeline()
    session_key = decisions['AAPL'].session_key
    pipeline.multi_timeframe_service.bars_by_symbol['AAPL'] = [
        _bar('AAPL', session_key, STATE_AT - timedelta(minutes=1))
    ]
    _observe_quote(pipeline, 'AAPL')
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    )
    record = pipeline.journal.events[0][1]
    assert record['latest_candle_available'] is True
    assert record['boundary_candle_available'] is False
    assert record['latest_closed_candle_timestamp'] == STATE_AT - timedelta(minutes=1)


def test_rest_fallback_quote_is_provenanced_but_excluded_from_ws_microstructure():
    pipeline, decisions = _pipeline()
    _observe_quote(pipeline, 'AAPL', seconds_before=3)
    fallback_at = STATE_AT - timedelta(seconds=1)
    pipeline.observe_accepted_snapshot(
        MarketSnapshot(
            symbol='AAPL',
            bid=100.9,
            ask=101.1,
            last=101.0,
            timestamp=fallback_at,
            received_at=fallback_at,
        ),
        source=MarketDataSource.REST_FALLBACK,
    )
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    )
    record = pipeline.journal.events[0][1]
    assert record['latest_market_source'] == 'rest_fallback'
    assert record['last'] == 101.0
    assert record['micro_10s_quote_count'] == 1


def test_research_journal_failure_is_contained_and_observable():
    pipeline, decisions = _pipeline(journal=RecordingJournal(raises=True))
    _observe_quote(pipeline, 'AAPL')
    assert _emit_one(
        pipeline,
        symbol='AAPL',
        state_at=STATE_AT,
        session_decision=decisions['AAPL'],
    ) is False
    assert pipeline.failure_count == 1


def test_boundary_batches_states_and_isolates_symbol_calculation_failure():
    pipeline, decisions = _pipeline()
    _observe_quote(pipeline, 'AIR.PA')
    _observe_quote(pipeline, 'AAPL')
    del pipeline.multi_timeframe_service.bars_by_symbol['AAPL']
    result = pipeline.emit_boundary(
        symbols=['AIR.PA', 'AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    assert result.expected_state_count == 2
    assert result.emitted_state_count == 1
    assert result.state_calculation_failure_count == 1
    assert result.journal_write_failure_count == 0
    assert pipeline.journal.open_count == 1
    assert [payload['symbol'] for _, payload in pipeline.journal.events] == ['AIR.PA']
    assert pipeline.expected_state_count == 2
    assert pipeline.emitted_state_count == 1
    assert pipeline.state_calculation_failure_count == 1


def test_boundary_batch_write_failure_is_isolated_and_health_visible():
    pipeline, decisions = _pipeline(journal=RecordingJournal(raises=True))
    _observe_quote(pipeline, 'AIR.PA')
    _observe_quote(pipeline, 'AAPL')
    result = pipeline.emit_boundary(
        symbols=['AIR.PA', 'AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    assert result.expected_state_count == 2
    assert result.emitted_state_count == 0
    assert result.journal_write_failure_count == 2
    assert pipeline.expected_state_count == 2
    assert pipeline.emitted_state_count == 0
    assert pipeline.journal_failure_count == 2


def test_duplicate_boundary_does_not_inflate_expected_state_count():
    pipeline, decisions = _pipeline()
    first = pipeline.emit_boundary(
        symbols=['AIR.PA', 'AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    duplicate = pipeline.emit_boundary(
        symbols=['AIR.PA', 'AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    assert first.expected_state_count == 2
    assert duplicate.expected_state_count == 0
    assert pipeline.expected_state_count == 2
    assert pipeline.emitted_state_count == 2
    assert pipeline.duplicate_prevented_count == 1


def test_run_scoped_health_summary_is_atomic_and_counts_missing_inputs(tmp_path):
    summary_path = tmp_path / 'data' / 'logs' / 'runs' / 'run-test' / 'research_summary.json'
    pipeline, decisions = _pipeline(summary_path=summary_path)
    pipeline.multi_timeframe_service.bars_by_symbol['AAPL'] = []
    result = pipeline.emit_boundary(
        symbols=['AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    pipeline.flush()
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert result.expected_state_count == 1
    assert result.emitted_state_count == 1
    assert summary['schema_version'] == 'research_summary_v1'
    assert summary['research_enabled'] is True
    assert summary['expected_state_count'] == 1
    assert summary['emitted_state_count'] == 1
    assert summary['states_missing_quote_count'] == 1
    assert summary['states_missing_candle_count'] == 1
    assert summary['states_missing_boundary_candle_count'] == 1
    assert summary['boundary_count'] == 1
    assert summary['research_journal_open_count'] == 1
    assert summary['last_boundary_duration_ms'] >= 0
    assert not summary_path.with_suffix('.json.tmp').exists()


def test_run_scoped_health_counts_stale_latest_candle_as_missing_boundary(tmp_path):
    summary_path = tmp_path / 'research_summary.json'
    pipeline, decisions = _pipeline(summary_path=summary_path)
    session_key = decisions['AAPL'].session_key
    pipeline.multi_timeframe_service.bars_by_symbol['AAPL'] = [
        _bar('AAPL', session_key, STATE_AT - timedelta(minutes=1))
    ]
    result = pipeline.emit_boundary(
        symbols=['AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert result.emitted_state_count == 1
    assert summary['states_missing_candle_count'] == 0
    assert summary['states_missing_boundary_candle_count'] == 1


def test_run_start_does_not_create_expected_states_for_earlier_boundary(tmp_path):
    summary_path = tmp_path / 'research_summary.json'
    pipeline, decisions = _pipeline(summary_path=summary_path)
    pipeline.collection_started_at = STATE_AT
    result = pipeline.emit_boundary(
        symbols=['AAPL'],
        state_at=STATE_AT,
        session_decisions=decisions,
    )
    pipeline.flush()
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert result.expected_state_count == 0
    assert summary['expected_state_count'] == 0
    assert summary['emitted_state_count'] == 0
    assert summary['boundary_count'] == 0


def test_disabled_run_has_bounded_research_summary_without_journal(tmp_path):
    summary_path = tmp_path / 'data' / 'logs' / 'runs' / 'run-test' / 'research_summary.json'
    research_journal_path = summary_path.with_name('research.jsonl.gz')
    write_research_summary(
        summary_path,
        empty_research_summary(
            run_id='run-test',
            enabled=False,
            updated_at=STATE_AT,
        ),
    )
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    assert summary['research_enabled'] is False
    assert summary['expected_state_count'] == 0
    assert summary['emitted_state_count'] == 0
    assert summary['states_missing_boundary_candle_count'] == 0
    assert not research_journal_path.exists()
