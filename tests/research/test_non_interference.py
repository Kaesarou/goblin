from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.instruments.models import AssetClass
from app.journal.serialization import serialize_value
from app.market.data_quality import MarketDataQualityConfig, MarketDataValidator
from app.market.models import MarketSnapshot
from app.market_data.candle_stream import QualityAwareCandleBuilder
from app.market_data.coordinator import MarketDataCoordinator
from app.market_data.models import MarketDataEvent, MarketDataSource
from app.runtime.clocked_candle_flow import ClockedCandleFlow
from app.runtime.market_data_event_flow import MarketDataEventFlow
from app.runtime.trading_session_window import TradingSessionDecision

START = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


class RecordingJournal:
    def __init__(self) -> None:
        self.events = []

    def write(self, event_type, payload):
        self.events.append((event_type, payload))
        return True


class RecordingStrategy:
    def __init__(self) -> None:
        self.snapshots = []

    def on_snapshot(self, snapshot):
        self.snapshots.append(snapshot)


class RecordingContext:
    def __init__(self) -> None:
        self.accepted = []

    def observe_accepted_snapshot(self, snapshot):
        self.accepted.append(snapshot)


class RecordingBrokerOperations:
    def __init__(self) -> None:
        self.snapshots = []

    def on_snapshot(self, *, snapshot, session_decision, source):
        self.snapshots.append((snapshot, session_decision, source))


class DownstreamTrace:
    def __init__(self) -> None:
        self.candidates = []
        self.candidate_sides = []
        self.selection = []
        self.entry_decisions = []
        self.risk_plans = []
        self.cooldowns = []
        self.positions = []
        self.lifecycle = []
        self.broker_tasks = []

    def record(
        self,
        *,
        closed_at,
        symbol,
        expected_symbols,
        candidate,
    ):
        self.candidates.append(candidate)
        self.candidate_sides.append(candidate.side)
        self.selection.append(
            {'closed_at': closed_at, 'selected': [candidate.symbol]}
        )
        self.entry_decisions.append(
            {
                'candidate_id': candidate.candidate_id,
                'action': 'READY_FOR_SELECTION',
            }
        )
        self.risk_plans.append(
            {
                'symbol': candidate.symbol,
                'side': candidate.side,
                'stop_loss_percent': 1.2,
                'take_profit_percent': 2.0,
            }
        )
        self.positions.append(
            {
                'symbol': candidate.symbol,
                'side': candidate.side,
                'status': 'open',
            }
        )
        self.cooldowns.append(
            {'symbol': candidate.symbol, 'allowed': True}
        )
        self.lifecycle.append(
            {
                'symbol': candidate.symbol,
                'event': 'initial_protection_armed',
                'breakeven': False,
                'trailing': False,
                'stale_exit': False,
                'force_close': False,
                'live_pnl': 0.0,
            }
        )
        self.broker_tasks.append(
            {
                'operation': 'open_position',
                'symbol': candidate.symbol,
                'side': candidate.side,
            }
        )
        return True


class RecordingResearchSidecar:
    def __init__(self) -> None:
        self.payload_events = []
        self.accepted_snapshots = []
        self.states = []

    def observe_payload_schema(self, event):
        self.payload_events.append(event)

    def observe_accepted_snapshot(self, snapshot):
        self.accepted_snapshots.append(snapshot)

    def maybe_emit(self, **kwargs):
        self.states.append(kwargs)
        return True


class TraceRuntime(MarketDataEventFlow, ClockedCandleFlow):
    def __init__(self, *, research_enabled: bool) -> None:
        decision = TradingSessionDecision(
            asset_class=AssetClass.EQUITY_US,
            session_active=True,
            session_24_7=False,
            collect_snapshots=True,
            new_entries_allowed=True,
            force_close_required=False,
            reason='session_tradable',
            session_start_time=START,
            session_end_time=START + timedelta(hours=6, minutes=30),
            time_until_session_end_minutes=390.0,
            session_key='EQUITY_US:test-session',
        )
        self.symbols = ['AAPL']
        self.active_symbols = ['AAPL']
        self.session_decisions = {'AAPL': decision}
        self.context_asset_classes = {}
        self.latest_snapshots = {}
        self.strategies = {'AAPL': RecordingStrategy()}
        self.candle_builders = {'AAPL': QualityAwareCandleBuilder()}
        self.market_data_validator = MarketDataValidator()
        self.coordinator = MarketDataCoordinator(
            websocket_required=True,
            symbol_silence_seconds=120.0,
        )
        self.coordinator.initialize_symbols(['AAPL'], now=START)
        self.broker_operations = RecordingBrokerOperations()
        self.market_context_service = RecordingContext()
        self.trade_journal = RecordingJournal()
        self.market_journal = RecordingJournal()
        self.candle_journal = RecordingJournal()
        self.errors_journal = RecordingJournal()
        self.debug_decisions_journal = RecordingJournal()
        self.daily_summary = {'schema_version': 10, 'positions': 0}
        self.decision_windows = DownstreamTrace()
        self.loop_id = 7
        self.run_id = 'run-test'
        self._last_bucket_by_symbol = {}
        self.risk_manager = object()
        self.pending_entry_manager = object()
        self.cooldown_guard = object()
        self.multi_timeframe_service = object()
        self.research_pipeline = (
            RecordingResearchSidecar() if research_enabled else None
        )

    def _market_data_quality_config(self, symbol):
        return MarketDataQualityConfig()

    def _invalidate_pending_after_symbol_lock(self, symbol, observed_at):
        return None

    def _session_decision_for_market_data_symbol(self, symbol):
        return self.session_decisions.get(symbol)


def _events():
    result = []
    for index, (seconds, price) in enumerate(
        ((5, 100.0), (20, 100.1), (40, 100.2), (65, 100.3))
    ):
        observed_at = START + timedelta(seconds=seconds)
        result.append(
            MarketDataEvent(
                symbol='AAPL',
                source=MarketDataSource.WEBSOCKET,
                received_at=observed_at,
                snapshot=MarketSnapshot(
                    symbol='AAPL',
                    bid=price - 0.01,
                    ask=price + 0.01,
                    last=price,
                    timestamp=observed_at,
                    received_at=observed_at,
                ),
                message_id=f'm-{index}',
                connection_id='c-1',
                price_changed=True,
            )
        )
    return result


def _run(runtime, events):
    for event in events:
        runtime._handle_event(event, event.received_at)
    trace = runtime.decision_windows
    return serialize_value(
        {
            'candles': runtime.candle_journal.events,
            'candidates': trace.candidates,
            'candidate_sides': trace.candidate_sides,
            'selection': trace.selection,
            'entry_decisions': trace.entry_decisions,
            'risk_plans': trace.risk_plans,
            'cooldowns': trace.cooldowns,
            'positions': trace.positions,
            'lifecycle': trace.lifecycle,
            'broker_tasks': trace.broker_tasks,
            'trade_journal': runtime.trade_journal.events,
            'market_journal': runtime.market_journal.events,
            'errors_journal': runtime.errors_journal.events,
            'debug_decisions_journal': (
                runtime.debug_decisions_journal.events
            ),
            'daily_summary': runtime.daily_summary,
            'strategy_snapshots': runtime.strategies['AAPL'].snapshots,
            'broker_snapshots': runtime.broker_operations.snapshots,
        }
    )


def test_same_market_data_sequence_has_identical_trading_trace(
    monkeypatch,
):
    def deterministic_candidate(**kwargs):
        candle = kwargs['closed_candle']
        side = 'BUY' if candle.close >= candle.open else 'SELL'
        candidate = SimpleNamespace(
            candidate_id=f"candidate:{candle.closed_at.isoformat()}",
            symbol=kwargs['symbol'],
            side=side,
            candle=candle,
            snapshot=kwargs['snapshot'],
        )
        kwargs['trade_journal'].write(
            'candidate_generated',
            {'candidate': candidate, 'side': side},
        )
        return candidate

    monkeypatch.setattr(
        'app.runtime.clocked_candle_flow.process_closed_candle',
        deterministic_candidate,
    )
    events = _events()
    disabled = TraceRuntime(research_enabled=False)
    enabled = TraceRuntime(research_enabled=True)

    disabled_trace = _run(disabled, events)
    enabled_trace = _run(enabled, events)

    assert enabled_trace == disabled_trace
    assert enabled_trace['candidate_sides'] == ['BUY']
    assert enabled_trace['risk_plans'][0]['stop_loss_percent'] == 1.2
    assert enabled_trace['risk_plans'][0]['take_profit_percent'] == 2.0
    assert enabled_trace['lifecycle'][0]['breakeven'] is False
    assert enabled_trace['lifecycle'][0]['trailing'] is False
    assert enabled_trace['lifecycle'][0]['stale_exit'] is False
    assert enabled_trace['lifecycle'][0]['force_close'] is False
    assert enabled_trace['lifecycle'][0]['live_pnl'] == 0.0
    assert len(enabled.research_pipeline.accepted_snapshots) == len(events)
    assert len(enabled.research_pipeline.states) == 1


def test_trading_decision_packages_do_not_import_research_runtime():
    repository = Path(__file__).resolve().parents[2]
    forbidden_roots = (
        repository / 'app' / 'strategies',
        repository / 'app' / 'execution',
        repository / 'app' / 'risk',
    )
    offenders = []
    for root in forbidden_roots:
        for source in root.rglob('*.py'):
            if 'app.research' in source.read_text(encoding='utf-8'):
                offenders.append(source.relative_to(repository).as_posix())

    assert offenders == []
