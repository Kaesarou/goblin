import sqlite3
from datetime import UTC, datetime, timedelta

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import ExitPriceSource
from app.persistence.closed_trade_memory_store import ClosedTradeMemoryStore
from app.risk.trade_cooldown import ClosedTradeMemoryEntry


CLOSED_AT = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


def memory_entry() -> ClosedTradeMemoryEntry:
    return ClosedTradeMemoryEntry(
        symbol='AMD',
        side='SELL',
        close_reason=PositionCloseReason.TAKE_PROFIT,
        opened_at=CLOSED_AT - timedelta(minutes=5),
        closed_at=CLOSED_AT,
        cooldown_expires_at=CLOSED_AT + timedelta(minutes=30),
        position_id='position-1',
        signal_price=100.0,
        executable_entry_estimate=99.9,
        broker_entry_fill_price=99.92,
        pnl_entry_price=99.92,
        pnl_exit_price=98.9,
        exit_price_source=ExitPriceSource.BROKER_FILL,
        take_profit=99.0,
        lowest_executable_price=98.8,
        gross_pnl=10.2,
        gross_pnl_percent=1.02,
        explicit_costs_deducted=0.5,
        net_pnl=9.7,
        net_pnl_percent=0.97,
        created_at=CLOSED_AT,
        session_key='US-2026-07-31',
    )


def test_store_round_trips_canonical_economics_and_session(tmp_path):
    path = str(tmp_path / 'goblin.sqlite')
    ClosedTradeMemoryStore(path).save_or_replace(memory_entry())

    loaded = ClosedTradeMemoryStore(path).find_latest(' amd ', ' sell ')

    assert loaded == memory_entry()


def test_store_keeps_recent_tp_after_fixed_cooldown_expiry(tmp_path):
    store = ClosedTradeMemoryStore(str(tmp_path / 'goblin.sqlite'))
    entry = store.save_or_replace(memory_entry())
    now = entry.closed_at + timedelta(minutes=45)

    assert store.find_active_cooldown(symbol='AMD', side='SELL', now=now) is None
    assert store.find_recent_take_profit(
        symbol='AMD',
        side='SELL',
        now=now,
        lookback_minutes=60,
    ) is not None


def test_empty_obsolete_table_is_rebuilt_without_legacy_columns(tmp_path):
    path = str(tmp_path / 'goblin.sqlite')
    with sqlite3.connect(path) as connection:
        connection.execute(
            '''
            CREATE TABLE closed_trade_memory (
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                close_reason TEXT NOT NULL, raw_close_reason TEXT,
                opened_at TEXT, closed_at TEXT NOT NULL,
                cooldown_expires_at TEXT NOT NULL, position_id TEXT,
                gross_pnl REAL, gross_pnl_percent REAL, created_at TEXT,
                session_key TEXT, PRIMARY KEY (symbol, side)
            )
            '''
        )

    store = ClosedTradeMemoryStore(path)
    store.save_or_replace(memory_entry())

    assert store.find_latest('AMD', 'SELL') == memory_entry()
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                'PRAGMA table_info(closed_trade_memory)'
            ).fetchall()
        }
    assert 'cooldown_contract_version' in columns
    assert 'raw_close_reason' not in columns


def test_nonempty_obsolete_table_is_preserved_and_refuses_inference(tmp_path):
    path = str(tmp_path / 'goblin.sqlite')
    with sqlite3.connect(path) as connection:
        connection.execute(
            '''
            CREATE TABLE closed_trade_memory (
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                close_reason TEXT NOT NULL, raw_close_reason TEXT,
                opened_at TEXT, closed_at TEXT NOT NULL,
                cooldown_expires_at TEXT NOT NULL, position_id TEXT,
                gross_pnl REAL, gross_pnl_percent REAL, created_at TEXT,
                session_key TEXT, PRIMARY KEY (symbol, side)
            )
            '''
        )
        connection.execute(
            '''
            INSERT INTO closed_trade_memory VALUES (
                'MU', 'SELL', 'take_profit', 'net_breakeven_stop_hit',
                ?, ?, ?, 'mu-1', 5.0, 0.05, ?, 'US-2026-07-31'
            )
            ''',
            (
                (CLOSED_AT - timedelta(minutes=5)).isoformat(),
                CLOSED_AT.isoformat(),
                (CLOSED_AT + timedelta(minutes=30)).isoformat(),
                CLOSED_AT.isoformat(),
            ),
        )

    try:
        ClosedTradeMemoryStore(path)
    except RuntimeError as exc:
        assert 'obsolete cooldown taxonomy' in str(exc)
    else:
        raise AssertionError('Obsolete cooldown rows must not be inferred.')

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            'SELECT close_reason, raw_close_reason FROM closed_trade_memory'
        ).fetchone()
    assert row == ('take_profit', 'net_breakeven_stop_hit')
