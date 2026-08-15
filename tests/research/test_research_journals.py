import gzip
import json
from datetime import UTC, datetime, timedelta

from app.journal.jsonl_journal import JsonlJournal
from app.journal.run_paths import build_run_journal_paths
from app.market.models import MarketSnapshot
from app.research.microstructure import MICROSTRUCTURE_CONTRACT_VERSION
from app.research.pipeline import (
    RESEARCH_FEATURE_NAMES,
    RESEARCH_FEATURE_SET_SHA256,
)
from app.research.research_state import RESEARCH_CAUSAL_CUTOFF_CONVENTION


def _read_gzip_jsonl(path):
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_research_journal_is_valid_gzip_with_a_flat_pandas_ready_record(
    tmp_path,
):
    paths = build_run_journal_paths(
        journal_path=str(tmp_path / 'data' / 'logs' / 'trades.jsonl'),
        run_id='run-test',
    )
    journal = JsonlJournal(
        str(paths.research),
        run_id='run-test',
        stream_name='research',
        compact=True,
    )
    payload = {
        'research_state_id': 'rs1_example',
        'state_at': datetime(2026, 8, 17, 14, 0, tzinfo=UTC),
        'symbol': 'AAPL',
        'asset_class': 'EQUITY_US',
        **{name: None for name in RESEARCH_FEATURE_NAMES},
    }

    assert journal.write('research_state', payload)
    records = _read_gzip_jsonl(paths.research)

    assert len(records) == 1
    assert records[0]['stream'] == 'research'
    assert records[0]['event_type'] == 'research_state'
    assert records[0]['payload']['research_state_id'] == 'rs1_example'
    assert all(
        not isinstance(value, (dict, list))
        for value in records[0]['payload'].values()
    )
    logs_root = tmp_path / 'data' / 'logs'
    assert logs_root in paths.research.parents
    with gzip.open(paths.research, 'rt', encoding='utf-8') as handle:
        assert handle.readline().startswith(
            '{"schema_version":1,"run_id":"run-test"'
        )


def test_legacy_jsonl_envelope_and_payload_are_not_changed(tmp_path):
    path = tmp_path / 'data' / 'logs' / 'runs' / 'run-test' / 'market.jsonl.gz'
    journal = JsonlJournal(
        str(path),
        run_id='run-test',
        stream_name='market',
    )
    snapshot = MarketSnapshot(
        symbol='AAPL',
        bid=99.9,
        ask=100.1,
        last=100.0,
        timestamp=datetime(2026, 8, 17, 14, 0, tzinfo=UTC),
    )
    payload = {
        'symbol': 'AAPL',
        'snapshot': snapshot,
        'source': 'websocket',
        'message_id': 'm-1',
        'connection_id': 'c-1',
        'loop_id': 1,
    }

    assert journal.write('market_price_changed', payload)
    record = _read_gzip_jsonl(path)[0]

    assert set(record) == {
        'schema_version',
        'run_id',
        'stream',
        'sequence',
        'timestamp',
        'event_type',
        'payload',
    }
    assert set(record['payload']) == set(payload)
    assert 'research_state_id' not in record['payload']
    assert 'payload_schema' not in record['payload']


def test_reproducible_compressed_log_budget_is_below_ten_percent(tmp_path):
    paths = build_run_journal_paths(
        journal_path=str(tmp_path / 'data' / 'logs' / 'trades.jsonl'),
        run_id='run-budget',
    )
    market = JsonlJournal(
        str(paths.market),
        run_id='run-budget',
        stream_name='market',
    )
    research = JsonlJournal(
        str(paths.research),
        run_id='run-budget',
        stream_name='research',
        compact=True,
    )
    started_at = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)

    # One accepted price-changing quote per second is a conservative normal
    # one-hour slice; research persists only one state every five minutes.
    for second in range(3600):
        observed_at = started_at + timedelta(seconds=second)
        price = 100.0 + (second % 997) / 10_000
        market.write(
            'market_price_changed',
            {
                'symbol': 'AAPL',
                'snapshot': MarketSnapshot(
                    symbol='AAPL',
                    bid=price - 0.01,
                    ask=price + 0.01,
                    last=price,
                    timestamp=observed_at,
                    received_at=observed_at,
                ),
                'source': 'websocket',
                'message_id': f'm-{second}',
                'connection_id': 'c-1',
                'loop_id': second,
            },
        )
        if second and second % 300 == 0:
            research.write(
                'research_state',
                {
                    'schema_version': 1,
                    'research_contract_version': (
                        'side_neutral_market_research_state_v1'
                    ),
                    'microstructure_contract_version': (
                        MICROSTRUCTURE_CONTRACT_VERSION
                    ),
                    'research_feature_set_sha256': (
                        RESEARCH_FEATURE_SET_SHA256
                    ),
                    'research_state_id': f'rs1_{second:024d}',
                    'state_at': observed_at,
                    'feature_cutoff_at': observed_at,
                    'causal_cutoff_convention': (
                        RESEARCH_CAUSAL_CUTOFF_CONVENTION
                    ),
                    'latest_market_timestamp': observed_at
                    - timedelta(seconds=1),
                    'latest_market_received_at': observed_at
                    - timedelta(milliseconds=500),
                    'latest_closed_candle_timestamp': observed_at,
                    'symbol': 'AAPL',
                    'asset_class': 'EQUITY_US',
                    'session_key': 'EQUITY_US:test',
                    'session_start_time': started_at,
                    'session_end_time': started_at + timedelta(hours=6, minutes=30),
                    'session_minute': second // 60,
                    'time_until_session_end_minutes': 390 - second / 60,
                    'session_progress_ratio': second / (390 * 60),
                    'weekday_utc': observed_at.weekday(),
                    'minute_of_day_utc': observed_at.hour * 60 + observed_at.minute,
                    'bid': price - 0.01,
                    'ask': price + 0.01,
                    'last': price,
                    'mid': price,
                    'observed_spread': 0.02,
                    'observed_spread_percent': 0.02,
                    'quote_available': True,
                    'last_is_broker_execution': True,
                    'quote_freshness_seconds': 1.0,
                    'quote_receive_freshness_seconds': 0.5,
                    'latest_candle_sample_count': 60,
                    'latest_candle_carried_forward': False,
                    'latest_candle_quality_degraded': False,
                    'latest_candle_source_price_age_seconds': 0.5,
                    'feature_expected_count': len(RESEARCH_FEATURE_NAMES),
                    'feature_available_count': len(RESEARCH_FEATURE_NAMES),
                    'feature_completeness_ratio': 1.0,
                    'market_context_available': True,
                    'micro_10s_available': True,
                    'micro_30s_available': True,
                    'micro_60s_available': True,
                    **{
                        name: round((index + second) / 10_000, 8)
                        for index, name in enumerate(RESEARCH_FEATURE_NAMES)
                    },
                },
            )

    market_size = paths.market.stat().st_size
    research_size = paths.research.stat().st_size
    ratio = research_size / market_size

    assert len(_read_gzip_jsonl(paths.research)) == 11
    assert market_size > 0
    assert research_size > 0
    assert ratio < 0.10, (
        f'research={research_size} bytes, market={market_size} bytes, '
        f'ratio={ratio:.4%}'
    )
