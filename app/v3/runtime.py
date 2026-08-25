from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Mapping

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
from app.v3.models import DecisionBatch, InventoryStatus, MarketState, OrderIntent
from app.v3.persistence import InventoryEventStore
from app.v3.recovery import evaluate_restart_safety
from app.v3.state_store import V3RuntimeStateStore, legacy_v1_state_counts

logger = logging.getLogger(__name__)

V3_RUNTIME_CONTRACT_VERSION = "inventory_runtime_v3_1"


@dataclass
class _DecisionWindow:
    closed_at: datetime
    expected_symbols: set[str]
    feature_by_symbol: dict[str, OnlineFeatureSnapshot]
    quality_by_symbol: dict[str, bool]


@dataclass(frozen=True)
class V3DecisionWindowBatch:
    closed_at: datetime
    expected_symbols: tuple[str, ...]
    completed_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    finalization_reason: str
    features: Mapping[str, OnlineFeatureSnapshot]
    quality: Mapping[str, bool]


class V3DecisionWindowCoordinator:
    """Synchronize completed M1 states without coupling to V1 TradeCandidate."""

    def __init__(self, *, grace_seconds: float = DECISION_WINDOW_GRACE_SECONDS) -> None:
        self.grace_seconds = float(grace_seconds)
        self._windows: dict[datetime, _DecisionWindow] = {}
        self._finalized: set[datetime] = set()

    def record(
        self,
        *,
        feature: OnlineFeatureSnapshot,
        quality_ok: bool,
        expected_symbols: set[str],
    ) -> bool:
        key = _utc(feature.asof)
        if key in self._finalized:
            return False
        window = self._windows.setdefault(
            key,
            _DecisionWindow(key, set(expected_symbols), {}, {}),
        )
        window.expected_symbols.update(expected_symbols)
        window.feature_by_symbol[feature.symbol] = feature
        window.quality_by_symbol[feature.symbol] = bool(quality_ok)
        return True

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
                    quality=dict(window.quality_by_symbol),
                )
            )
        # Bounded memory: finalized timestamps older than 24h can no longer reappear.
        cutoff = actual_now - timedelta(days=1)
        self._finalized = {value for value in self._finalized if value >= cutoff}
        return tuple(result)


class GoblinV3Runtime:
    """Event-driven V3 demo/live runtime around the same pure InventoryPlanner as replay.

    V1 candidate/WAIT/managed-edge code is not invoked. Existing market-data,
    candle, research and broker primitives are reused as infrastructure only.
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
            symbol: instrument_registry.resolve(symbol).asset_class for symbol in self.symbols
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
            strategy_version=config.strategy.name,
            model_version=(
                planner.recoverability_scorer.model_version
                if getattr(planner, "recoverability_scorer", None) is not None
                else None
            ),
        )
        self.latest_snapshots: dict[str, MarketSnapshot] = {}
        self.latest_features: dict[str, OnlineFeatureSnapshot] = {}
        self.session_decisions: dict[str, object] = {}

        self._equity: float | None = None
        self._started = False
        self._stop_requested = False
        self._last_fallback_monotonic = 0.0
        self._last_close_confirmation_monotonic = 0.0
        self._last_equity_monotonic = 0.0
        self._last_research_boundary: datetime | None = None
        self.metrics = {
            "market_snapshots": 0,
            "candles_closed": 0,
            "decision_windows": 0,
            "intents_planned": 0,
            "orders_submitted": 0,
            "errors": 0,
        }

    def startup(self, *, now: datetime | None = None) -> None:
        if self._started:
            return
        actual_now = _utc(now or datetime.now(UTC))
        legacy_counts = legacy_v1_state_counts(self.settings.position_store_path)
        if any(legacy_counts.values()):
            self.trade_journal.write(
                "v3_startup_rejected",
               {
                    "reason": "legacy_v1_position_state_present",
                    "legacy_row_counts": legacy_counts,
                },
            )
            raise RuntimeError(
                "V3 refuses to start while V1 open/pending position state exists. "
                "Flatten or explicitly resolve it before starting V3; no automatic migration is performed."
            )
        events = self.event_store.events()
        self.book = InventoryBook.from_events(events)
        restored_inventories = self.runtime_state_store.restore_inventory_book(self.book)
        restored_features = self.runtime_state_store.restore_feature_engine(self.feature_engine)
        self.executor.book = self.book
        self.executor.restore_pending_closes_from_events(events)
        restart_safety = evaluate_restart_safety(events)
        reason = restart_safety.reason
        if not restart_safety.safe:
            self.executor.halt_new_risk(reason or "unresolved_broker_mutation_at_restart")

        if self.book.active_inventories:
            missing_feature_symbols = {
                inventory.symbol
                for inventory in self.book.active_inventories
                if inventory.symbol not in restored_features
            }
            if missing_feature_symbols:
                self.executor.halt_new_risk(
                    "missing_causal_feature_state_for_open_inventory:"
                    + ",".join(sorted(missing_feature_symbols))
                )

        for inventory in self.book.active_inventories:
            for leg in inventory.broker_legs:
                self.execution_broker.remember_position_instrument(leg.position_id, inventory.symbol)
                try:
                    if not self.execution_broker.is_position_open(leg.position_id):
                        self.executor.halt_new_risk(
                            f"persisted_broker_leg_missing_at_startup:{leg.position_id}"
                        )
                except Exception as exc:
                    self.executor.halt_new_risk(fžcsilaus_uege.except is view and not necessary")
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
                "recoverability_authority_enabled": bool(self.config.recoverability.enabled),
                "hedge_execution_enabled": False,
                "restored_inventory_states": list(restored_inventories),
                "restored_feature_states": list(restored_features),
                "new_risk_allowed": self.executor.new_risk_allowed,
                "restart_safety": restart_safety.safe,
                "unresolved_restart_action_ids": list(restart_safety.unresolved_action_ids),
            },
        )

    def run(self, *, timeout_seconds: float = 1.0) -> None:
        if not self._started:
            self.startup()
        try:
            while not self._stop_requested:
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
                    metrics=self.metrics,
                    open_positions=len(self.book.active_inventories),
                    active_symbols=len(self._expected_symbols_at(now)),
                    now=now,
                )
        except KeyboardInterrupt:
            self.trade_journal.write("v3_runtime_interrupted", {})
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_requested = True
        try:
            self.live_market_data.stop()
        finally:
            self.mutation_runner.close(wait=False)
            self.maintenance_runner.close(wait=False)
        self.runtime_state_store.save_feature_engine(self.feature_engine)
        self.runtime_state_store.save_inventory_book(self.book, asof=datetime.now(UTC))
        if self.research_pipeline is not None:
            try:
                self.research_pipeline.flush()
            except Exception:
                logger.exception("V3 research flush failed")
        self.trade_journal.write("v3_runtime_stopped", self.metrics)

    def _handle_event(self, event: MarketDataEvent, now: datetime) -> None:
        symbol = event.symbol.strip().upper()
        precheck = self.coordinator.precheck(event)
        if not precheck.accepted:
            return
        snapshot = event.snapshot
        if snapshot is None:
            return
        validation = self.market_data_validator.validate(
            snapshot, self._market_data_quality_config(symbol), now=now
        )
        if validation.status != MarketDataStatus.ACCEPTED:
            self.trade_journal.write(
                "v3_market_data_rejected",
                {"symbol": symbol, "reasons": list(validation.reasons), "source": event.source.value},
            )
            return
        self.coordinator.mark_accepted(event)
        self.latest_snapshots[symbol] = snapshot
        self.market_context_service.observe_accepted_snapshot(snapshot)
        if self.research_pipeline is not None:
            self.research_pipeline.observe_accepted_snapshot(snapshot, source=event.source)
        self.market_journal.write(
            "v3_market_quote_accepted",
            {
                "symbol": symbol,
                "source": event.source.value,
                "snapshot": snapshot,
                "entry_allowed": self._operational_entry_allowed(symbol) if symbol in self.asset_class_by_symbol else False,
            },
        )
        # Warm the separate REST fallback validator without forwarding it.
        self.fallback_validator.observe_accepted(snapshot)

        if symbol not in self.asset_class_by_symbol:
            return
        session = self.session_decisions.get(symbol)
        if session is None or session_timestamp_rejection_reason(decision=session, timestamp=snapshot.timestamp) is not None:
            return

        for intent in self.intent_book.triggered(snapshot):
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
                        "source": "fresh_quote",
                    },
                )

        builder = self.candle_builders[symbol]
        builder.prepare_event(event)
        builder.on_snapshot(snapshot)
        result = builder.take_last_closed_result()
        if result is not None:
            self._process_closed_candle(symbol, result, now)

    def _finalize_clocked_candles(self, now: datetime) -> None:
        for symbol, builder in self.candle_builders.items():
            session = self.session_decisions.get(symbol)
            snapshot = self.latest_snapshots.get(symbol)
            if session is None or snapshot is None:
                continue
            for result in builder.finalize_until(
                now, grace_seconds=CANDLE_CLOCK_GRACE_SECONDS, max_carry_forward_age_seconds=CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS
            ):
                self._process_closed_candle(symbol, result, now)

    def _process_closed_candle(self, symbol, result, now: datetime) -> None:
        candle = replace(
            result.candle,
            carried_forward=result.quality.carried_forward,
            source_price_age_seconds=result.quality.last_price_age_seconds,
            quality_degraded=result.quality.degraded,
        )
        session = self.session_decisions[symbol]
        self.multi_timeframe_service.on_base_candle(symbol=symbol, candle=candle, session_decision=session)
        feature = self.feature_engine.update(candle)
        self.latest_features[symbol] = feature
        self.book.observe_candle(symbol=symbol, high=candle.high, low=candle.low, close=candle.close)
        # Persist the trailing/feature state BEFORE any new broker mutation can be planned.
        self.runtime_state_store.save_feature_engine(self.feature_engine)
        self.runtime_state_store.save_inventory_book(self.book, asof=candle.closed_at)
        expected = self._expected_symbols_at(candle.closed_at)
        recorded = self.windows.record(feature=feature, quality_ok=not candle.quality_degraded, expected_symbols=expected)
        if recorded:
            self.metrics["candles_closed"] += 1
        self.candle_journal.write(
            "v3_candle_finalized",
          {"symbol": symbol, "candle": candle, "quality": result.quality, "recorded_in_window": recorded},
         )

    def _flush_decision_windows(self, now: datetime) -> None:
        for batch in self.windows.pop_ready(now=now):
            self._process_decision_window(batch)

    def _process_decision_window(self, batch: V3DecisionWindowBatch) -> None:
        if self._equity is None:
            self.trade_journal.write(
                "v3_decision_window_blocked",
                {"reason": "account_equity_unavailable", "closed_at": batch.closed_at},
            )
            return
        self.metrics["decision_windows"] += 1
        portfolio = self.book.portfolio(equity=self._equity)
        symbol_count = len(portfolio.inventories)
        free_slots = max(0, self.config.risk.max_inventories - symbol_count)

        market_states: dict[str, MarketState] = {}
        for symbol, feature in batch.features.items():
            snapshot = self.latest_snapshots.get(symbol)
            if snapshot is None:
                self._retain_reduce_only_for_missing({symbol})
                continue
            entry_allowed = self._operational_entry_allowed(symbol) and batch.quality.get(symbol, False)
            market = feature.market_state(snapshot=snapshot, entry_allowed=entry_allowed, quality_ok=batch.quality.get(symbol, False))
            market_states[symbol] = market

        # First re-evaluate every existing inventory. It reserves portfolio authority and must
        # NEVER be suppressed by the Forager/capacity choice.
        for inventory in portfolio.inventories:
            market = market_states.get(inventory.symbol)
            if market is None:
                self._retain_reduce_only_for_missing({inventory.symbol})
                continue
            decision = self.planner.plan_symbol(market=market, portfolio-portfolio)
            self.intent_book.replace_symbol(inventory.symbol, decision.intents)
            self.metrics["intents_planned"] += len(decision.intents)
            self._journal_decision(inventory.symbol, decision, decision.intents, extra={"role": "active_inventory"})

        if free_slots > 0:
            candidates = []
            for symbol, market in market_states.items():
                if self.book.active_for_symbol(symbol) is not None:
                    continue
                feature = batch.features[symbol]
                candidates.append(
                    ForagerCandidate(
                        market=market,
                        forager_volatility=feature.forager_volatility,
                        ema_readiness=feature.ema_readiness,
                        activity=feature.activity,
                    )
                )
            for ranked in self.forager.rank(candidates, limit=free_slots):
                decision = self.planner.plan_symbol(market=ranked.market, portfolio=portfolio)
                self.intent_book.replace_symbol(ranked.market.symbol, decision.intents)
                self.metrics["intents_planned"] += len(decision.intents)
                self._journal_decision(
                    ranked.market.symbol,
                    decision,
                    decision.intents,
                    extra={"role": "flat_forager", "forager_score": ranked.score},
                )

        # Flat symbols not selected in this window must not keep stale entry authority.
        selected_flat = {item.symbol for item in self.forager.rank([], limit=0)}
        for symbol in market_states:
            if self.book.active_for_symbol(symbol) is None and symbol not in selected_flat:
                # replace_symbol is intentionally called for all flat non-selected symbols.
                if not any(intent.symbol == symbol for intent in self.intent_book.snapshot()):
                    self.intent_book.replace_symbol(symbol, ())

        self.trade_journal.write(
            "v3_decision_window_finalized",
            {
                "closed_at": batch.closed_at,
                "expected_symbols": list(batch.expected_symbols),
                "completed_symbols": list(batch.completed_symbols),
                "missing_symbols": list(batch.missing_symbols),
                "finalization_reason": batch.finalization_reason,
            },
        )

    def _maybe_schedule_equity_refresh(self, monotonic_now: float, *, force: bool = False) -> None:
        if not force and monotonic_now - self._last_equity_monotonic < 30.0:
            return
        if self.maintenance_runner.has_pending_kind("v3_account_equity"):
            return
        self.maintenance_runner.submit(
            kind="v3_account_equity",
            operation=self.execution_broker.get_account_equity,
        )
        self._last_equity_monotonic = monotonic_now

    def _run_position_fallback_if_due(self, now: datetime, monotonic_now: float) -> None:
        if monotonic_now - self._last_fallback_monotonic < POSITION_FALLBACK_INTERVAL_SECONDS:
            return
        symbols = [inventory.symbol for inventory in self.book.active_inventories]
        if not symbols:
            return
        fallback = self.coordinator.position_fallback_symbols(symbols=symbols, now=now)
        if not fallback or self.maintenance_runner.has_pending_kind("v3_position_fallback"):
            return
        self.maintenance_runner.submit(
            kind="v3_position_fallback",
            operation=lambda symbols=tuple(fallback): self.rest_market_data.get_market_snapshots(list(symbols)),
            context={"symbols": list(fallback)},
            lane=BrokerTaskLane.STANDARD,
        )
        self._last_fallback_monotonic = monotonic_now

    def _schedule_close_confirmation_checks(self, monotonic_now: float) -> None:
        if monotonic_now - self._last_close_confirmation_monotonic < 10.0:
            return
        self._last_close_confirmation_monotonic = monotonic_now
        self.executor.schedule_close_confirmations()

    def _drain_broker_tasks(self) -> None:
        resolved = self.executor.drain()
        if resolved:
            # Fills reset trailing inside InventoryBook. Persist that reset
            # immediately so a crash cannot restore old extrema.
            self.runtime_state_store.save_inventory_book(self.book, asof=datetime.now(UTC))
        for completion in self.maintenance_runner.drain():
            if completion.kind == "v3_account_equity":
                if completion.error is None:
                    equity = float(completion.value)
                    if math.isfinite(equity) and equity > 0:
                        self._equity = equity
                else:
                    self.metrics["errors"] += 1
                    self.trade_journal.write(
                        "v3_account_equity_error", {"error": str(completion.error)}
                    )
            elif completion.kind == "v3_position_fallback":
                self._handle_position_fallback_completion(completion)

    def _handle_position_fallback_completion(self, completion) -> None:
        symbols = list((completion.context or {}).get("symbols", []))
        if completion.error is not None:
            self.coordinator.mark_fallback_failed(symbols)
            self.trade_journal.write(
                "v3_position_fallback_error",
                {"symbols": symbols, "error": str(completion.error)},
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
            # REST fallback is position-management only: never open/reenter risk.
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
                            "source": "rest_fallback",
                        },
                    )
        if recovered:
            self.coordinator.mark_fallback_succeeded(recovered)

    def _refresh_sessions(self, now: datetime) -> None:
        for symbol in self.symbols:
            asset_class = self.asset_class_by_symbol[symbol]
            decision = self.trading_session_service.evaluate(asset_class=asset_class, now=now)
            previous = self.session_decisions.get(symbol)
            self.session_decisions[symbol] = decision
            if previous != decision:
                # V3 uses session_active only for risk authority. It deliberately
                # ignores V1's last-hour force-close/new-entry cutoff.
                self.trade_journal.write(
                    "v3_session_state",
                    {
                        "symbol": symbol,
                        "session_active": decision.session_active,
                        "session_key": decision.session_key,
                        "source_reason": decision.reason,
                        "v3_new_risk_allowed": bool(decision.session_active),
                        "v3_force_close_at_session_end": False,
                    },
                )

    def _expected_symbols_at(self, asof: datetime) -> set[str]:
        result: set[str] = set()
        for symbol in self.symbols:
            asset_class = self.asset_class_by_symbol[symbol]
            decision = self.trading_session_service.evaluate(asset_class=asset_class, now=asof)
            if decision.session_active:
                result.add(symbol)
        return result

    def _operational_entry_allowed(self, symbol: str) -> bool:
        session = self.session_decisions.get(symbol)
        return bool(
            session is not None
            and getattr(session, "session_active", False)
            and self.coordinator.entry_allowed(symbol)
        )

    def _retain_reduce_only_for_missing(self, symbols) -> None:
        by_symbol: dict[str, list[OrderIntent]] = {}
        for intent in self.intent_book.snapshot():
            if intent.symbol in symbols and intent.reduce_only:
                by_symbol.setdefault(intent.symbol, []).append(intent)
        for symbol in symbols:
            self.intent_book.replace_symbol(symbol, tuple(by_symbol.get(symbol, ())))

    def _journal_decision(
        self,
        symbol: str,
        decision: DecisionBatch,
        intents: tuple[OrderIntent, ...],
        *,
        extra: dict | None = None,
    ) -> None:
        self.trade_journal.write(
            "v3_inventory_decision",
            {
                "symbol": symbol,
                "decisions": [
                    {"reason": item.reason.value, "detail": dict(item.detail), "asof": item.asof}
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
                            intent.cost_estimate.total if intent.cost_estimate is not None else None
                        ),
                        "metadata": dict(intent.metadata),
                    }
                    for intent in intents
                ],
                **(extra or {}),
            },
        )

    def _market_data_quality_config(self, symbol: str):
        if symbol in self.asset_class_by_symbol:
            return self.instrument_registry.config_for(symbol).market_data_quality
        asset_class = self.context_asset_classes[symbol]
        return self.instrument_registry.instrument_configs[asset_class].market_data_quality

    def _emit_due_research_state(self, now: datetime) -> None:
        if self.research_pipeline is None:
            return
        cadence = self.research_pipeline.sampling_cadence_minutes
        eligible = _utc(now) - timedelta(seconds=CANDLE_CLOCK_GRACE_SECONDS)
        minute = eligible.minute - eligible.minute % cadence
        boundary = eligible.replace(minute=minute, second=0, microsecond=0)
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
            # Research remains isolated from trading authority.
            self.metrics["errors"] += 1
            logger.exception("V3 research boundary failed")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
