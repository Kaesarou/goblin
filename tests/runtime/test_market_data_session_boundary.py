from datetime import UTC, datetime, timedelta

from app.instruments.models import AssetClass
from app.market.data_quality import (
    MarketDataQualityConfig,
    MarketDataValidator,
)
from app.market.models import MarketSnapshot
from app.market_data.candle_stream import QualityAwareCandleBuilder
from app.market_data.coordinator import MarketDataCoordinator
from app.market_data.models import MarketDataEvent, MarketDataSource
from app.runtime.market_data_event_flow import MarketDataEventFlow
from app.runtime.market_data_maintenance import MarketDataMaintenance
from app.runtime.market_data_session_flow import MarketDataSessionFlow
from app.runtime.session_runtime import SNAPSHOT_BEFORE_SESSION_START
from app.runtime.trading_session_window import TradingSessionDecision

SESSION_START = datetime(2026, 7, 23, 13, 30, tzinfo=UTC)
SESSION_END = datetime(2026, 7, 23, 20, 0, tzinfo=UTC)


class RecordingJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class RecordingBrokerOperations:
    def __init__(self) -> None:
        self.snapshots: list[MarketSnapshot] = []

    def on_snapshot(self, *, snapshot, session_decision, source) -> None:
        self.snapshots.append(snapshot)


class RecordingStrategy:
    def __init__(self) -> None:
        self.snapshots: list[MarketSnapshot] = []

    def on_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.snapshots.append(snapshot)


class RecordingMarketContext:
    def __init__(self) -> None:
        self.calls: list[dict[str, MarketSnapshot]] = []
        self.accepted_snapshots: list[MarketSnapshot] = []

    def observe_accepted_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.accepted_snapshots.append(snapshot)

    def update(
        self,
        *,
        snapshots,
        session_decisions,
        context_asset_classes,
    ) -> None:
        self.calls.append(dict(snapshots))


class BoundaryRuntime(
    MarketDataEventFlow,
    MarketDataMaintenance,
    MarketDataSessionFlow,
):
    def __init__(self) -> None:
        decision = TradingSessionDecision(
            asset_class=AssetClass.EQUITY_US,
            session_active=True,
            session_24_7=False,
            collect_snapshots=True,
            new_entries_allowed=True,
            force_close_required=False,
            reason='session_tradable',
            session_start_time=SESSION_START,
            session_end_time=SESSION_END,
            time_until_session_end_minutes=390.0,
            session_key='equity_us:test-session',
        )
        self.symbols = ['AMZN']
        self.active_symbols = ['AMZN']
        self.session_decisions = {'AMZN': decision}
        self.context_asset_classes = {'SPX500': AssetClass.EQUITY_US}
        self.latest_snapshots: dict[str, MarketSnapshot] = {}
        self.strategies = {'AMZN': RecordingStrategy()}
        self.candle_builders = {'AMZN': QualityAwareCandleBuilder()}
        self.market_data_validator = MarketDataValidator()
        self.coordinator = MarketDataCoordinator(
            websocket_required=True,
            symbol_silence_seconds=120.0,
        )
        self.coordinator.initialize_symbols(
            ['AMZN', 'SPX500'],
            now=SESSION_START,
        )
        self.broker_operations = RecordingBrokerOperations()
        self.market_context_service = RecordingMarketContext()
        self.trade_journal = RecordingJournal()
        self.market_journal = RecordingJournal()
        self.loop_id = 1
        self._last_context_update = 0.0

    def _market_data_quality_config(
        self,
        symbol: str,
    ) -> MarketDataQualityConfig:
        return MarketDataQualityConfig()

    def _invalidate_pending_after_symbol_lock(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> None:
        return None


def _event(
    symbol: str,
    *,
    timestamp: datetime,
    received_at: datetime,
    price: float = 100.0,
) -> MarketDataEvent:
    snapshot = MarketSnapshot(
        symbol=symbol,
        bid=price - 0.1,
        ask=price + 0.1,
        last=price,
        timestamp=timestamp,
        received_at=received_at,
    )
    return MarketDataEvent(
        symbol=symbol,
        source=MarketDataSource.WEBSOCKET,
        received_at=received_at,
        snapshot=snapshot,
        message_id=f'{symbol}:{timestamp.isoformat()}',
        connection_id='test-connection',
    )


def test_cached_preopen_snapshot_stays_position_only():
    runtime = BoundaryRuntime()
    received_at = SESSION_START + timedelta(seconds=1)
    cached_at = SESSION_START - timedelta(milliseconds=206)

    runtime._handle_event(
        _event('AMZN', timestamp=cached_at, received_at=received_at),
        received_at,
    )
    runtime._handle_event(
        _event('SPX500', timestamp=cached_at, received_at=received_at),
        received_at,
    )
    runtime._update_context_if_due(2.0)

    assert set(runtime.latest_snapshots) == {'AMZN', 'SPX500'}
    assert [item.symbol for item in runtime.broker_operations.snapshots] == [
        'AMZN',
        'SPX500',
    ]
    assert runtime.strategies['AMZN'].snapshots == []
    assert runtime.market_context_service.calls == []
    assert runtime.candle_builders['AMZN'].finalize_until(
        SESSION_START + timedelta(seconds=2),
        grace_seconds=1.0,
        max_carry_forward_age_seconds=120.0,
    ) == []

    ignored = [
        payload
        for event_type, payload in runtime.trade_journal.events
        if event_type == 'market_data_event_ignored'
    ]
    assert len(ignored) == 2
    assert {payload['reason'] for payload in ignored} == {
        SNAPSHOT_BEFORE_SESSION_START
    }
    assert all(
        payload['position_management_forwarded'] for payload in ignored
    )

    valid_at = SESSION_START + timedelta(seconds=3)
    runtime._handle_event(
        _event('AMZN', timestamp=valid_at, received_at=valid_at),
        valid_at,
    )
    runtime._update_context_if_due(4.0)

    assert runtime.market_context_service.calls == [
        {'AMZN': runtime.latest_snapshots['AMZN']}
    ]


def test_in_session_snapshot_enters_strategy_candles_and_context():
    runtime = BoundaryRuntime()
    received_at = SESSION_START + timedelta(seconds=2)

    runtime._handle_event(
        _event('AMZN', timestamp=received_at, received_at=received_at),
        received_at,
    )
    runtime._handle_event(
        _event('SPX500', timestamp=received_at, received_at=received_at),
        received_at,
    )
    runtime._update_context_if_due(2.0)

    assert [item.symbol for item in runtime.strategies['AMZN'].snapshots] == [
        'AMZN'
    ]
    assert runtime.market_context_service.calls == [
        {
            'AMZN': runtime.latest_snapshots['AMZN'],
            'SPX500': runtime.latest_snapshots['SPX500'],
        }
    ]


def test_quarantined_quote_never_reaches_position_or_strategy_flow():
    runtime = BoundaryRuntime()
    for index in range(21):
        observed_at = SESSION_START + timedelta(seconds=index + 1)
        runtime._handle_event(
            _event(
                'AMZN',
                timestamp=observed_at,
                received_at=observed_at,
                price=100.0 + index * 0.005,
            ),
            observed_at,
        )
    accepted_count = len(runtime.broker_operations.snapshots)
    suspect_at = SESSION_START + timedelta(seconds=22)

    runtime._handle_event(
        _event(
            'AMZN',
            timestamp=suspect_at,
            received_at=suspect_at,
            price=99.10,
        ),
        suspect_at,
    )

    assert len(runtime.broker_operations.snapshots) == accepted_count
    assert len(runtime.strategies['AMZN'].snapshots) == accepted_count
    assert runtime.latest_snapshots['AMZN'].last == 100.10
    assert any(
        event_type == 'market_data_quarantined'
        for event_type, _ in runtime.trade_journal.events
    )

    recovery_at = SESSION_START + timedelta(seconds=23)
    runtime._handle_event(
        _event(
            'AMZN',
            timestamp=recovery_at,
            received_at=recovery_at,
            price=100.105,
        ),
        recovery_at,
    )

    assert len(runtime.broker_operations.snapshots) == accepted_count + 1
    assert any(
        event_type == 'market_data_quality_resolved'
        for event_type, _ in runtime.trade_journal.events
    )
