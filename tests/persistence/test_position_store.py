import sqlite3
from datetime import UTC, datetime

import pytest

from app.execution.managed_stop import ManagedProtectionType
from app.execution.position_models import EntryPriceSource, TrackedPosition
from app.persistence.position_store import PositionStore


OPENED_AT = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


def position(position_id: str, symbol: str = 'MSFT') -> TrackedPosition:
    return TrackedPosition(
        position_id=position_id,
        symbol=symbol,
        side='BUY',
        amount=500.0,
        signal_price=100.0,
        executable_entry_estimate=100.1,
        broker_entry_fill_price=100.08,
        pnl_entry_price=100.08,
        entry_price_source=EntryPriceSource.BROKER_FILL,
        stop_loss=99.0,
        take_profit=102.0,
        opened_at=OPENED_AT,
        highest_executable_price=101.0,
        lowest_executable_price=99.8,
        highest_last_execution_price=101.1,
        lowest_last_execution_price=99.9,
        trailing_stop_net_buffer_percent=0.1,
        managed_stop_protection_type=ManagedProtectionType.TRAILING,
        estimated_explicit_cost=0.5,
        estimated_explicit_cost_percent=0.1,
        pretrade_estimated_spread_cost=1.0,
        pretrade_observed_spread_percent=0.2,
        pretrade_estimated_total_cost=1.5,
        pretrade_estimated_total_cost_percent=0.3,
        stale_position_enabled=True,
        stale_position_max_age_minutes=60,
        stale_position_min_favorable_move_percent=0.35,
        stale_position_buffer_percent=0.1,
    )


def test_position_store_round_trips_v2_economics_and_lifecycle(tmp_path):
    store = PositionStore(str(tmp_path / 'goblin.sqlite'))
    store.save_open_position(position('position-1'))

    loaded = store.load_open_positions()[0]

    assert loaded == position('position-1')


def test_position_store_saves_replaces_and_deletes(tmp_path):
    store = PositionStore(str(tmp_path / 'goblin.sqlite'))
    store.save_open_position(position('position-1', 'MSFT'))
    store.save_open_position(position('position-1', 'AAPL'))

    assert [item.symbol for item in store.load_open_positions()] == ['AAPL']
    store.delete_open_position('position-1')
    assert store.load_open_positions() == []


def test_empty_obsolete_table_is_rebuilt_without_legacy_columns(tmp_path):
    path = tmp_path / 'goblin.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE open_positions ('
            'position_id TEXT PRIMARY KEY, entry_price REAL NOT NULL)'
        )

    store = PositionStore(str(path))
    store.save_open_position(position('position-1'))

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(open_positions)'
            ).fetchall()
        }
    assert 'position_contract_version' in columns
    assert 'pnl_entry_price' in columns
    assert 'entry_price' not in columns


def test_nonempty_obsolete_table_preserves_rows_and_refuses_inference(tmp_path):
    path = tmp_path / 'goblin.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TABLE open_positions ('
            'position_id TEXT PRIMARY KEY, entry_price REAL NOT NULL)'
        )
        connection.execute(
            "INSERT INTO open_positions VALUES ('position-legacy', 100.0)"
        )

    with pytest.raises(RuntimeError, match='flat portfolio'):
        PositionStore(str(path))

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM open_positions'
        ).fetchone()[0] == 1
