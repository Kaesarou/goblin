import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from app.execution.candidate_economics import CandidateEconomicsEstimator
from app.execution.position_tracker import PositionTracker
from app.execution.trade_executor import TradeExecutor
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.jsonl_journal import JsonlJournal
from app.market.data_quality import MarketDataValidator
from app.market.market_context import MarketContextService
from app.market.session_timeframe_service import FullSessionMultiTimeframeService
from app.market_data.candle_stream import QualityAwareCandleBuilder
from app.market_data.contracts import LiveMarketDataFeed, RestMarketDataClient
from app.market_data.coordinator import MarketDataCoordinator
from app.market_data.models import MARKET_DATA_MODEL_VERSION
from app.persistence.pending_close_store import PendingCloseStore
from app.persistence.position_store import PositionStore
from app.persistence.trade_cooldown_store import TradeCooldownStore
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.runtime.broker_task_runner import BrokerTaskLane, BrokerTaskRunner
from app.runtime.clocked_candle_flow import ClockedCandleFlow
from app.runtime.decision_window import DecisionWindowCoordinator
from app.runtime.market_data_event_flow import MarketDataEventFlow
from app.runtime.market_data_maintenance import MarketDataMaintenance
from app.runtime.market_data_session_flow import MarketDataSessionFlow
from app.runtime.pending_entry import PendingEntryManager
from app.runtime.resilient_broker_operations import (
    ResilientBrokerOperationsCoordinator,
)
from app.runtime.resilient_candidate_execution import (
    ResilientCandidateExecutionCoordinator,
)
from app.runtime.runtime_heartbeat import RuntimeHeartbeat
from app.runtime.runtime_policy import (
    CANDLE_CLOCK_GRACE_SECONDS,
    DECISION_WINDOW_GRACE_SECONDS,
    POSITION_FALLBACK_INTERVAL_SECONDS,
    POSITION_RECONCILIATION_GRACE_SECONDS,
    POSITION_RECONCILIATION_MISS_INTERVAL_SECONDS,
    POSITION_RECONCILIATION_REQUIRED_MISSES,
    REST_CONTROL_ANOMALY_PERCENT,
    REST_CONTROL_INTERVAL_SECONDS,
    UNKNOWN_ORDER_LOOKUP_INTERVAL_SECONDS,
    UNKNOWN_ORDER_MAX_AGE_MINUTES,
    WS_POSITION_SILENCE_SECONDS,
)
from app.runtime.trading_session_window import TradingSessionState
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from app.strategies.strategy import TrendStrategy

logger = logging.getLogger(__name__)
BrokerAuthorizationErrorChecker = Callable[[Exception], bool]


class EventDrivenMarketRuntime(
    MarketDataSessionFlow,
    MarketDataEventFlow,
    MarketDataMaintenance,
    ClockedCandleFlow,
):
    def __init__(
        self,
        *,
        settings,
        symbols: list[str],
        run_id: str,
        strategy_profile: BalancedStrategyConfig,
        instrument_registry: InstrumentRegistry,
        execution_broker,
        rest_market_data: RestMarketDataClient,
        live_market_data: LiveMarketDataFeed,
        strategies: dict[str, TrendStrategy],
        candle_builders: dict[str, QualityAwareCandleBuilder],
        trading_session_service,
        trading_session_state: TradingSessionState,
        risk_manager: RiskManager,
        candidate_economics_estimator: CandidateEconomicsEstimator,
        executor: TradeExecutor,
        position_tracker: PositionTracker,
        position_store: PositionStore,
        pending_close_store: PendingCloseStore,
        cooldown_store: TradeCooldownStore,
        cooldown_guard: TradeCooldownGuard,
        pending_entry_manager: PendingEntryManager,
        market_data_validator: MarketDataValidator,
        market_context_service: MarketContextService,
        multi_timeframe_service: FullSessionMultiTimeframeService,
        trade_journal: JsonlJournal,
        market_journal: JsonlJournal,
        candle_journal: JsonlJournal,
        heartbeat: RuntimeHeartbeat,
        is_broker_authorization_error: BrokerAuthorizationErrorChecker,
    ) -> None:
        self.settings = settings
        self.symbols = symbols
        self.run_id = run_id
        self.strategy_profile = strategy_profile
        self.instrument_registry = instrument_registry
        self.execution_broker = execution_broker
        self.rest_market_data = rest_market_data
        self.live_market_data = live_market_data
        self.strategies = strategies
        self.candle_builders = candle_builders
        self.trading_session_service = trading_session_service
        self.trading_session_state = trading_session_state
        self.risk_manager = risk_manager
        self.candidate_economics_estimator = candidate_economics_estimator
        self.executor = executor
        self.position_tracker = position_tracker
        self.position_store = position_store
        self.pending_close_store = pending_close_store
        self.cooldown_store = cooldown_store
        self.cooldown_guard = cooldown_guard
        self.pending_entry_manager = pending_entry_manager
        self.market_data_validator = market_data_validator
        self.market_context_service = market_context_service
        self.multi_timeframe_service = multi_timeframe_service
        self.trade_journal = trade_journal
        self.market_journal = market_journal
        self.candle_journal = candle_journal
        self.heartbeat = heartbeat
        self.is_broker_authorization_error = is_broker_authorization_error
        self.coordinator = MarketDataCoordinator(
            websocket_required=True,
            symbol_silence_seconds=WS_POSITION_SILENCE_SECONDS,
        )
        self.decision_windows = DecisionWindowCoordinator(
            grace_seconds=DECISION_WINDOW_GRACE_SECONDS
        )
        self.broker_task_runner = BrokerTaskRunner()
        self.broker_operations = ResilientBrokerOperationsCoordinator(
            runner=self.broker_task_runner,
            execution_broker=execution_broker,
            rest_market_data=rest_market_data,
            executor=executor,
            position_tracker=position_tracker,
            risk_manager=risk_manager,
            position_store=position_store,
            pending_close_store=pending_close_store,
            cooldown_store=cooldown_store,
            trade_journal=trade_journal,
            market_data_coordinator=self.coordinator,
            is_broker_authorization_error=is_broker_authorization_error,
            instrument_registry=instrument_registry,
            reconciliation_grace_seconds=(
                POSITION_RECONCILIATION_GRACE_SECONDS
            ),
            reconciliation_required_misses=(
                POSITION_RECONCILIATION_REQUIRED_MISSES
            ),
            reconciliation_miss_interval_seconds=(
                POSITION_RECONCILIATION_MISS_INTERVAL_SECONDS
            ),
            rest_control_anomaly_percent=REST_CONTROL_ANOMALY_PERCENT,
        )
        self.candidate_execution = ResilientCandidateExecutionCoordinator(
            runner=self.broker_task_runner,
            execution_broker=execution_broker,
            executor=executor,
            risk_manager=risk_manager,
            position_tracker=position_tracker,
            position_store=position_store,
            trade_journal=trade_journal,
            strategy_profile=strategy_profile,
            cooldown_guard=cooldown_guard,
            candidate_economics_estimator=candidate_economics_estimator,
            pending_entry_manager=pending_entry_manager,
            unknown_lookup_interval_seconds=(
                UNKNOWN_ORDER_LOOKUP_INTERVAL_SECONDS
            ),
            unknown_max_age_minutes=UNKNOWN_ORDER_MAX_AGE_MINUTES,
        )
        self.latest_snapshots = {}
        self.session_decisions = {}
        self.active_symbols: list[str] = []
        self.context_asset_classes = {}
        self.loop_id = 0
        self._last_session_refresh = 0.0
        self._last_context_update = 0.0
        self._last_rest_control = 0.0
        self._last_position_fallback = 0.0
        self._last_position_reconciliation = 0.0
        self._last_bucket_by_symbol = {}
        self._degraded_buckets: set[tuple[str, datetime]] = set()
        self._feed_started = False
        self._subscribed_symbols: tuple[str, ...] = ()
        self._applied_feed_symbols: tuple[str, ...] = ()

    def run(self) -> str:
        session_now = datetime.now(timezone.utc)
        session_monotonic = time.monotonic()
        self._refresh_sessions_if_due(session_now, session_monotonic)
        monitored_symbols = self._desired_market_data_symbols()

        self.live_market_data.start(monitored_symbols)
        self._feed_started = True
        self._subscribed_symbols = tuple(monitored_symbols)
        self._applied_feed_symbols = self.live_market_data.subscribed_symbols()
        started_at = datetime.now(timezone.utc)
        self.coordinator.initialize_symbols(
            list(self._applied_feed_symbols),
            now=started_at,
        )

        started_monotonic = time.monotonic()
        self._last_rest_control = started_monotonic
        self._last_position_reconciliation = started_monotonic
        self.trade_journal.write(
            'market_data_runtime_started',
            {
                'market_data_version': MARKET_DATA_MODEL_VERSION,
                'primary_source': 'websocket',
                'requested_symbols': monitored_symbols,
                'subscribed_symbols': list(self._applied_feed_symbols),
                'rest_control_interval_seconds': (
                    REST_CONTROL_INTERVAL_SECONDS
                ),
                'position_silence_seconds': WS_POSITION_SILENCE_SECONDS,
                'position_fallback_interval_seconds': (
                    POSITION_FALLBACK_INTERVAL_SECONDS
                ),
                'decision_window_grace_seconds': (
                    DECISION_WINDOW_GRACE_SECONDS
                ),
                'candle_clock_grace_seconds': CANDLE_CLOCK_GRACE_SECONDS,
            },
        )
        try:
            while True:
                self.loop_id += 1
                now = datetime.now(timezone.utc)
                monotonic_now = time.monotonic()
                self._drain_broker_completions(now)
                self._refresh_sessions_if_due(now, monotonic_now)
                self._refresh_applied_market_data_subscription(now)
                self._reconcile_positions_if_due(now, monotonic_now)
                event = self.live_market_data.next_event(timeout_seconds=0.10)
                now = datetime.now(timezone.utc)
                monotonic_now = time.monotonic()
                if event is not None:
                    self._handle_event(event, now)
                self._finalize_clocked_candles(now)
                self._update_context_if_due(monotonic_now)
                self._run_position_fallback_if_due(now, monotonic_now)
                self._run_rest_control_if_due(now, monotonic_now)
                self._flush_decision_windows(now)
                self.candidate_execution.schedule_unknown_order_lookups(
                    now=now
                )
                self._drain_broker_completions(now)
                self.heartbeat.maybe_emit(
                    journal=self.trade_journal,
                    logger=logger,
                    metrics=self.trade_journal.runtime_metrics(),
                    open_positions=(
                        len(self.position_tracker.open_positions_snapshot())
                        + self.candidate_execution.pending_open_count()
                    ),
                    active_symbols=len(self.active_symbols),
                )
        except KeyboardInterrupt:
            self.trade_journal.write(
                'runtime_interrupted',
                {'run_id': self.run_id, 'loop_id': self.loop_id},
            )
            logger.info('Stopping Goblin!')
            return 'stopped'
        finally:
            self._feed_started = False
            self.live_market_data.stop()
            self.broker_task_runner.close(wait=False)
            self.trade_journal.write(
                'market_data_runtime_stopped',
                {
                    'coordinator_metrics': self.coordinator.metrics,
                    'feed_diagnostics': self.live_market_data.diagnostics(),
                    'symbol_states': self.coordinator.snapshot(),
                    'broker_operations': self.broker_operations.diagnostics(),
                    'candidate_execution': self.candidate_execution.diagnostics(),
                    'broker_tasks_pending': (
                        self.broker_task_runner.pending_count()
                    ),
                    'broker_tasks_pending_standard': (
                        self.broker_task_runner.pending_count(
                            lane=BrokerTaskLane.STANDARD
                        )
                    ),
                    'broker_tasks_pending_close': (
                        self.broker_task_runner.pending_count(
                            lane=BrokerTaskLane.CLOSE
                        )
                    ),
                    'loop_id': self.loop_id,
                },
            )

    def _drain_broker_completions(self, now: datetime) -> None:
        for completion in self.broker_task_runner.drain():
            if self.candidate_execution.handle_completion(
                completion,
                now=now,
            ):
                continue
            self.broker_operations.handle_completion(
                completion,
                now=now,
                latest_snapshots=self.latest_snapshots,
            )
