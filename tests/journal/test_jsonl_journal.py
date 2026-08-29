import gzip
import json
from datetime import datetime, timezone
from typing import NamedTuple

from app.instruments.models import AssetClass
from app.journal.jsonl_journal import JsonlJournal
from app.journal.serialization import serialize_value
from app.runtime.trading_session_window import TradingSessionDecision


class SessionDecisionStub(NamedTuple):
    session_key: str
    session_start_time: datetime
    session_end_time: datetime


def test_jsonl_journal_serializes_namedtuple_with_datetimes(tmp_path):
    journal_path = tmp_path / 'trades.jsonl'
    journal = JsonlJournal(str(journal_path))

    decision = SessionDecisionStub(
        session_key='EQUITY_US:2026-07-06T15:30:00+02:00:2026-07-06T22:00:00+02:00',
        session_start_time=datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc),
        session_end_time=datetime(2026, 7, 6, 22, 0, tzinfo=timezone.utc),
    )

    journal.write('session_started', {'session_decision': decision})

    record = json.loads(journal_path.read_text(encoding='utf-8'))

    assert record['event_type'] == 'session_started'
    assert record['payload']['session_decision'] == {
        'session_key': 'EQUITY_US:2026-07-06T15:30:00+02:00:2026-07-06T22:00:00+02:00',
        'session_start_time': '2026-07-06T15:30:00+00:00',
        'session_end_time': '2026-07-06T22:00:00+00:00',
    }


def test_trading_session_decision_with_asset_class_enum_is_json_serializable():
    decision = TradingSessionDecision(
        asset_class=AssetClass.EQUITY_EU,
        session_active=True,
        session_24_7=False,
        collect_snapshots=True,
        new_entries_allowed=True,
        force_close_required=False,
        reason='session_tradable',
        session_start_time=datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc),
        session_end_time=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
        time_until_session_end_minutes=120.0,
        session_key='EQUITY_EU:2026-07-10T09:00:00+00:00:2026-07-10T15:30:00+00:00',
    )

    serialized = serialize_value({'session_decision': decision})

    json.dumps(serialized)
    assert serialized['session_decision']['asset_class'] == 'EQUITY_EU'
    assert serialized['session_decision']['session_start_time'] == '2026-07-10T09:00:00+00:00'
    assert serialized['session_decision']['session_end_time'] == '2026-07-10T15:30:00+00:00'


def test_write_many_preserves_order_sequence_and_uses_one_gzip_open(tmp_path):
    journal_path = tmp_path / 'research.jsonl.gz'
    journal = JsonlJournal(
        str(journal_path),
        run_id='run-test',
        stream_name='research',
        compact=True,
    )

    written = journal.write_many(
        [
            ('research_state', {'symbol': 'AIR.PA'}),
            ('research_state', {'symbol': 'AAPL'}),
            ('research_state', {'symbol': 'MSFT'}),
        ]
    )

    with gzip.open(journal_path, 'rt', encoding='utf-8') as handle:
        records = [json.loads(line) for line in handle]
    assert written == 3
    assert [record['sequence'] for record in records] == [1, 2, 3]
    assert [record['payload']['symbol'] for record in records] == [
        'AIR.PA',
        'AAPL',
        'MSFT',
    ]
    assert journal.sequence == 3
    assert journal.written_count == 3
    assert journal.failed_count == 0
    assert journal.open_count == 1


def test_write_many_failure_is_all_or_nothing_and_counted(tmp_path, monkeypatch):
    journal = JsonlJournal(str(tmp_path / 'research.jsonl.gz'))

    def fail_open():
        raise OSError('disk unavailable')

    monkeypatch.setattr(journal, '_open_append', fail_open)

    assert journal.write_many([('one', {}), ('two', {})]) == 0
    assert journal.sequence == 0
    assert journal.written_count == 0
    assert journal.failed_count == 2


def test_legacy_write_behavior_remains_single_record(tmp_path):
    journal_path = tmp_path / 'legacy.jsonl'
    journal = JsonlJournal(str(journal_path), run_id='run-test')

    assert journal.write('legacy', {'value': 1}) is True

    record = json.loads(journal_path.read_text(encoding='utf-8'))
    assert record['sequence'] == 1
    assert record['event_type'] == 'legacy'
    assert record['payload'] == {'value': 1}
    assert journal.written_count == 1
    assert journal.failed_count == 0
    assert journal.open_count == 1


def test_trade_journal_soft_budget_suppresses_noise_and_marks_activation_once(tmp_path):
    journal_path = tmp_path / 'trades.jsonl.gz'
    journal = JsonlJournal(
        str(journal_path),
        run_id='run-test',
        stream_name='trades',
        soft_max_bytes=1,
        hard_max_bytes=100_000,
        min_free_bytes=0,
        critical_event_types={'v3_inventory_event'},
    )

    assert journal.write('v3_session_state', {'symbol': 'AAPL'}) is True
    assert journal.write('v3_session_state', {'symbol': 'MSFT'}) is True
    assert journal.write('v3_session_state', {'symbol': 'NVDA'}) is True
    assert journal.write(
        'v3_inventory_event',
        {'inventory_event_type': 'ENTRY_FILLED'},
    ) is True

    with gzip.open(journal_path, 'rt', encoding='utf-8') as handle:
        records = [json.loads(line) for line in handle]

    assert [record['event_type'] for record in records] == [
        'v3_session_state',
        'trade_journal_budget_active',
        'v3_inventory_event',
    ]
    marker = records[1]
    assert marker['payload']['reason'] == 'soft_max_bytes'
    assert marker['payload']['soft_max_bytes'] == 1
    assert journal.written_count == 3
    assert journal.suppressed_count == 2
    assert journal.budget_reason == 'soft_max_bytes'
    metrics = journal.budget_metrics()
    assert metrics['soft_cap_active'] is True
    assert metrics['hard_cap_active'] is False
    assert metrics['budget_event_emitted'] is True
    assert metrics['suppressed_count'] == 2


def test_trade_journal_hard_budget_suppresses_even_critical_events(tmp_path):
    journal_path = tmp_path / 'trades.jsonl.gz'
    journal = JsonlJournal(
        str(journal_path),
        run_id='run-test',
        stream_name='trades',
        soft_max_bytes=1,
        hard_max_bytes=1,
        min_free_bytes=0,
        critical_event_types={'v3_inventory_event'},
    )

    assert journal.write(
        'v3_inventory_event',
        {'inventory_event_type': 'ENTRY_FILLED'},
    ) is True
    assert journal.write(
        'v3_inventory_event',
        {'inventory_event_type': 'EXIT_FILLED'},
    ) is True

    with gzip.open(journal_path, 'rt', encoding='utf-8') as handle:
        records = [json.loads(line) for line in handle]

    assert [record['payload']['inventory_event_type'] for record in records] == [
        'ENTRY_FILLED'
    ]
    assert journal.written_count == 1
    assert journal.suppressed_count == 1
    assert journal.budget_reason == 'hard_max_bytes'
    assert journal.budget_metrics()['budget_event_emitted'] is False
