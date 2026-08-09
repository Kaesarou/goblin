from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable

from app.brokers.base import (
    BrokerClient,
    BrokerCloseExecution,
    ClosePositionRejectedError,
    ClosePositionSubmission,
    ClosePositionSubmissionUnknownError,
)
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import PositionCloseSignal, TrackedPosition
from app.execution.position_tracker import PositionTracker
from app.execution.trade_executor import TradeExecutor
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.jsonl_journal import JsonlJournal
from app.market.data_quality import (
    MarketDataStatus,
    MarketDataValidator,
)
from app.market.models import MarketSnapshot
from app.market_data.contracts import RestMarketDataClient
from app.market_data.coordinator import MarketDataCoordinator
from app.persistence.pending_close_store import PendingCloseStore
from app.persistence.position_store import PositionStore
from app.persistence.trade_cooldown_store import TradeCooldownStore
from app.risk.risk_manager import RiskManager
from app.runtime.broker_queries import (
    PositionReconciliationResult,
    reconcile_positions_and_close_executions,
)
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskRunner
from app.runtime.closed_position_cooldown import (
    register_trade_cooldown_for_closed_position,
    register_trade_cooldown_for_unknown_confirmed_close,
)
from app.runtime.pending_close import CloseState, PendingClose
from app.runtime.position_reconciliation_state import (
    PositionReconciliationOutcome,
    PositionReconciliationTracker,
)
from app.utils.commons import spread_percent

logger = logging.getLogger(__name__)
BrokerAuthorizationErrorChecker = Callable[[Exception], bool]


@dataclass(frozen=True)
class _ReconciliationContext:
    positions: tuple[TrackedPosition, ...]
    requested_at: datetime


@dataclass(frozen=True)
class _RestContext:
    symbols: tuple[str, ...]
    requested_at: datetime


@dataclass(frozen=True)
class _CloseContext:
    position_id: str


class AsyncBrokerOperationsCoordinator:
    def __init__(
        self,
        *,
        runner: BrokerTaskRunner,
        execution_broker: BrokerClient,
        rest_market_data: RestMarketDataClient,
        executor: TradeExecutor,
        position_tracker: PositionTracker,
        risk_manager: RiskManager,
        position_store: PositionStore,
        pending_close_store: PendingCloseStore,
        cooldown_store: TradeCooldownStore,
        trade_journal: JsonlJournal,
        market_data_coordinator: MarketDataCoordinator,
        is_broker_authorization_error: BrokerAuthorizationErrorChecker,
        instrument_registry: InstrumentRegistry | None = None,
        fallback_market_data_validator: MarketDataValidator | None = None,
        reconciliation_grace_seconds: float = 30.0,
        reconciliation_required_misses: int = 3,
        reconciliation_miss_interval_seconds: float = 10.0,
        rest_control_anomaly_percent: float = 0.25,
        close_confirmation_delayed_seconds: float = 30.0,
        close_manual_intervention_seconds: float = 300.0,
    ) -> None:
        self.runner = runner
        self.execution_broker = execution_broker
        self.rest_market_data = rest_market_data
        self.executor = executor
        self.position_tracker = position_tracker
        self.risk_manager = risk_manager
        self.position_store = position_store
        self.pending_close_store = pending_close_store
        self.cooldown_store = cooldown_store
        self.trade_journal = trade_journal
        self.market_data_coordinator = market_data_coordinator
        self.is_broker_authorization_error = is_broker_authorization_error
        self.instrument_registry = instrument_registry
        self.fallback_market_data_validator = (
            fallback_market_data_validator
            if fallback_market_data_validator is not None
            else MarketDataValidator()
            if instrument_registry is not None
            else None
        )
        self.rest_control_anomaly_percent = max(0.0, rest_control_anomaly_percent)
        self.close_confirmation_delayed_seconds = max(
            0.0,
            close_confirmation_delayed_seconds,
        )
        self.close_manual_intervention_seconds = max(
            self.close_confirmation_delayed_seconds,
            close_manual_intervention_seconds,
        )
        self.reconciliation = PositionReconciliationTracker(
            grace_seconds=reconciliation_grace_seconds,
            required_misses=reconciliation_required_misses,
            minimum_miss_interval_seconds=reconciliation_miss_interval_seconds,
        )
        self._pending_closes: dict[str, PendingClose] = {}

    def reset_market_data_quality_symbol(self, symbol: str) -> None:
        if self.fallback_market_data_validator is not None:
            self.fallback_market_data_validator.reset_symbol(symbol)

    def restore_pending_closes(
        self,
        pending_closes: list[PendingClose],
        *,
        open_states: dict[str, bool],
        observed_at: datetime,
    ) -> None:
        now = _as_utc(observed_at)
        tracked_ids = {
            position.position_id
            for position in self.position_tracker.open_positions_snapshot()
        }
        for restored in pending_closes:
            pending = restored
            if pending.state == CloseState.SUBMITTING:
                pending = pending.mark_submission_unknown(
                    error=RuntimeError('runtime_restarted_during_close_submission'),
                    submitted_at=pending.requested_at,
                )
                self._persist_pending(pending, stage='startup_unknown_restore')
                self.trade_journal.write(
                    'position_close_submission_unknown',
                    self._pending_payload(
                        pending,
                        restored=True,
                        message=pending.last_error,
                    ),
                )
            self._pending_closes[pending.position_id] = pending
            self.trade_journal.write(
                'position_close_confirmation_pending',
                self._pending_payload(
                    pending,
                    restored=True,
                    risk_reserved=True,
                ),
            )
            if pending.position_id not in tracked_ids:
                self._report_manual_intervention(
                    pending,
                    now=now,
                    reason='pending_close_without_restored_position',
                )
                continue
            state = open_states.get(pending.position_id)
            if state is False:
                self.trade_journal.write(
                    'position_close_confirmation_pending',
                    self._pending_payload(
                        pending,
                        restored=True,
                        risk_reserved=True,
                        startup_portfolio_absent=True,
                        action='await_background_close_fill_lookup',
                    ),
                )
            elif state is True:
                self._observe_pending_still_open(pending, observed_at=now)
            else:
                self.trade_journal.write(
                    'position_reconciliation_warning',
                    {
                        'position_id': pending.position_id,
                        'symbol': pending.symbol,
                        'stage': 'startup_pending_close_reconciliation',
                        'message': 'broker_position_state_missing',
                    },
                )

    def schedule_reconciliation(self, *, now: datetime) -> bool:
        if self.runner.has_pending_kind('position_reconciliation'):
            return False
        positions = tuple(self.position_tracker.open_positions_snapshot())
        if not positions:
            return False
        context = _ReconciliationContext(
            positions=positions,
            requested_at=_as_utc(now),
        )
        position_ids = [position.position_id for position in positions]
        pending_close_order_ids = {
            position_id: pending.close_order_id
            for position_id, pending in self._pending_closes.items()
            if pending.close_order_id is not None
        }
        self.runner.submit(
            kind='position_reconciliation',
            context=context,
            operation=lambda ids=position_ids, close_orders=pending_close_order_ids: (
                reconcile_positions_and_close_executions(
                    self.execution_broker,
                    ids,
                    close_orders,
                )
            ),
        )
        return True

    def schedule_rest_control(
        self,
        *,
        symbols: list[str],
        now: datetime,
    ) -> bool:
        if not symbols or self.runner.has_pending_kind('rest_control'):
            return False
        context = _RestContext(tuple(symbols), _as_utc(now))
        self.runner.submit(
            kind='rest_control',
            context=context,
            operation=lambda requested=list(symbols): (
                self.rest_market_data.get_market_snapshots(requested)
            ),
        )
        return True

    def schedule_position_fallback(
        self,
        *,
        symbols: list[str],
        now: datetime,
    ) -> bool:
        if not symbols or self.runner.has_pending_kind('position_fallback'):
            return False
        context = _RestContext(tuple(symbols), _as_utc(now))
        self.runner.submit(
            kind='position_fallback',
            context=context,
            operation=lambda requested=list(symbols): (
                self.rest_market_data.get_market_snapshots(requested)
            ),
        )
        return True

    def on_snapshot(
        self,
        *,
        snapshot: MarketSnapshot,
        session_decision: Any = None,
        source: str = 'websocket',
    ) -> None:
        if (
            source != 'rest_fallback'
            and self.fallback_market_data_validator is not None
        ):
            self.fallback_market_data_validator.observe_accepted(snapshot)
        force_close = bool(
            session_decision is not None
            and getattr(session_decision, 'force_close_required', False)
        )
        force_close_metadata = None
        if force_close:
            force_close_metadata = {
                'session_decision': getattr(session_decision, 'reason', None),
                'time_until_session_end_minutes': getattr(
                    session_decision,
                    'time_until_session_end_minutes',
                    None,
                ),
            }
        close_signals = self.position_tracker.evaluate_snapshot(
            snapshot,
            force_close=force_close,
            force_close_metadata=force_close_metadata,
        )
        self._record_managed_stop_updates(snapshot)
        for close_signal in close_signals:
            self.submit_close(
                signal=close_signal,
                source=f'{source}_position_guard',
            )

    def submit_close(
        self,
        *,
        signal: PositionCloseSignal,
        source: str,
        session_decision: Any = None,
    ) -> bool:
        if signal.position_id in self._pending_closes:
            return False
        tracked_ids = {
            position.position_id
            for position in self.position_tracker.open_positions_snapshot()
        }
        if signal.position_id not in tracked_ids:
            return False

        pending = PendingClose(
            position_id=signal.position_id,
            symbol=signal.symbol,
            signal=signal,
            source=source,
            state=CloseState.SUBMITTING,
            requested_at=datetime.now(timezone.utc),
            metadata=self._runtime_close_metadata(session_decision),
        )
        if not self._persist_pending(pending, stage='close_requested'):
            return False
        self._pending_closes[pending.position_id] = pending
        self.trade_journal.write(
            'position_close_requested',
            self._pending_payload(
                pending,
                risk_reserved=True,
                position_persisted=True,
            ),
        )
        try:
            self.runner.submit(
                kind='close_position',
                task_id=f'close_position:{signal.position_id}',
                context=_CloseContext(position_id=signal.position_id),
                operation=lambda position_id=signal.position_id: (
                    self.executor.close(position_id)
                ),
            )
        except Exception as exc:
            rejected = pending.mark_rejected(error=exc)
            self._pending_closes[pending.position_id] = rejected
            self._persist_pending(rejected, stage='close_task_submission')
            self.trade_journal.write(
                'position_close_rejected',
                self._pending_payload(
                    rejected,
                    message=str(exc),
                    rejection_stage='runtime_task_submission',
                ),
            )
            self._report_manual_intervention(
                rejected,
                now=datetime.now(timezone.utc),
                reason='close_task_submission_failed',
            )
            return False
        return True

    def handle_completion(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
        latest_snapshots: dict[str, MarketSnapshot],
    ) -> bool:
        if completion.kind == 'position_reconciliation':
            self._handle_reconciliation(
                completion,
                now=_as_utc(now),
                latest_snapshots=latest_snapshots,
            )
            return True
        if completion.kind == 'rest_control':
            self._handle_rest_control(
                completion,
                latest_snapshots=latest_snapshots,
                now=_as_utc(now),
            )
            return True
        if completion.kind == 'position_fallback':
            self._handle_position_fallback(completion, now=_as_utc(now))
            return True
        if completion.kind == 'close_position':
            self._handle_close(completion, now=_as_utc(now))
            return True
        return False

    def diagnostics(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for pending in self._pending_closes.values():
            states[pending.state.value] = states.get(pending.state.value, 0) + 1
        return {
            'pending_close_position_ids': sorted(self._pending_closes),
            'pending_close_states': states,
            'reconciliation_evidence': self.reconciliation.snapshot(),
        }

    def _handle_reconciliation(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
        latest_snapshots: dict[str, MarketSnapshot],
    ) -> None:
        context = completion.context
        if not isinstance(context, _ReconciliationContext):
            return
        if completion.error is not None:
            if self.is_broker_authorization_error(completion.error):
                raise completion.error
            self.trade_journal.write(
                'position_reconciliation_warning',
                {
                    'position_count': len(context.positions),
                    'message': str(completion.error),
                },
            )
            return
        result = completion.value
        if not isinstance(result, PositionReconciliationResult):
            self.trade_journal.write(
                'position_reconciliation_warning',
                {
                    'position_count': len(context.positions),
                    'message': 'invalid_position_state_payload',
                },
            )
            return
        normalized_states = {
            str(key): bool(value)
            for key, value in result.open_states.items()
        }

        for position_id, pending in list(self._pending_closes.items()):
            if position_id not in normalized_states:
                self.trade_journal.write(
                    'position_reconciliation_warning',
                    {
                        'position_id': position_id,
                        'symbol': pending.symbol,
                        'message': 'broker_position_state_missing',
                        'state': pending.state.value,
                    },
                )
                continue
            if normalized_states[position_id]:
                self._observe_pending_still_open(pending, observed_at=now)
            else:
                unavailable_reason = result.close_execution_unavailable.get(
                    position_id
                )
                if unavailable_reason is not None:
                    self.trade_journal.write(
                        'broker_close_fill_unavailable',
                        self._pending_payload(
                            pending,
                            reason=unavailable_reason,
                            portfolio_absence_confirmed=True,
                            executable_estimate_retained=True,
                        ),
                    )
                self._confirm_pending_close(
                    pending,
                    closed_at=now,
                    source='runtime_portfolio_reconciliation',
                    broker_execution=result.close_executions.get(position_id),
                )

        current_by_id = {
            position.position_id: position
            for position in self.position_tracker.open_positions_snapshot()
        }
        pending_ids = set(self._pending_closes)
        positions = [
            current_by_id[position.position_id]
            for position in context.positions
            if position.position_id in current_by_id
            and position.position_id not in pending_ids
        ]
        outcome = self.reconciliation.observe(
            positions=positions,
            open_states={
                position_id: state
                for position_id, state in normalized_states.items()
                if position_id not in pending_ids
            },
            observed_at=now,
        )
        self._write_reconciliation_outcome(outcome)
        for position in outcome.confirmed_closed:
            self._apply_reconciled_close(
                position,
                closed_at=now,
                snapshot=latest_snapshots.get(position.symbol),
            )

    def _write_reconciliation_outcome(
        self,
        outcome: PositionReconciliationOutcome,
    ) -> None:
        for position in outcome.newly_suspect:
            evidence = self.reconciliation.evidence_for(position.position_id)
            self.trade_journal.write(
                'position_reconciliation_suspect',
                {
                    'position': position,
                    'evidence': evidence,
                    'risk_reserved': True,
                },
            )
        for position in outcome.still_suspect:
            evidence = self.reconciliation.evidence_for(position.position_id)
            self.trade_journal.write(
                'position_reconciliation_suspect_updated',
                {'position': position, 'evidence': evidence},
            )
        for position in outcome.recovered:
            self.trade_journal.write(
                'position_reconciliation_recovered',
                {'position': position},
            )
        if outcome.grace_ignored:
            self.trade_journal.write(
                'position_reconciliation_grace',
                {
                    'position_ids': [
                        position.position_id
                        for position in outcome.grace_ignored
                    ],
                    'count': len(outcome.grace_ignored),
                },
            )
        if outcome.missing_states:
            self.trade_journal.write(
                'position_reconciliation_warning',
                {
                    'position_ids': [
                        position.position_id
                        for position in outcome.missing_states
                    ],
                    'message': 'broker_position_state_missing',
                },
            )

    def _apply_reconciled_close(
        self,
        position: TrackedPosition,
        *,
        closed_at: datetime,
        snapshot: MarketSnapshot | None,
    ) -> None:
        closed_position = None
        removed = position
        if snapshot is not None:
            executable_estimate = snapshot.executable_exit_price(position.side)
            closed_position = self.position_tracker.record_closed_position(
                PositionCloseSignal(
                    position_id=position.position_id,
                    symbol=position.symbol,
                    side=position.side,
                    reason=PositionCloseReason.MANUAL_OR_BROKER_CLOSE,
                    detected_at=snapshot.timestamp,
                    last_execution_price=snapshot.last,
                    executable_estimate=executable_estimate,
                    bid_at_detection=snapshot.bid,
                    ask_at_detection=snapshot.ask,
                    observed_spread_percent=spread_percent(snapshot),
                    metadata={
                        'confirmation_source': 'portfolio_reconciliation',
                        'broker_close_fill_available': False,
                    },
                ),
                confirmed_at=closed_at,
            )
            if closed_position is None:
                return
        else:
            removed = self.position_tracker.remove_position(position.position_id)
            if removed is None:
                return
        closed_session_key = self.risk_manager.record_close_position(
            removed.symbol
        )
        try:
            self.position_store.delete_open_position(removed.position_id)
        except Exception as exc:
            self.trade_journal.write(
                'position_persistence_error',
                {
                    'symbol': removed.symbol,
                    'position_id': removed.position_id,
                    'message': str(exc),
                },
            )
        if closed_position is not None:
            register_trade_cooldown_for_closed_position(
                closed_position=closed_position,
                risk_manager=self.risk_manager,
                cooldown_store=self.cooldown_store,
                trade_journal=self.trade_journal,
                session_key=closed_session_key,
            )
        else:
            register_trade_cooldown_for_unknown_confirmed_close(
                position=removed,
                closed_at=closed_at,
                risk_manager=self.risk_manager,
                cooldown_store=self.cooldown_store,
                trade_journal=self.trade_journal,
                session_key=closed_session_key,
            )
        self.execution_broker.forget_position_instrument(removed.position_id)
        self.trade_journal.write(
            'position_reconciled_closed',
            {
                'source': 'runtime_broker_reconciliation_confirmed',
                'position': removed,
                'closed_position': closed_position,
                'closed_at': closed_at,
                'confirmation_policy': 'three_fresh_absences_after_grace',
                'broker_close_fill_available': False,
                'exit_price_provenance': (
                    closed_position.exit_price_source
                    if closed_position is not None
                    else 'unavailable'
                ),
                'risk_released': True,
            },
        )

    def _handle_rest_control(
        self,
        completion: BrokerTaskCompletion,
        *,
        latest_snapshots: dict[str, MarketSnapshot],
        now: datetime,
    ) -> None:
        context = completion.context
        if not isinstance(context, _RestContext):
            return
        if completion.error is not None:
            self.trade_journal.write(
                'rest_control_error',
                {
                    'symbols': list(context.symbols),
                    'message': str(completion.error),
                },
            )
            return
        snapshots = completion.value
        if not isinstance(snapshots, dict):
            return
        deltas: list[float] = []
        anomalies: list[dict[str, Any]] = []
        for symbol, rest_snapshot in snapshots.items():
            websocket_snapshot = latest_snapshots.get(symbol)
            if websocket_snapshot is None or websocket_snapshot.last == 0:
                continue
            delta_percent = abs(
                (rest_snapshot.last - websocket_snapshot.last)
                / websocket_snapshot.last
                * 100
            )
            deltas.append(delta_percent)
            if delta_percent >= self.rest_control_anomaly_percent:
                anomalies.append(
                    {
                        'symbol': symbol,
                        'delta_percent': round(delta_percent, 6),
                        'rest_last': rest_snapshot.last,
                        'websocket_last': websocket_snapshot.last,
                    }
                )
        sorted_deltas = sorted(deltas)
        self.trade_journal.write(
            'rest_control_completed',
            {
                'requested_symbol_count': len(context.symbols),
                'received_symbol_count': len(snapshots),
                'compared_symbol_count': len(deltas),
                'missing_symbol_count': max(
                    0,
                    len(context.symbols) - len(snapshots),
                ),
                'median_delta_percent': round(median(deltas), 6) if deltas else None,
                'p95_delta_percent': _percentile(sorted_deltas, 0.95),
                'max_delta_percent': round(max(deltas), 6) if deltas else None,
                'anomaly_count': len(anomalies),
                'anomalies': anomalies,
                'requested_at': context.requested_at,
                'completed_at': now,
            },
        )

    def _handle_position_fallback(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
    ) -> None:
        context = completion.context
        if not isinstance(context, _RestContext):
            return
        symbols = list(context.symbols)
        if completion.error is not None:
            self.market_data_coordinator.mark_fallback_failed(symbols)
            self.trade_journal.write(
                'rest_position_fallback_error',
                {'symbols': symbols, 'message': str(completion.error)},
            )
            return
        snapshots = completion.value
        if not isinstance(snapshots, dict):
            snapshots = {}
        received = list(snapshots)
        if received:
            self.market_data_coordinator.mark_fallback_succeeded(received)
        missing = [symbol for symbol in symbols if symbol not in snapshots]
        if missing:
            self.market_data_coordinator.mark_fallback_failed(missing)
        self.trade_journal.write(
            'rest_position_fallback_completed',
            {
                'requested_symbols': symbols,
                'received_symbols': received,
                'missing_symbols': missing,
                'requested_at': context.requested_at,
                'completed_at': now,
            },
        )
        for snapshot in snapshots.values():
            if not self._position_fallback_snapshot_is_accepted(
                snapshot,
                now=now,
            ):
                continue
            self.on_snapshot(
                snapshot=snapshot,
                session_decision=None,
                source='rest_fallback',
            )

    def _position_fallback_snapshot_is_accepted(
        self,
        snapshot: MarketSnapshot,
        *,
        now: datetime,
    ) -> bool:
        validator = self.fallback_market_data_validator
        registry = self.instrument_registry
        if validator is None or registry is None:
            return True
        validation = validator.validate(
            snapshot,
            registry.config_for(snapshot.symbol).market_data_quality,
            now=now,
        )
        if validation.status is not MarketDataStatus.ACCEPTED:
            event_type = (
                'market_data_quarantined'
                if validation.status is MarketDataStatus.QUARANTINED
                else 'market_data_rejected'
            )
            self.trade_journal.write(
                event_type,
                {
                    'symbol': snapshot.symbol,
                    'validation': validation,
                    'source': 'rest_fallback',
                },
            )
            return False
        if validation.reasons:
            self.trade_journal.write(
                'market_data_quality_resolved',
                {
                    'symbol': snapshot.symbol,
                    'validation': validation,
                    'source': 'rest_fallback',
                },
            )
        return True

    def _handle_close(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
    ) -> None:
        context = completion.context
        if not isinstance(context, _CloseContext):
            return
        pending = self._pending_closes.get(context.position_id)
        if pending is None:
            self.trade_journal.write(
                'position_reconciliation_warning',
                {
                    'position_id': context.position_id,
                    'message': 'close_completion_without_pending_state',
                },
            )
            return

        error = completion.error
        if error is not None:
            if isinstance(error, ClosePositionRejectedError):
                rejected = pending.mark_rejected(error=error)
                self._pending_closes[pending.position_id] = rejected
                self._persist_pending(rejected, stage='close_rejected')
                self.trade_journal.write(
                    'position_close_rejected',
                    self._pending_payload(
                        rejected,
                        message=str(error),
                        broker_response=error.broker_response,
                    ),
                )
                self._report_manual_intervention(
                    rejected,
                    now=now,
                    reason='broker_explicit_rejection',
                )
                if self.is_broker_authorization_error(error):
                    raise error
                return

            submitted_at = getattr(error, 'submitted_at', None)
            unknown = pending.mark_submission_unknown(
                error=error,
                submitted_at=(
                    submitted_at if isinstance(submitted_at, datetime) else None
                ),
            )
            self._pending_closes[pending.position_id] = unknown
            self._persist_pending(unknown, stage='close_submission_unknown')
            self.trade_journal.write(
                'position_close_submission_unknown',
                self._pending_payload(
                    unknown,
                    message=str(error),
                    broker_response=getattr(error, 'broker_response', None),
                ),
            )
            if self.is_broker_authorization_error(error):
                raise error
            return

        submission = completion.value
        if not isinstance(submission, ClosePositionSubmission):
            error = ClosePositionSubmissionUnknownError(
                position_id=pending.position_id,
                submitted_at=pending.requested_at,
                cause=TypeError(
                    'close operation returned invalid submission payload: '
                    f'{type(submission).__name__}'
                ),
            )
            unknown = pending.mark_submission_unknown(
                error=error,
                submitted_at=pending.requested_at,
            )
            self._pending_closes[pending.position_id] = unknown
            self._persist_pending(unknown, stage='invalid_close_submission')
            self.trade_journal.write(
                'position_close_submission_unknown',
                self._pending_payload(unknown, message=str(error)),
            )
            return

        try:
            submitted = pending.mark_submitted(submission)
        except ValueError as exc:
            unknown_error = ClosePositionSubmissionUnknownError(
                position_id=pending.position_id,
                submitted_at=submission.submitted_at,
                cause=exc,
                broker_response=submission.broker_response,
            )
            unknown = pending.mark_submission_unknown(
                error=unknown_error,
                submitted_at=submission.submitted_at,
            )
            self._pending_closes[pending.position_id] = unknown
            self._persist_pending(unknown, stage='mismatched_close_submission')
            self.trade_journal.write(
                'position_close_submission_unknown',
                self._pending_payload(
                    unknown,
                    message=str(unknown_error),
                    broker_response=submission.broker_response,
                ),
            )
            return

        self._pending_closes[pending.position_id] = submitted
        self._persist_pending(submitted, stage='close_submitted')
        self.trade_journal.write(
            'position_close_submitted',
            self._pending_payload(
                submitted,
                broker_response=submission.broker_response,
            ),
        )
        self.trade_journal.write(
            'position_close_confirmation_pending',
            self._pending_payload(
                submitted,
                risk_reserved=True,
                position_persisted=True,
            ),
        )

    def _observe_pending_still_open(
        self,
        pending: PendingClose,
        *,
        observed_at: datetime,
    ) -> None:
        now = _as_utc(observed_at)
        updated = pending.observe_still_open(observed_at=now)
        age_seconds = updated.confirmation_age_seconds(now=now)
        if (
            age_seconds >= self.close_confirmation_delayed_seconds
            and updated.delayed_reported_at is None
        ):
            updated = updated.mark_delayed_reported(reported_at=now)
            self.trade_journal.write(
                'position_close_confirmation_delayed',
                self._pending_payload(
                    updated,
                    confirmation_age_seconds=round(age_seconds, 3),
                ),
            )
        if (
            age_seconds >= self.close_manual_intervention_seconds
            and updated.manual_intervention_reported_at is None
        ):
            updated = updated.mark_manual_intervention_reported(reported_at=now)
            self.trade_journal.write(
                'position_close_manual_intervention_required',
                self._pending_payload(
                    updated,
                    reason='close_confirmation_timeout',
                    confirmation_age_seconds=round(age_seconds, 3),
                ),
            )
        self._pending_closes[updated.position_id] = updated
        self._persist_pending(updated, stage='close_confirmation_check')

    def _confirm_pending_close(
        self,
        pending: PendingClose,
        *,
        closed_at: datetime,
        source: str,
        broker_execution: BrokerCloseExecution | None = None,
    ) -> None:
        tracked_ids = {
            position.position_id
            for position in self.position_tracker.open_positions_snapshot()
        }
        if pending.position_id not in tracked_ids:
            self._report_manual_intervention(
                pending,
                now=closed_at,
                reason='portfolio_closed_but_tracker_position_missing',
            )
            return
        try:
            self.pending_close_store.delete_with_open_position(pending.position_id)
        except Exception as exc:
            self.trade_journal.write(
                'position_persistence_error',
                {
                    'symbol': pending.symbol,
                    'position_id': pending.position_id,
                    'stage': 'close_confirmation_cleanup',
                    'message': str(exc),
                },
            )
            return

        closed_position = self.position_tracker.record_closed_position(
            pending.signal,
            broker_execution=broker_execution,
            confirmed_at=closed_at,
        )
        closed_session_key = self.risk_manager.record_close_position(pending.symbol)
        if closed_position is not None:
            register_trade_cooldown_for_closed_position(
                closed_position=closed_position,
                risk_manager=self.risk_manager,
                cooldown_store=self.cooldown_store,
                trade_journal=self.trade_journal,
                session_key=closed_session_key,
            )
        self.reconciliation.clear(pending.position_id)
        self.execution_broker.forget_position_instrument(pending.position_id)
        self._pending_closes.pop(pending.position_id, None)
        if broker_execution is not None:
            self.trade_journal.write(
                'broker_close_fill_resolved',
                self._pending_payload(
                    pending,
                    broker_execution=broker_execution,
                    broker_exit_fill_price=(
                        broker_execution.executed_exit_price
                    ),
                    broker_executed_at=broker_execution.executed_at,
                ),
            )
        self.trade_journal.write(
            'position_close_confirmed',
            self._pending_payload(
                pending,
                confirmation_source=source,
                confirmed_at=closed_at,
                closed_position=closed_position,
                confirmation_policy='first_absence_after_close_request',
                broker_close_fill_available=(broker_execution is not None),
                risk_released=True,
                position_persistence_cleared=True,
            ),
        )

    def _report_manual_intervention(
        self,
        pending: PendingClose,
        *,
        now: datetime,
        reason: str,
    ) -> None:
        if pending.manual_intervention_reported_at is not None:
            return
        updated = pending.mark_manual_intervention_reported(reported_at=now)
        self._pending_closes[updated.position_id] = updated
        self._persist_pending(updated, stage='manual_intervention')
        self.trade_journal.write(
            'position_close_manual_intervention_required',
            self._pending_payload(updated, reason=reason),
        )

    def _persist_pending(self, pending: PendingClose, *, stage: str) -> bool:
        try:
            self.pending_close_store.save(pending)
        except Exception as exc:
            self.trade_journal.write(
                'position_persistence_error',
                {
                    'symbol': pending.symbol,
                    'position_id': pending.position_id,
                    'stage': stage,
                    'message': str(exc),
                },
            )
            return False
        return True

    @staticmethod
    def _runtime_close_metadata(session_decision: Any) -> dict[str, Any] | None:
        if session_decision is None:
            return None
        return {
            'session_decision_reason': getattr(session_decision, 'reason', None),
            'time_until_session_end_minutes': getattr(
                session_decision,
                'time_until_session_end_minutes',
                None,
            ),
        }

    @staticmethod
    def _pending_payload(
        pending: PendingClose,
        **extra: Any,
    ) -> dict[str, Any]:
        payload = {
            'position_id': pending.position_id,
            'symbol': pending.symbol,
            'state': pending.state.value,
            'source': pending.source,
            'close_signal': pending.signal,
            'requested_at': pending.requested_at,
            'submitted_at': pending.submitted_at,
            'accepted_at': pending.accepted_at,
            'close_order_id': pending.close_order_id,
            'reference_id': pending.reference_id,
            'confirmation_checks': pending.confirmation_checks,
            'last_confirmation_at': pending.last_confirmation_at,
            'last_error': pending.last_error,
            'metadata': pending.metadata,
        }
        payload.update(extra)
        return payload

    def _record_managed_stop_updates(self, snapshot: MarketSnapshot) -> None:
        for update in self.position_tracker.consume_managed_stop_updates():
            position = update.position
            self.trade_journal.write(
                'managed_stop_updated',
                {
                    'symbol': position.symbol,
                    'position_id': position.position_id,
                    'side': position.side,
                    'protection_type': position.managed_stop_protection_type,
                    'previous_stop_loss': update.previous_position.stop_loss,
                    'new_stop_loss': position.stop_loss,
                    'observed_at': update.observed_at,
                    'snapshot': snapshot,
                    'position': position,
                    'metadata': position.last_stop_update_metadata,
                },
            )
            try:
                self.position_store.save_open_position(position)
            except Exception as exc:
                self.trade_journal.write(
                    'position_persistence_error',
                    {
                        'symbol': position.symbol,
                        'position_id': position.position_id,
                        'stage': 'managed_stop_update',
                        'message': str(exc),
                    },
                )


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    index = min(
        len(sorted_values) - 1,
        max(0, round((len(sorted_values) - 1) * fraction)),
    )
    return round(sorted_values[index], 6)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
