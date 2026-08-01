from datetime import UTC, datetime
from types import SimpleNamespace

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import EntryPriceSource, TrackedPosition
from app.persistence.trade_cooldown_store import TradeCooldownStore
from app.risk.trade_cooldown import TradeCooldownConfig
from app.runtime.closed_position_cooldown import (
    register_trade_cooldown_for_unknown_confirmed_close,
)


class Journal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def test_confirmed_external_close_without_price_uses_unknown_taxonomy(tmp_path):
    closed_at = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
    position = TrackedPosition(
        position_id='position-1',
        symbol='AMD',
        side='SELL',
        amount=1_000.0,
        signal_price=100.0,
        executable_entry_estimate=99.9,
        broker_entry_fill_price=None,
        pnl_entry_price=99.9,
        entry_price_source=EntryPriceSource.EXECUTABLE_ESTIMATE,
        stop_loss=101.0,
        take_profit=98.0,
        opened_at=closed_at,
    )
    store = TradeCooldownStore(str(tmp_path / 'goblin.sqlite'))
    journal = Journal()
    risk_manager = SimpleNamespace(
        risk_profile_for=lambda symbol: SimpleNamespace(
            trade_cooldown=TradeCooldownConfig()
        )
    )

    register_trade_cooldown_for_unknown_confirmed_close(
        position=position,
        closed_at=closed_at,
        risk_manager=risk_manager,
        cooldown_store=store,
        trade_journal=journal,
        session_key='EQUITY_US:test',
    )

    entry = store.find_latest('AMD', 'SELL')
    assert entry is not None
    assert entry.close_reason is PositionCloseReason.UNKNOWN_CONFIRMED_CLOSE
    assert journal.events[0][1]['source'] == (
        'unknown_confirmed_close_without_price'
    )
