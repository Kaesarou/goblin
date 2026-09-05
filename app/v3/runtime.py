from __future__ import annotations

import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Mapping

from app.brokers.etoro.account_equity_mapper import ACCOUNT_EQUITY_SOURCE
from app.market.data_quality import MarketDataStatus, MarketDataValidator
from app.market.models import MarketSnapshot
from app.market_data.coordinator import MarketDataCoordinator
from app.market_data.models import MarketDataEvent, MarketDataSource
from app.runtime.broker_task_runner import BrokerTaskLane, BrokerTaskRunner
from app.runtime.runtime_policy import (
    CANDLE_CLOCK_GRACE_SECONDS,
    CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS,
    DECISION_WINDOW_GRACE_SECONDS,
    POSITION_FALLBACK_INTERVAL_SECONDS,
    WS_POSITION_SILENCE_SECONDS,
)
from app.runtime.session_runtime import session_timestamp_rejection_reason
from app.v3.book import InventoryBook
from app.v3.features import OnlineFeatureEngine, OnlineFeatureSnapshot
from app.v3.forager import ForagerCandidate, NoVolumeForager
from app.v3.intents import RestingIntentBook
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import DecisionBatch, DecisionReason, MarketState, OrderIntent
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety
from app.v3.state_store import V3RuntimeStateStore

logger = logging.getLogger(__name__)

V3_RUNTIME_CONTRACT_VERSION = "inventory_runtime_v3_6"

_MATERIAL_DECISION_REASONS = frozenset(
    {
        DecisionReason.RECOVERABILITY_ACCEPTED,
        DecisionReason.RECOVERABILITY_REJECTED,
        DecisionReason.MAX_ENTRY_FILLS,
        DecisionReason.SYMBOL_EXPOSURE_CAP,
        DecisionReason.PORTFOLIO_EXPOSURE_CAP,
        DecisionReason.ECONOMICS_REJECTED,
        DecisionReason.TRAILING_EXIT,
        DecisionReason.UNSTUCK,
    }
)


@dataclass
class _DecisionWindow:
    closed_at: datetime
    expected_symbols: set[str]
    feature_by_symbol: dict[str, OnlineFeatureSnapshot]
    snapshot_by_symbol: dict[str, MarketSnapshot]
    quality_by_symbol: dict[str, bool]


@dataclass(frozen=True)
class V3DecisionWindowBatch:
    closed_at: datetime
    expected_symbols: tuple[str, ...]
    completed_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    finalization_reason: str
    features: Mapping[str, OnlineFeatureSnapshot]
    snapshots: Mapping[str, MarketSnapshot]
    quality: Mapping[str, bool]


class V3DecisionWindowCoordinator:
    """Synchronize completed M1 states without candidate-domain coupling."""

    def __init__(self, *, grace_seconds: float = DECISION_WINDOW_GRACE_SECONDS) -> None:
        self.grace_seconds = float(grace_seconds)
        self._windows: dict[datetime, _DecisionWindow] = {}
        self._finalized: set[datetime] = set()

    def record(
        self,
        *,
        feature: OnlineFeatureSnapshot,
        snapshot: MarketSnapshot,
        quality_ok: bool,
        expected_symbols: set[str],
    ) -> bool:
        key = _utc(feature.asof)
        if key in self._finalized:
            return False
        window = self._windows.setdefault(
            key,
            _DecisionWindow(key, set(expected_symbols), {}, {}, {}),
        )
        window.expected_symbols.update(expected_symbols)
        window.feature_by_symbol[feature.symbol] = feature
        window.snapshot_by_symbol[feature.symbol] = snapshot
        window.quality_by_symbol[feature.symbol] = bool(quality_ok)
        return True

    def reset_symbol(self, symbol: str) -> None:
        for key, window in list(self._windows.items()):
            window.expected_symbols.discard(symbol)
            window.feature_by_symbol.pop(symbol, None)
            window.snapshot_by_symbol.pop(symbol, None)
            window.quality_by_symbol.pop(symbol, None)
            if not window.expected_symbols and not window.feature_by_symbol:
                self._windows.pop(key)

    def pop_ready(self, *, now: datetime) -> tuple[V3DecisionWindowBatch, ...]:
        actual_now = _utc(now)
        ready: list[tuple[datetime, str]] = []
        for key, window in self._windows.items():
            complete = window.expected_symbols.issubset(window.feature_by_symbol)
            expired = actual_now >= key + timedelta(seconds=self.grace_seconds)
            if complete:
                ready.append((key, "all_symbols_completed"))
            elif expired:
                ready.append((key, "grace_expired"))

        result: list[V3DecisionWindowBatch] = []
        for key, reason in sorted(ready):
            window = self._windows.pop(key)
            self._finalized.add(key)
            completed = set(window.feature_by_symbol)
            result.append(
                V3DecisionWindowBatch(
                    closed_at=key,
                    expected_symbols=tuple(sorted(window.expected_symbols)),
                    completed_symbols=tuple(sorted(completed)),
                    missing_symbols=tuple(sorted(window.expected_symbols - completed)),
                    finalization_reason=reason,
                    features=dict(window.feature_by_symbol),
                    snapshots=dict(window.snapshot_by_symbol),
                    quality=dict(window.quality_by_symbol),
                )
            )

        cutoff = actual_now - timedelta(days=1)
        self._finalized = {value for value in self._finalized if value >= cutoff}
        return tuple(result)


class GoblinV3Runtime:
    """Event-driven V3 runtime around the same pure planner used by replay.

    Raw market/candle streams stay reconstructible and comparable with the frozen
    research corpus. High-volume V3 diagnostics are aggregated into heartbeats;
    detailed trade journal events are emitted only on material state changes,
    intents and anomalies.
    """

    def __init__(
        self,
        *,
        settings,
        symbols: list[str],
        run_id: str,
        instrument_registry,
        execution_broker,
        rest_market_data,
        live_market_data,
        candle_builders: Mapping[str, object],
        trading_session_service,
        market_context_service,
        multi_timeframe_service,
        market_data_validator: MarketDataValidator,
        planner,
        config,
        feature_engine: OnlineFeatureEngine,
        event_store: InventoryEventStore,
        runtime_state_store: V3RuntimeStateStore,
        trade_journal,
        market_journal,
        candle_journal,
        heartbeat,
        research_pipeline=None,
    ) -> None:
        self.settings = settings
        self.symbols = [symbol.strip().upper() for symbol in symbols]
        self.run_id = run_id
        self.instrument_registry = instrument_registry
        self.execution_broker = execution_broker
        self.rest_market_data = rest_market_data
        self.live_market_data = live_market_data
        self.candle_builders = dict(candle_builders)
        self.trading_session_service = trading_session_service
        self.market_context_service = market_context_service
        self.multi_timeframe_service = multi_timeframe_service
        self.market_data_validator = market_data_validator
        self.fallback_validator = MarketDataValidator()
        self.planner = planner
        self.config = config
        self.feature_engine = feature_engine
        self.event_store = event_store
        self.runtime_state_store = runtime_state_store
        self.trade_journal = trade_journal
        self.market_journal = market_journal
        self.candle_journal = candle_journal
        self.heartbeat = heartbeat
        self.research_pipeline = research_pipeline

        self.asset_class_by_symbol = {
            symbol: instrument_registry.resolve(symbol).asset_class
            for symbol in self.symbols
        }
        self.context_asset_classes: dict[str, object] = {}
        active_asset_classes = set(self.asset_class_by_symbol.values())
        for asset_class, benchmarks in settings.benchmark_symbols_by_asset_class().items():
            if asset_class not in active_asset_classes:
                continue
            for benchmark in benchmarks:
                self.context_asset_classes[benchmark.strip().upper()] = asset_class
        self.monitored_symbols = list(
            dict.fromkeys([*self.symbols, *self.context_asset_classes.keys()])
        )

        self.coordinator = MarketDataCoordinator(
            websocket_required=live_market_data.requires_websocket_health,
            symbol_silence_seconds=WS_POSITION_SILENCE_SECONDS,
        )
        self.windows = V3DecisionWindowCoordinator()
        self.forager = NoVolumeForager()
        self.intent_book = RestingIntentBook()
        self.book = InventoryBook.from_events(event_store.events())
        self.mutation_runner = BrokerTaskRunner()
        self.maintenance_runner = BrokerTaskRunner()
        self.executor = V3BrokerExecutor(
            broker=execution_broker,
            task_runner=self.mutation_runner,
            event_store=event_store,
            book=self.book,
            runtime_state_store=runtime_state_store,
            strategy_version=config.strategy.name,
            model_version=(
                planner.recoverability_scorer.artifact.model_version
                if getattr(planner, "recoverability_scorer", None) is not None
                else None
            ),
        )

        self.latest_snapshots: dict[str, MarketSnapshot] = {}
        self._last_session_snapshots: dict[str, MarketSnapshot] = {}
        self.latest_features: dict[str, OnlineFeatureSnapshot] = {}
        self.session_decisions: dict[str, object] = {}

        self.loop_id = 0
        self._current_run_equity: float | None = None
        self._exit_planning_equity: float | None = None
        self._equity_observed_at: datetime | None = None
        self._equity_source: str | None = None
        self._started = False
        self._stop_requested = False
        self.stop_reason: str | None = None
        self._last_fallback_monotonic = 0.0
        self._last_close_confirmation_monotonic = 0.0
        self._last_equity_monotonic = 0.0
        self._last_research_boundary: datetime | None = None

        self._market_quality_signature: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        self._maintenance_errors: set[str] = set()
        self._last_logged_decision_signature: dict[str, tuple[object, ...]] = {}
        self._decision_reason_counts: Counter[str] = Counter()

        self.metrics = {
            "market_snapshots": 0,
            "candles_closed": 0,
            "decision_windows": 0,
            "decision_windows_incomplete": 0,
            "decision_quotes_outside_bucket": 0,
            "intents_planned": 0,
            "orders_submitted": 0,
            "market_data_rejected": 0,
            "errors": 0,
            "equity_refresh_attempts": 0,
            "equity_refresh_failures": 0,
            "equity_consecutive_failures": 0,
            "equity_refresh_recoveries": 0,
            "equity_last_success_at": None,
            "candle_session_rejections": 0,
            "session_transitions": 0,
        }

    def startup(self, *, now: datetime | None = None) -> None:
        if self._started:
            return

        actual_now = _utc(now or datetime.now(UTC))
        events = self.event_store.events()
        self.book = InventoryBook.from_events(events)
        restored_inventories = self.runtime_state_store.restore_inventory_book(self.book)
        restored_features = self.runtime_state_store.restore_feature_engine(
            self.feature_engine
        )
        self._restore_exit_planning_equity_reference()
        self.executor.book = self.book
        self.executor.restore_pending_close_confirmations(events)

        restart_safety = evaluate_restart_safety(events)
        if not restart_safety.safe:
            self.executor.halted_reason = (
                restart_safety.reason or "unresolved_broker_mutation_at_restart"
            )

        active_inventories = self._active_inventories()
        if active_inventories:
            restored_feature_set = set(restored_features)
            missing_feature_symbols = {
                inventory.symbol
                for inventory in active_inventories
                if inventory.symbol not in restored_feature_set
            }
            if missing_feature_symbols:
                self.executor.halted_reason = (
                    "missing_causal_feature_state_for_open_inventory:"
                    + ",".join(sorted(missing_feature_symbols))
                )

        try:
            broker_reconciliation_issues = self.executor.verify_known_broker_legs()
        except Exception as exc:
            self.trade_journal.write(
                "v3_startup_rejected",
                {
                    "reason": "broker_leg_verification_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            raise RuntimeError("V3 broker-leg verification failed at startup") from exc
        if broker_reconciliation_issues:
            self.trade_journal.write(
                "v3_startup_rejected",
                {
                    "reason": "broker_leg_reconciliation_failed",
                    "issues": list(broker_reconciliation_issues),
                },
            )
            raise RuntimeError(
                "V3 persisted inventory does not match broker units; manual "
                "reconciliation is required."
            )

        self.coordinator.initialize_symbols(self.monitored_symbols, now=actual_now)
        self._refresh_sessions(actual_now)
        self._maybe_schedule_equity_refresh(0.0, force=True)
        self.live_market_data.start(self.monitored_symbols)
        self._started = True

        self.trade_journal.write(
            "v3_runtime_started",
            {
                "runtime_contract_version": V3_RUNTIME_CONTRACT_VERSION,
                "strategy": self.config.strategy.name,
                "recoverability_authority_enabled": bool(
                    self.config.recoverability.enabled
                ),
                "hedge_execution_enabled": False,
                "restored_inventory_state_count": len(restored_inventories),
                "restored_feature_state_count": len(restored_features),
                "active_inventory_count": len(active_inventories),
                **self._risk_authority_snapshot(),
                "restart_safety": restart_safety.safe,
                "restart_reason": restart_safety.reason,
                "unresolved_restart_action_ids": list(
                    restart_safety.unresolved_action_ids
                ),
                "decision_quote_provenance": "last_complete_quote_in_closed_bucket",
                "log_policy": {
                    "raw_market": "sampled_10s_per_symbol",
                    "raw_candles": "one_event_per_finalized_candle",
                    "silent_decisions": "heartbeat_aggregates_only",
                    "decision_details": "material_state_changes_only",
                    "market_quality": "transition_only",
                    "causal_state": "run_checkpoints_plus_sqlite_restart_cache",
                },
            },
        )

    def run(self, *, timeout_seconds: float = 1.0) -> None:
        self.stop_reason = None
        try:
            if not self._started:
                self.startup()
            while not self._stop_requested:
                self.loop_id += 1
                now = datetime.now(UTC)
                monotonic_now = time.monotonic()

                self._refresh_sessions(now)
                self._drain_broker_tasks()
                self._maybe_schedule_equity_refresh(monotonic_now)
                self._run_position_fallback_if_due(now, monotonic_now)
                self._schedule_close_confirmation_checks(monotonic_now)

                event = self.live_market_data.next_event(timeout_seconds)
                if event is not None:
                    self._handle_event(event, now)

                self._finalize_clocked_candles(now)
                self._flush_decision_windows(now)
                self._emit_due_research_state(now)
                self.heartbeat.maybe_emit(
                    journal=self.trade_journal,
                    logger=logger,
                    metrics=self._heartbeat_metrics(),
                    open_positions=len(self._active_inventories()),
                    active_symbols=len(self._expected_symbols_at(now)),
                    now=now,
                )
        except KeyboardInterrupt:
            self.stop_reason = "interrupted"
            self.trade_journal.write("v3_runtime_interrupted", {})
        except Exception:
            self.stop_reason = "error"
            raise
        finally:
            if self.stop_reason is None:
                self.stop_reason = "requested"
            try:
                self.stop()
            except Exception:
                self.stop_reason = "error"
                raise

    def stop(self) -> None:
        if self._stop_requested and not self._started:
            return
        self._stop_requested = True
        try:
            self.live_market_data.stop()
        finally:
            self.mutation_runner.close(wait=False)
            self.maintenance_runner.close(wait=False)

        self.runtime_state_store.save_feature_engine(self.feature_engine)
        self.runtime_state_store.save_inventory_book(
            self.book, asof=datetime.now(UTC)
        )
        if self.research_pipeline is not None:
            try:
                self.research_pipeline.flush()
            except Exception:
                self.metrics["errors"] += 1
                logger.exception("V3 research flush failed")

        self.trade_journal.write(
            "v3_runtime_stopped",
            self._heartbeat_metrics(),
        )
        self._started = False

    def _handle_event(self, event: MarketDataEvent, now: datetime) -> None:
        symbol = event.symbol.strip().upper()
        precheck = self.coordinator.precheck(event)
        if not precheck.accepted:
            return

        snapshot = event.snapshot
        if snapshot is None:
            return

        validation = self.market_data_validator.validate(
            snapshot,
            self._market_data_quality_config(symbol),
            now=now,
        )
        if validation.status != MarketDataStatus.ACCEPTED:
            self.metrics["market_data_rejected"] += 1
            self._record_market_quality_transition(
                symbol=symbol,
                source=event.source,
                status=validation.status,
                reasons=tuple(validation.reasons),
            )
            return

        self._record_market_quality_recovery(symbol=symbol, source=event.source)
        self.coordinator.mark_accepted(event)
        self.latest_snapshots[symbol] = snapshot
        self.market_context_service.observe_accepted_snapshot(snapshot)
        self.fallback_validator.observe_accepted(snapshot)

        if event.price_changed:
            self.metrics["market_snapshots"] += 1
            self.market_journal.write(
                "market_price_changed",
                {
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "source": event.source.value,
                    "message_id": event.message_id,
                    "connection_id": event.connection_id,
                    "loop_id": self.loop_id,
                },
            )

        if self.research_pipeline is not None:
            self.research_pipeline.observe_accepted_snapshot(
                snapshot,
                source=event.source,
            )

        if symbol not in self.asset_class_by_symbol:
            return

        session = self.session_decisions.get(symbol)
        session_allows_new_risk = (
            session is not None
            and session_timestamp_rejection_reason(
                decision=session,
                timestamp=snapshot.timestamp,
            )
            is None
        )

        for intent in self.intent_book.triggered(snapshot):
            if not intent.reduce_only and not (
                session_allows_new_risk
                and self._operational_entry_allowed(symbol)
            ):
                continue
            if self.executor.schedule(intent, snapshot=snapshot):
                self.intent_book.mark_dispatched(intent.intent_id)
                self.metrics["orders_submitted"] += 1
                self.trade_journal.write(
                    "v3_intent_triggered",
                    {
                        "intent_id": intent.intent_id,
                        "purpose": intent.purpose.value,
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "reduce_only": intent.reduce_only,
                        "source": "fresh_quote",
                    },
                )

        if not session_allows_new_risk:
            return

        self._last_session_snapshots[symbol] = snapshot
        builder = self.candle_builders[symbol]
        builder.prepare_event(event)
        builder.on_snapshot(snapshot)
        result = builder.take_last_closed_result()
        if result is not None:
            self._process_closed_candle(
                symbol,
                result,
                now,
                source="event",
            )

    def _session_at(self, symbol: str, timestamp: datetime):
        return self.trading_session_service.evaluate(
            asset_class=self.asset_class_by_symbol[symbol], now=timestamp,
        )

    def _finalize_symbol_candles(self, symbol: str, now: datetime) -> None:
        snapshot = self._last_session_snapshots.get(symbol)
        if snapshot is None:
            return
        session = self._session_at(symbol, snapshot.timestamp)
        if session_timestamp_rejection_reason(decision=session, timestamp=snapshot.timestamp):
            return
        until = _utc(now)
        if not session.session_24_7:
            until = min(until, _utc(session.session_end_time) + timedelta(
                seconds=CANDLE_CLOCK_GRACE_SECONDS,
            ))
        for result in self.candle_builders[symbol].finalize_until(
            until,
            grace_seconds=CANDLE_CLOCK_GRACE_SECONDS,
            max_carry_forward_age_seconds=CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS,
        ):
            self._process_closed_candle(symbol, result, now, source="clock")

    def _finalize_clocked_candles(self, now: datetime) -> None:
        for symbol in self.candle_builders:
            self._finalize_symbol_candles(symbol, now)

    def _process_closed_candle(
        self,
        symbol,
        result,
        now: datetime,
        *,
        source: str,
    ) -> None:
        candle = replace(
            result.candle,
            carried_forward=result.quality.carried_forward,
            source_price_age_seconds=result.quality.last_price_age_seconds,
            quality_degraded=result.quality.degraded,
        )
        session = self._session_at(symbol, candle.opened_at)
        invalid_session = session_timestamp_rejection_reason(
            decision=session, timestamp=candle.opened_at,
        )
        if (invalid_session is not None or
                (not session.session_24_7 and session.session_end_time is not None
                 and _utc(candle.closed_at) > _utc(session.session_end_time))):
            self.metrics["candle_session_rejections"] += 1
            self._record_maintenance_error(
                f"candle_session:{symbol}", "v3_candle_session_rejected",
                {"symbol": symbol, "opened_at": candle.opened_at,
                 "reason": invalid_session or "candle_crosses_session_end"},
            )
            return
        self._clear_maintenance_error(
            f"candle_session:{symbol}", "v3_candle_session_recovered", {"symbol": symbol},
        )
        self.multi_timeframe_service.on_base_candle(
            symbol=symbol,
            candle=candle,
            session_decision=session,
        )
        feature = self.feature_engine.update(candle)
        self.latest_features[symbol] = feature
        self.book.observe_candle(
            symbol=symbol,
            high=candle.high,
            low=candle.low,
            close=candle.close,
        )

        decision_snapshot, quote_in_bucket = _decision_quote_for_candle(
            result=result,
            latest_snapshot=self.latest_snapshots.get(symbol),
            candle=candle,
        )
        if decision_snapshot is not None and not quote_in_bucket:
            self.metrics["decision_quotes_outside_bucket"] += 1

        quality_ok = bool(
            decision_snapshot is not None
            and quote_in_bucket
            and not candle.quality_degraded
        )
        entry_allowed = bool(
            quality_ok and self._operational_entry_allowed(symbol)
        )
        expected = self._expected_symbols_at(candle.opened_at)
        recorded = False
        if decision_snapshot is not None:
            recorded = self.windows.record(
                feature=feature,
                snapshot=decision_snapshot,
                quality_ok=quality_ok,
                expected_symbols=expected,
            )
        else:
            self._decision_reason_counts[
                DecisionReason.MARKET_DATA_INVALID.value
            ] += 1
            self._retain_reduce_only({symbol})

        self.metrics["candles_closed"] += 1

        self.candle_journal.write(
            "candle_finalized",
            {
                "symbol": symbol,
                "candle": candle,
                "quality": result.quality,
                "entry_allowed": entry_allowed,
                "feed_state": self.coordinator.state_for(symbol).value,
                "finalization_source": source,
                "finalized_at": _utc(now),
                "loop_id": self.loop_id,
                "v3_window_recorded": recorded,
                "decision_quote_available": decision_snapshot is not None,
                "decision_quote_in_bucket": quote_in_bucket,
                "decision_quote_timestamp": (
                    None
                    if decision_snapshot is None
                    else decision_snapshot.timestamp
                ),
            },
        )
        if decision_snapshot is not None and not recorded:
            self.trade_journal.write(
                "v3_decision_window_late_symbol",
                {
                    "symbol": symbol,
                    "closed_at": candle.closed_at,
                    "finalization_source": source,
                },
            )

    def _flush_decision_windows(self, now: datetime) -> None:
        for batch in self.windows.pop_ready(now=now):
            self._process_decision_window(batch)

    def _process_decision_window(self, batch: V3DecisionWindowBatch) -> None:
        self.runtime_state_store.save_feature_engine(
            self.feature_engine,
            symbols=batch.completed_symbols,
        )
        self.runtime_state_store.save_inventory_book(
            self.book,
            asof=batch.closed_at,
        )

        equity_reference = self._current_run_equity or self._exit_planning_equity
        if equity_reference is None:
            self._retain_reduce_only(self.symbols)
            self._record_maintenance_error(
                "equity_unavailable",
                "v3_decision_window_blocked",
                {
                    "reason": "reduce_only_planning_blocked_no_equity_reference",
                    "closed_at": batch.closed_at,
                },
            )
            return
        self._clear_maintenance_error(
            "equity_unavailable",
            "v3_account_equity_recovered",
            {},
        )

        self.metrics["decision_windows"] += 1
        portfolio = self.book.portfolio(equity=equity_reference)
        free_slots = max(
            0,
            self.config.risk.max_inventories - portfolio.active_inventory_count,
        )

        market_states: dict[str, MarketState] = {}
        for symbol, feature in batch.features.items():
            snapshot = batch.snapshots.get(symbol)
            if snapshot is None:
                self._retain_reduce_only({symbol})
                continue
            quality_ok = bool(batch.quality.get(symbol, False))
            market_states[symbol] = feature.market_state(
                snapshot=snapshot,
                entry_allowed=(
                    quality_ok and self._operational_entry_allowed(symbol)
                ),
                quality_ok=quality_ok,
            )

        for inventory in portfolio.inventories:
            market = market_states.get(inventory.symbol)
            if market is None or not market.quality_ok:
                self._retain_reduce_only({inventory.symbol})
                self._decision_reason_counts[
                    DecisionReason.MARKET_DATA_INVALID.value
                ] += 1
                continue

            decision = self.planner.plan_existing_inventory(
                market=market,
                portfolio=portfolio,
                allow_new_risk=market.entry_allowed,
            )
            intents = tuple(
                intent
                for intent in decision.intents
                if intent.reduce_only or market.entry_allowed
            )
            self.intent_book.replace_symbol(inventory.symbol, intents)
            self.metrics["intents_planned"] += len(intents)
            self._journal_decision(
                inventory.symbol,
                decision,
                intents,
                extra={"role": "active_inventory"},
            )

        candidates: list[ForagerCandidate] = []
        if free_slots > 0 and self._current_run_equity is not None:
            for symbol, market in market_states.items():
                if self.book.active_for_symbol(symbol) is not None:
                    continue
                feature = batch.features[symbol]
                candidates.append(
                    ForagerCandidate(
                        market=market,
                        forager_volatility=feature.forager_volatility,
                        ema_readiness=feature.ema_readiness,
                    )
                )

        ranked_candidates = self.forager.rank(candidates, limit=free_slots)
        selected_flat = {item.market.symbol for item in ranked_candidates}
        for ranked in ranked_candidates:
            decision = self.planner.plan_symbol(
                market=ranked.market,
                portfolio=portfolio,
            )
            intents = tuple(
                intent
                for intent in decision.intents
                if intent.reduce_only or ranked.market.entry_allowed
            )
            self.intent_book.replace_symbol(ranked.market.symbol, intents)
            self.metrics["intents_planned"] += len(intents)
            self._journal_decision(
                ranked.market.symbol,
                decision,
                intents,
                extra={
                    "role": "flat_forager",
                    "forager_score": ranked.score,
                },
            )

        for symbol in market_states:
            if (
                self.book.active_for_symbol(symbol) is None
                and symbol not in selected_flat
            ):
                self.intent_book.replace_symbol(symbol, ())
                self._clear_logged_decision(symbol)

        if batch.missing_symbols or batch.finalization_reason != "all_symbols_completed":
            self.metrics["decision_windows_incomplete"] += 1
            self.trade_journal.write(
                "v3_decision_window_incomplete",
                {
                    "closed_at": batch.closed_at,
                    "expected_count": len(batch.expected_symbols),
                    "completed_count": len(batch.completed_symbols),
                    "missing_symbols": list(batch.missing_symbols),
                    "finalization_reason": batch.finalization_reason,
                },
            )

    def _maybe_schedule_equity_refresh(
        self,
        monotonic_now: float,
        *,
        force: bool = False,
    ) -> None:
        if not force and monotonic_now - self._last_equity_monotonic < 30.0:
            return
        if self.maintenance_runner.has_pending_kind("v3_account_equity"):
            return
        self.maintenance_runner.submit(
            kind="v3_account_equity",
            operation=self.execution_broker.get_account_equity,
        )
        self._last_equity_monotonic = monotonic_now
        self.metrics["equity_refresh_attempts"] += 1

    def _run_position_fallback_if_due(
        self,
        now: datetime,
        monotonic_now: float,
    ) -> None:
        if (
            monotonic_now - self._last_fallback_monotonic
            < POSITION_FALLBACK_INTERVAL_SECONDS
        ):
            return
        symbols = [inventory.symbol for inventory in self._active_inventories()]
        if not symbols:
            return
        fallback = self.coordinator.position_fallback_symbols(
            symbols=symbols,
            now=now,
        )
        if (
            not fallback
            or self.maintenance_runner.has_pending_kind("v3_position_fallback")
        ):
            return
        self.maintenance_runner.submit(
            kind="v3_position_fallback",
            operation=lambda symbols=tuple(fallback): (
                self.rest_market_data.get_market_snapshots(list(symbols))
            ),
            context={"symbols": list(fallback)},
            lane=BrokerTaskLane.STANDARD,
        )
        self._last_fallback_monotonic = monotonic_now

    def _schedule_close_confirmation_checks(self, monotonic_now: float) -> None:
        if monotonic_now - self._last_close_confirmation_monotonic < 1.0:
            return
        self._last_close_confirmation_monotonic = monotonic_now
        self.executor.schedule_close_confirmation_checks(
            monotonic_now=monotonic_now,
        )

    def _drain_broker_tasks(self) -> None:
        resolved = self.executor.drain()
        if resolved:
            for intent_id in resolved:
                self.intent_book.resolve(intent_id)
            self.runtime_state_store.save_inventory_book(
                self.book,
                asof=datetime.now(UTC),
            )

        for completion in self.maintenance_runner.drain():
            if completion.kind == "v3_account_equity":
                equity = completion.value
                if (completion.error is None and not isinstance(equity, bool)
                        and isinstance(equity, (int, float))
                        and math.isfinite(equity) and equity > 0):
                    now = datetime.now(UTC)
                    source = ("paper_broker" if self.settings.broker == "paper"
                              else ACCOUNT_EQUITY_SOURCE)
                    self.runtime_state_store.save_broker_equity(
                        value=equity, observed_at=now, source=source,
                    )
                    self._current_run_equity = float(equity)
                    self._exit_planning_equity = float(equity)
                    self._equity_observed_at, self._equity_source = now, source
                    if self.metrics["equity_consecutive_failures"]:
                        self.metrics["equity_refresh_recoveries"] += 1
                    self.metrics["equity_consecutive_failures"] = 0
                    self.metrics["equity_last_success_at"] = now
                    self.trade_journal.write("v3_account_equity_snapshot", {
                        "equity": equity, "source": source, "observed_at": now,
                    })
                    self._clear_maintenance_error(
                        "equity_refresh", "v3_account_equity_recovered", {"equity": equity},
                    )
                else:
                    self.metrics["equity_refresh_failures"] += 1
                    self.metrics["equity_consecutive_failures"] += 1
                    self._record_maintenance_error(
                        "equity_refresh", "v3_account_equity_error",
                        {"reason": "broker_equity_refresh_failed",
                         "error_type": (type(completion.error).__name__ if completion.error
                                        else "InvalidEquityValue")},
                    )
            elif completion.kind == "v3_position_fallback":
                self._handle_position_fallback_completion(completion)

    def _handle_position_fallback_completion(self, completion) -> None:
        symbols = list((completion.context or {}).get("symbols", []))
        if completion.error is not None:
            self.metrics["errors"] += 1
            self.coordinator.mark_fallback_failed(symbols)
            self._record_maintenance_error(
                "position_fallback",
                "v3_position_fallback_error",
                {
                    "symbols": symbols,
                    "error_type": type(completion.error).__name__,
                    "message": str(completion.error),
                },
            )
            return

        snapshots = completion.value or {}
        recovered: list[str] = []
        for symbol in symbols:
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                continue
            validation = self.fallback_validator.validate(
                snapshot,
                self._market_data_quality_config(symbol),
                now=datetime.now(UTC),
            )
            if validation.status != MarketDataStatus.ACCEPTED:
                continue
            recovered.append(symbol)
            for intent in self.intent_book.triggered(snapshot):
                if not intent.reduce_only:
                    continue
                if self.executor.schedule(intent, snapshot=snapshot):
                    self.intent_book.mark_dispatched(intent.intent_id)
                    self.metrics["orders_submitted"] += 1
                    self.trade_journal.write(
                        "v3_intent_triggered",
                        {
                            "intent_id": intent.intent_id,
                            "purpose": intent.purpose.value,
                            "symbol": intent.symbol,
                            "side": intent.side,
                            "reduce_only": True,
                            "source": "rest_fallback",
                        },
                    )

        if recovered:
            self.coordinator.mark_fallback_succeeded(recovered)
            self._clear_maintenance_error(
                "position_fallback",
                "v3_position_fallback_recovered",
                {"symbols": recovered},
            )

    def _restore_exit_planning_equity_reference(self) -> None:
        reference = self.runtime_state_store.load_broker_equity()
        if reference is not None:
            self._exit_planning_equity = reference.value
            self._equity_observed_at = reference.observed_at
            self._equity_source = reference.source

    def _refresh_sessions(self, now: datetime) -> None:
        decisions = {symbol: self._session_at(symbol, now) for symbol in self.symbols}
        transitions = []
        ended_keys = set()
        for symbol, decision in decisions.items():
            previous = self.session_decisions.get(symbol)
            old_key = previous.session_key if previous is not None else None
            if old_key != decision.session_key:
                # Flush the old session's final M1 before resetting its builder.
                self._finalize_symbol_candles(
                    symbol, _utc(now) + timedelta(seconds=CANDLE_CLOCK_GRACE_SECONDS),
                )
                transitions.append(symbol)
                if old_key is not None:
                    ended_keys.add(old_key)
        if transitions:
            self._flush_decision_windows(
                _utc(now) + timedelta(seconds=DECISION_WINDOW_GRACE_SECONDS),
            )
        for symbol in transitions:
            self.candle_builders[symbol].reset()
            self.multi_timeframe_service.reset_symbol(symbol, clear_history=False)
            self.market_data_validator.reset_symbol(symbol)
            self.fallback_validator.reset_symbol(symbol)
            self.coordinator.reset_symbol(symbol, now=now)
            self.windows.reset_symbol(symbol)
            if self.research_pipeline is not None:
                self.research_pipeline.reset_symbol(symbol)
            self.latest_snapshots.pop(symbol, None)
            self.latest_features.pop(symbol, None)
            self._last_session_snapshots.pop(symbol, None)
            self._retain_reduce_only({symbol})
            self._clear_logged_decision(symbol)
            self.metrics["session_transitions"] += 1
        for session_key in ended_keys:
            self.market_context_service.reset_session(session_key)
        for symbol, decision in decisions.items():
            previous = self.session_decisions.get(symbol)
            self.session_decisions[symbol] = decision
            if previous is None or previous.state_signature() != decision.state_signature():
                self.trade_journal.write("v3_session_state", {
                    "symbol": symbol,
                    "session_key": decision.session_key,
                    "source_reason": decision.reason,
                    **self._symbol_risk_authority(symbol),
                    "v3_force_close_at_session_end": False,
                })

    def _expected_symbols_at(self, asof: datetime) -> set[str]:
        result: set[str] = set()
        for symbol in self.symbols:
            asset_class = self.asset_class_by_symbol[symbol]
            decision = self.trading_session_service.evaluate(
                asset_class=asset_class,
                now=asof,
            )
            if decision.session_active:
                result.add(symbol)
        return result

    def _operational_entry_allowed(self, symbol: str) -> bool:
        session = self.session_decisions.get(symbol)
        return bool(
            session is not None
            and getattr(session, "session_active", False)
            and getattr(session, "new_entries_allowed", False)
            and self._current_run_equity is not None
            and self.coordinator.entry_allowed(symbol)
            and self.executor.new_risk_allowed
        )

    def _symbol_risk_authority(self, symbol: str) -> dict[str, object]:
        session = self.session_decisions.get(symbol)
        checks = {
            "session_active": bool(session and session.session_active),
            "session_new_entries_allowed": bool(session and session.new_entries_allowed),
            "market_data_entry_allowed": bool(self.coordinator.entry_allowed(symbol)),
            "current_run_equity_available": self._current_run_equity is not None,
            "executor_new_risk_allowed": self.executor.new_risk_allowed,
        }
        blockers = [name for name, allowed in checks.items() if not allowed]
        if self.executor.halted_reason:
            blockers.append(self.executor.halted_reason)
        return {**checks, "v3_new_risk_allowed": all(checks.values()),
                "new_risk_blockers": blockers}

    def _risk_authority_snapshot(self) -> dict[str, object]:
        by_symbol = {symbol: self._symbol_risk_authority(symbol) for symbol in self.symbols}
        combined = any(value["v3_new_risk_allowed"] for value in by_symbol.values())
        return {
            "session_active": any(value["session_active"] for value in by_symbol.values()),
            "session_new_entries_allowed": any(value["session_new_entries_allowed"] for value in by_symbol.values()),
            "current_run_equity_available": self._current_run_equity is not None,
            "executor_new_risk_allowed": self.executor.new_risk_allowed,
            "v3_new_risk_allowed": combined,
            "new_risk_allowed": combined,
            "new_risk_blockers": {symbol: value["new_risk_blockers"] for symbol, value in by_symbol.items()},
            "risk_authority_by_symbol": by_symbol,
            "exit_planning_equity": self._exit_planning_equity,
            "equity_reference_source": self._equity_source,
            "equity_reference_observed_at": self._equity_observed_at,
            "reduce_only_planning_blocker": (
                "reduce_only_planning_blocked_no_equity_reference"
                if self._exit_planning_equity is None else None
            ),
        }

    def _active_inventories(self):
        return tuple(
            inventory
            for inventory in self.book.inventories
            if inventory.total_units > 0
        )

    def _retain_reduce_only(self, symbols) -> None:
        symbol_set = {str(symbol).strip().upper() for symbol in symbols}
        by_symbol: dict[str, list[OrderIntent]] = {}
        for intent in self.intent_book.snapshot():
            if intent.symbol in symbol_set and intent.reduce_only:
                by_symbol.setdefault(intent.symbol, []).append(intent)
        for symbol in symbol_set:
            self.intent_book.replace_symbol(
                symbol,
                tuple(by_symbol.get(symbol, ())),
            )

    def _journal_decision(
        self,
        symbol: str,
        decision: DecisionBatch,
        intents: tuple[OrderIntent, ...],
        *,
        extra: dict | None = None,
    ) -> None:
        reasons = tuple(item.reason for item in decision.decisions)
        for reason in reasons:
            self._decision_reason_counts[reason.value] += 1

        material = bool(intents) or any(
            reason in _MATERIAL_DECISION_REASONS for reason in reasons
        )
        if not material:
            self._clear_logged_decision(symbol)
            return

        signature = (
            tuple(reason.value for reason in reasons),
            tuple(
                (
                    intent.purpose.value,
                    intent.side,
                    round(intent.notional, 4),
                    (
                        None
                        if intent.limit_price is None
                        else round(intent.limit_price, 8)
                    ),
                    intent.reduce_only,
                )
                for intent in intents
            ),
            tuple(sorted((extra or {}).items())),
        )
        if self._last_logged_decision_signature.get(symbol) == signature:
            return
        self._last_logged_decision_signature[symbol] = signature

        self.trade_journal.write(
            "v3_inventory_decision",
            {
                "symbol": symbol,
                "decisions": [
                    {
                        "reason": item.reason.value,
                        "detail": dict(item.detail),
                        "asof": item.asof,
                    }
                    for item in decision.decisions
                ],
                "intents": [
                    {
                        "intent_id": intent.intent_id,
                        "purpose": intent.purpose.value,
                        "side": intent.side,
                        "notional": intent.notional,
                        "limit_price": intent.limit_price,
                        "reduce_only": intent.reduce_only,
                        "expected_gross_capture": intent.expected_gross_capture,
                        "estimated_cost": (
                            intent.cost_estimate.total
                            if intent.cost_estimate is not None
                            else None
                        ),
                    }
                    for intent in intents
                ],
                **(extra or {}),
            },
        )

    def _clear_logged_decision(self, symbol: str) -> None:
        if self._last_logged_decision_signature.pop(symbol, None) is None:
            return
        self.trade_journal.write(
            "v3_inventory_decision_cleared",
            {"symbol": symbol},
        )

    def _record_market_quality_transition(
        self,
        *,
        symbol: str,
        source: MarketDataSource,
        status: MarketDataStatus,
        reasons: tuple[str, ...],
    ) -> None:
        signature = (source.value, status.value, reasons)
        if self._market_quality_signature.get(symbol) == signature:
            return
        self._market_quality_signature[symbol] = signature
        self.trade_journal.write(
            "v3_market_data_quality_changed",
            {
                "symbol": symbol,
                "source": source.value,
                "status": status.value,
                "reasons": list(reasons),
            },
        )

    def _record_market_quality_recovery(
        self,
        *,
        symbol: str,
        source: MarketDataSource,
    ) -> None:
        previous = self._market_quality_signature.pop(symbol, None)
        if previous is None:
            return
        self.trade_journal.write(
            "v3_market_data_quality_recovered",
            {
                "symbol": symbol,
                "source": source.value,
                "previous_status": previous[1],
                "previous_reasons": list(previous[2]),
            },
        )

    def _record_maintenance_error(
        self,
        key: str,
        event_type: str,
        payload: dict,
    ) -> None:
        if key in self._maintenance_errors:
            return
        self._maintenance_errors.add(key)
        self.trade_journal.write(event_type, payload)

    def _clear_maintenance_error(
        self,
        key: str,
        event_type: str,
        payload: dict,
    ) -> None:
        if key not in self._maintenance_errors:
            return
        self._maintenance_errors.discard(key)
        self.trade_journal.write(event_type, payload)

    def _journal_budget_metrics(self) -> dict[str, object]:
        return {
            "trades": self.trade_journal.trade_journal.budget_metrics(),
            "market": {
                **self.market_journal.journal.budget_metrics(),
                "sampled_out_count": self.market_journal.sampled_out_count,
                "budget_suppressed_count": self.market_journal.budget_suppressed_count,
                "budget_exhausted": self.market_journal.budget_exhausted,
            },
            "candles": {
                **self.candle_journal.journal.budget_metrics(),
                "sampled_out_count": self.candle_journal.sampled_out_count,
                "budget_suppressed_count": self.candle_journal.budget_suppressed_count,
                "budget_exhausted": self.candle_journal.budget_exhausted,
            },
        }

    def _heartbeat_metrics(self) -> dict[str, object]:
        return {
            **self.metrics,
            "decision_reason_counts": dict(
                sorted(self._decision_reason_counts.items())
            ),
            "market_data_coordinator": dict(self.coordinator.metrics),
            "broker_confirmation": self.executor.confirmation_metrics(),
            "journal_budget": self._journal_budget_metrics(),
            "stop_reason": self.stop_reason,
            **self._risk_authority_snapshot(),
            "pending_close_confirmations": self.executor.pending_close_confirmation_snapshot(),
            "risk_halt_reason": self.executor.halted_reason,
        }

    def _market_data_quality_config(self, symbol: str):
        if symbol in self.asset_class_by_symbol:
            return self.instrument_registry.config_for(symbol).market_data_quality
        asset_class = self.context_asset_classes[symbol]
        return self.instrument_registry.instrument_configs[
            asset_class
        ].market_data_quality

    def _emit_due_research_state(self, now: datetime) -> None:
        if self.research_pipeline is None:
            return
        cadence = self.research_pipeline.sampling_cadence_minutes
        eligible = _utc(now) - timedelta(
            seconds=CANDLE_CLOCK_GRACE_SECONDS
        )
        minute = eligible.minute - eligible.minute % cadence
        boundary = eligible.replace(
            minute=minute,
            second=0,
            microsecond=0,
        )
        if boundary == self._last_research_boundary:
            return
        self._last_research_boundary = boundary
        try:
            self.research_pipeline.emit_boundary(
                symbols=self.symbols,
                state_at=boundary,
                session_decisions=self.session_decisions,
            )
        except Exception:
            self.metrics["errors"] += 1
            logger.exception("V3 research boundary failed")


def _decision_quote_for_candle(*, result, latest_snapshot, candle):
    """Return the newest causal quote and whether it belongs to this M1 bucket.

    A rollover event may already have advanced ``latest_snapshot`` into the next
    minute. The closed candle therefore owns its explicit ``decision_snapshot``.
    Older quotes are allowed only as non-authoritative provenance for carried
    candles; future quotes are never relabelled onto the closed state.
    """
    explicit = getattr(result, "decision_snapshot", None)
    if explicit is not None:
        timestamp = _utc(explicit.timestamp)
        opened_at = _utc(candle.opened_at)
        closed_at = _utc(candle.closed_at)
        if opened_at <= timestamp < closed_at:
            return explicit, True
        if timestamp < closed_at:
            return explicit, False
        return None, False

    if latest_snapshot is None:
        return None, False
    timestamp = _utc(latest_snapshot.timestamp)
    closed_at = _utc(candle.closed_at)
    if timestamp >= closed_at:
        return None, False
    opened_at = _utc(candle.opened_at)
    return latest_snapshot, opened_at <= timestamp < closed_at


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
