from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.brokers.base import BrokerClient, OpenPositionResult
from app.execution.candidate_economics import (
    CandidateEconomicsEstimator,
    EvaluatedTradeCandidate,
)
from app.execution.candidate_selector import (
    EvaluatedCandidateSelectionResult,
    rank_evaluated_trade_candidates,
)
from app.execution.position_tracker import PositionTracker
from app.execution.sl_tp_profile import EffectiveSlTp, EffectiveSlTpResolver
from app.execution.trade_candidate import TradeCandidate
from app.execution.trade_executor import TradeExecutor
from app.journal.jsonl_journal import JsonlJournal
from app.persistence.position_store import PositionStore
from app.risk.models import TradePlan
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.runtime.broker_queries import (
    UnknownOrderLookup,
    UnknownOrderResolution,
    is_confirmation_unknown_error,
    order_id_from_confirmation_error,
    resolve_unknown_open_order,
)
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskRunner
from app.runtime.candidate_flow import (
    _candidate_selection_result_from_evaluated,
    _evaluate_risk_manager,
    _resolve_runtime_effective_sl_tp,
    _slippage_percent,
    apply_outcome_probability_to_evaluated_candidates,
    apply_tp_feasibility_to_evaluated_candidates,
    apply_trade_cooldown_guard,
    attach_entry_decisions,
    select_evaluated_trade_candidates_with_strategy_profile,
    select_trade_candidates_with_strategy_profile,
)
from app.runtime.pending_candidate_lifecycle import (
    invalidate_pending_candidate,
    keep_pending_waiting,
    pending_entry_id,
    reconcile_pending_selection_rejections,
)
from app.runtime.pending_entry import PendingEntryManager
from app.strategies.models import StrategyProfileConfig
from app.utils.commons import spread_percent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CandidateBatch:
    batch_id: str
    candidates: tuple[TradeCandidate, ...]
    created_at: datetime


@dataclass(frozen=True)
class _OrderContext:
    reservation_id: str
    candidate: TradeCandidate
    trade_plan: TradePlan
    economics: Any
    effective_sl_tp: EffectiveSlTp | None
    instrument_profile: Any
    risk_profile: Any
    submitted_at: datetime


@dataclass
class _Reservation:
    reservation_id: str
    symbol: str
    session_key: str
    created_at: datetime
    state: str = 'open_pending'


@dataclass
class _UnknownOrder:
    context: _OrderContext
    order_id: str | None
    reference_id: str | None
    attempts: int
    first_unknown_at: datetime
    next_lookup_at: datetime
    lookup_pending: bool = False


class AsyncCandidateExecutionCoordinator:
    """Keep broker I/O off the event loop while applying state on the main thread."""

    def __init__(
        self,
        *,
        runner: BrokerTaskRunner,
        execution_broker: BrokerClient,
        executor: TradeExecutor,
        risk_manager: RiskManager,
        position_tracker: PositionTracker,
        position_store: PositionStore | None,
        trade_journal: JsonlJournal,
        strategy_profile: StrategyProfileConfig,
        cooldown_guard: TradeCooldownGuard | None,
        candidate_economics_estimator: CandidateEconomicsEstimator | None,
        pending_entry_manager: PendingEntryManager | None,
        unknown_lookup_interval_seconds: float = 15.0,
        unknown_max_age_minutes: float = 30.0,
    ) -> None:
        self.runner = runner
        self.execution_broker = execution_broker
        self.executor = executor
        self.risk_manager = risk_manager
        self.position_tracker = position_tracker
        self.position_store = position_store
        self.trade_journal = trade_journal
        self.strategy_profile = strategy_profile
        self.cooldown_guard = cooldown_guard
        self.candidate_economics_estimator = candidate_economics_estimator
        self.pending_entry_manager = pending_entry_manager
        self.unknown_lookup_interval = timedelta(
            seconds=max(5.0, unknown_lookup_interval_seconds)
        )
        self.unknown_max_age = timedelta(
            minutes=max(1.0, unknown_max_age_minutes)
        )
        self._queued_batches: deque[_CandidateBatch] = deque()
        self._equity_batch_ids: set[str] = set()
        self._reservations: dict[str, _Reservation] = {}
        self._unknown_orders: dict[str, _UnknownOrder] = {}

    def submit_candidates(
        self,
        candidates: list[TradeCandidate],
        *,
        now: datetime,
    ) -> None:
        if not candidates:
            return
        filtered = candidates
        if self.cooldown_guard is not None:
            filtered = apply_trade_cooldown_guard(
                candidates=filtered,
                risk_manager=self.risk_manager,
                cooldown_guard=self.cooldown_guard,
                trade_journal=self.trade_journal,
            )
        if not filtered:
            return
        batch = _CandidateBatch(
            batch_id=f'candidate-batch:{uuid4()}',
            candidates=tuple(filtered),
            created_at=_as_utc(now),
        )
        self._queued_batches.append(batch)
        self._start_next_equity_lookup()

    def handle_completion(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
    ) -> bool:
        if completion.kind == 'candidate_equity':
            self._handle_equity_completion(completion, now=_as_utc(now))
            return True
        if completion.kind == 'open_order':
            self._handle_open_order_completion(completion, now=_as_utc(now))
            return True
        if completion.kind == 'unknown_order_lookup':
            self._handle_unknown_lookup_completion(
                completion,
                now=_as_utc(now),
            )
            return True
        return False

    def schedule_unknown_order_lookups(self, *, now: datetime) -> None:
        actual_now = _as_utc(now)
        for reservation_id, unknown in list(self._unknown_orders.items()):
            if unknown.lookup_pending or actual_now < unknown.next_lookup_at:
                continue
            if actual_now - unknown.first_unknown_at >= self.unknown_max_age:
                self.trade_journal.write(
                    'order_confirmation_manual_intervention_required',
                    {
                        'reservation_id': reservation_id,
                        'order_id': unknown.order_id,
                        'reference_id': unknown.reference_id,
                        'symbol': unknown.context.candidate.symbol,
                        'side': unknown.context.candidate.signal.action,
                        'attempts': unknown.attempts,
                        'unknown_since': unknown.first_unknown_at,
                    },
                )
                unknown.next_lookup_at = actual_now + self.unknown_lookup_interval
                continue
            unknown.lookup_pending = True
            lookup = UnknownOrderLookup(
                order_id=unknown.order_id,
                reference_id=unknown.reference_id,
                symbol=unknown.context.candidate.symbol,
                side=unknown.context.candidate.signal.action,
                amount=float(unknown.context.trade_plan.amount or 0.0),
                submitted_at=unknown.context.submitted_at,
                known_position_ids=tuple(
                    position.position_id
                    for position in self.position_tracker.open_positions_snapshot()
                ),
            )
            self.runner.submit(
                kind='unknown_order_lookup',
                task_id=f'unknown-order:{reservation_id}',
                context=reservation_id,
                operation=lambda current=lookup: resolve_unknown_open_order(
                    self.execution_broker,
                    current,
                ),
            )

    def pending_open_count(self) -> int:
        return len(self._reservations)

    def diagnostics(self) -> dict[str, Any]:
        return {
            'queued_candidate_batches': len(self._queued_batches),
            'pending_open_reservations': len(self._reservations),
            'unknown_orders': {
                reservation_id: {
                    'symbol': item.context.candidate.symbol,
                    'order_id': item.order_id,
                    'attempts': item.attempts,
                    'first_unknown_at': item.first_unknown_at,
                    'next_lookup_at': item.next_lookup_at,
                }
                for reservation_id, item in self._unknown_orders.items()
            },
        }

    def _start_next_equity_lookup(self) -> None:
        if self._equity_batch_ids or not self._queued_batches:
            return
        batch = self._queued_batches.popleft()
        self._equity_batch_ids.add(batch.batch_id)
        self.runner.submit(
            kind='candidate_equity',
            task_id=batch.batch_id,
            context=batch,
            operation=self.execution_broker.get_account_equity,
        )

    def _handle_equity_completion(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
    ) -> None:
        batch = completion.context
        if not isinstance(batch, _CandidateBatch):
            return
        self._equity_batch_ids.discard(batch.batch_id)
        try:
            if completion.error is not None:
                self._record_batch_error(batch, completion.error)
                return
            equity = float(completion.value)
            self._prepare_and_submit_orders(batch, equity=equity, now=now)
        finally:
            self._start_next_equity_lookup()

    def _prepare_and_submit_orders(
        self,
        batch: _CandidateBatch,
        *,
        equity: float,
        now: datetime,
    ) -> None:
        candidates = list(batch.candidates)
        selected_evaluated: list[EvaluatedTradeCandidate] | None = None
        rejected_evaluated = None
        if self.candidate_economics_estimator is None:
            selection_result = select_trade_candidates_with_strategy_profile(
                candidates,
                self.risk_manager,
                self.strategy_profile,
            )
            ranked_candidates = selection_result.selected_candidates
        else:
            evaluated = [
                self.candidate_economics_estimator.evaluate(candidate, equity)
                for candidate in candidates
            ]
            self.trade_journal.write(
                'candidate_economics',
                {'equity': equity, 'evaluated_candidates': evaluated},
            )
            evaluated = apply_tp_feasibility_to_evaluated_candidates(
                evaluated_candidates=evaluated,
                risk_manager=self.risk_manager,
            )
            self.trade_journal.write(
                'candidate_tp_feasibility',
                {'equity': equity, 'evaluated_candidates': evaluated},
            )
            evaluated = attach_entry_decisions(evaluated)
            evaluated = (
                apply_outcome_probability_to_evaluated_candidates(
                    evaluated_candidates=evaluated,
                    risk_manager=self.risk_manager,
                )
            )
            self.trade_journal.write(
                'candidate_outcome_probability',
                {
                    'equity': equity,
                    'evaluated_candidates': evaluated,
                },
            )
            evaluated_selection = (
                EvaluatedCandidateSelectionResult(
                    rank_evaluated_trade_candidates(evaluated),
                    [],
                )
                if self.strategy_profile is None
                else select_evaluated_trade_candidates_with_strategy_profile(
                    evaluated,
                    self.risk_manager,
                    self.strategy_profile,
                )
            )
            reconcile_pending_selection_rejections(
                rejected_candidates=evaluated_selection.rejected_candidates,
                pending_entry_manager=self.pending_entry_manager,
                trade_journal=self.trade_journal,
            )
            selected_evaluated = evaluated_selection.selected_candidates
            rejected_evaluated = evaluated_selection.rejected_candidates
            selection_result = _candidate_selection_result_from_evaluated(
                evaluated_selection
            )
            ranked_candidates = selection_result.selected_candidates

        self.trade_journal.write(
            'candidate_selection',
            {
                'strategy_profile': self.strategy_profile.name,
                'selected_candidates': selection_result.selected_candidates,
                'rejected_candidates': selection_result.rejected_candidates,
                'selected_evaluated_candidates': selected_evaluated,
                'rejected_evaluated_candidates': rejected_evaluated,
            },
        )
        if not ranked_candidates:
            return

        economics_by_id = {
            id(item.candidate): item.economics
            for item in selected_evaluated or []
        }
        effective_by_id = {
            id(item.candidate): item.effective_sl_tp
            for item in selected_evaluated or []
        }
        resolver = EffectiveSlTpResolver()
        self.trade_journal.write(
            'candidate_ranking',
            {
                'candidates': ranked_candidates,
                'evaluated_candidates': selected_evaluated,
            },
        )

        for candidate in ranked_candidates:
            if not self._reservation_allowed(candidate):
                self._write_pending_risk_rejection(candidate)
                continue
            effective_sl_tp = (
                effective_by_id.get(id(candidate))
                or _resolve_runtime_effective_sl_tp(
                    candidate=candidate,
                    risk_profile=self.risk_manager.risk_profile_for(
                        candidate.symbol
                    ),
                    resolver=resolver,
                )
            )
            plan = _evaluate_risk_manager(
                risk_manager=self.risk_manager,
                candidate=candidate,
                equity=equity,
                effective_sl_tp=effective_sl_tp,
            )
            instrument_profile = self.risk_manager.instrument_profile_for(
                candidate.symbol
            )
            risk_profile = self.risk_manager.risk_profile_for(candidate.symbol)
            economics = economics_by_id.get(id(candidate))
            self.trade_journal.write(
                'decision',
                {
                    'symbol': candidate.symbol,
                    'snapshot': candidate.snapshot,
                    'candle': candidate.candle,
                    'signal': candidate.signal,
                    'candidate': candidate,
                    'candidate_economics': economics,
                    'effective_sl_tp': effective_sl_tp,
                    'equity': equity,
                    'trade_plan': plan,
                    'instrument_profile': instrument_profile,
                    'risk_profile': risk_profile,
                },
            )
            if not plan.approved:
                invalidate_pending_candidate(
                    candidate=candidate,
                    reason=f'risk_reject:{plan.reason}',
                    pending_entry_manager=self.pending_entry_manager,
                    trade_journal=self.trade_journal,
                )
                continue

            reservation = self._reserve(candidate, now=now)
            context = _OrderContext(
                reservation_id=reservation.reservation_id,
                candidate=candidate,
                trade_plan=plan,
                economics=economics,
                effective_sl_tp=effective_sl_tp,
                instrument_profile=instrument_profile,
                risk_profile=risk_profile,
                submitted_at=now,
            )
            self.trade_journal.write(
                'order_submitted',
                {
                    'reservation_id': reservation.reservation_id,
                    'symbol': candidate.symbol,
                    'candidate': candidate,
                    'candidate_economics': economics,
                    'effective_sl_tp': effective_sl_tp,
                    'trade_plan': plan,
                    'instrument_profile': instrument_profile,
                    'risk_profile': risk_profile,
                    'execution_state': 'open_pending',
                },
            )
            self.runner.submit(
                kind='open_order',
                context=context,
                operation=lambda current_plan=plan: self.executor.execute(
                    current_plan
                ),
            )

    def _handle_open_order_completion(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
    ) -> None:
        context = completion.context
        if not isinstance(context, _OrderContext):
            return
        if completion.error is not None:
            if is_confirmation_unknown_error(completion.error):
                self._mark_confirmation_unknown(
                    context,
                    completion.error,
                    now=now,
                )
                return
            self._release_reservation(context.reservation_id)
            self._record_order_failure(context, completion.error)
            return
        result = completion.value
        if not isinstance(result, OpenPositionResult):
            self._release_reservation(context.reservation_id)
            self._record_order_failure(
                context,
                RuntimeError('broker_returned_no_result'),
            )
            return
        self._apply_filled_order(context, result=result)

    def _handle_unknown_lookup_completion(
        self,
        completion: BrokerTaskCompletion,
        *,
        now: datetime,
    ) -> None:
        reservation_id = str(completion.context)
        unknown = self._unknown_orders.get(reservation_id)
        if unknown is None:
            return
        unknown.lookup_pending = False
        unknown.attempts += 1
        unknown.next_lookup_at = now + self.unknown_lookup_interval
        if completion.error is not None:
            self.trade_journal.write(
                'order_confirmation_lookup_warning',
                {
                    'reservation_id': reservation_id,
                    'order_id': unknown.order_id,
                    'symbol': unknown.context.candidate.symbol,
                    'attempt': unknown.attempts,
                    'message': str(completion.error),
                },
            )
            return
        resolution = completion.value
        if not isinstance(resolution, UnknownOrderResolution):
            return
        if resolution.status == 'confirmed' and resolution.result is not None:
            self._unknown_orders.pop(reservation_id, None)
            self.trade_journal.write(
                'order_confirmation_recovered',
                {
                    'reservation_id': reservation_id,
                    'order_id': unknown.order_id,
                    'reference_id': unknown.reference_id,
                    'matched_by': resolution.matched_by,
                    'details': resolution.details,
                },
            )
            self._apply_filled_order(
                unknown.context,
                result=resolution.result,
            )
            return
        if resolution.status == 'rejected':
            self._unknown_orders.pop(reservation_id, None)
            self._release_reservation(reservation_id)
            self._record_order_failure(
                unknown.context,
                RuntimeError('broker_confirmed_order_rejected'),
            )
            return
        self.trade_journal.write(
            'order_confirmation_still_unknown',
            {
                'reservation_id': reservation_id,
                'order_id': unknown.order_id,
                'reference_id': unknown.reference_id,
                'symbol': unknown.context.candidate.symbol,
                'attempt': unknown.attempts,
                'matched_by': resolution.matched_by,
                'details': resolution.details,
            },
        )

    def _apply_filled_order(
        self,
        context: _OrderContext,
        *,
        result: OpenPositionResult,
    ) -> None:
        candidate = context.candidate
        plan = context.trade_plan
        self._unknown_orders.pop(context.reservation_id, None)
        self._release_reservation(context.reservation_id)
        self.trade_journal.write(
            'order_filled',
            {
                'reservation_id': context.reservation_id,
                'symbol': candidate.symbol,
                'position_id': result.position_id,
                'execution_result': result,
                'candidate': candidate,
                'trade_plan': plan,
            },
        )
        signal_price = candidate.snapshot.last
        executed_entry_price = result.executed_entry_price
        executable_entry_estimate = candidate.snapshot.executable_entry_price(
            candidate.signal.action
        )
        pnl_entry_price = (
            executed_entry_price
            if executed_entry_price is not None
            else executable_entry_estimate
        )
        adjusted_plan = self.risk_manager.adjust_trade_plan_to_entry_price(
            trade_plan=plan,
            entry_price=pnl_entry_price,
        )
        tracked_position = self.position_tracker.record_open_position(
            position_id=result.position_id,
            trade_plan=adjusted_plan,
            signal_price=signal_price,
            executable_entry_estimate=executable_entry_estimate,
            broker_entry_fill_price=executed_entry_price,
        )
        self.risk_manager.record_open_position(
            candidate.symbol,
            session_key=candidate.session_key,
        )
        identifier = pending_entry_id(candidate)
        if self.pending_entry_manager is not None and identifier is not None:
            self.pending_entry_manager.remove_by_id(identifier)
        if self.position_store is not None:
            try:
                self.position_store.save_open_position(tracked_position)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    'Position persistence save error | position_id=%s | '
                    'symbol=%s | error=%s',
                    tracked_position.position_id,
                    tracked_position.symbol,
                    exc,
                )
                self.trade_journal.write(
                    'position_persistence_error',
                    {
                        'symbol': tracked_position.symbol,
                        'position_id': tracked_position.position_id,
                        'position': tracked_position,
                        'message': str(exc),
                    },
                )
        self.trade_journal.write(
            'position_opened',
            {
                'reservation_id': context.reservation_id,
                'symbol': candidate.symbol,
                'position_id': result.position_id,
                'position': tracked_position,
                'candidate': candidate,
                'candidate_economics': context.economics,
                'effective_sl_tp': context.effective_sl_tp,
                'trade_plan': adjusted_plan,
                'original_trade_plan': plan,
                'adjusted_trade_plan': adjusted_plan,
                'signal_price': signal_price,
                'bid_at_entry': candidate.snapshot.bid,
                'ask_at_entry': candidate.snapshot.ask,
                'observed_spread_percent': spread_percent(candidate.snapshot),
                'broker_entry_fill_price': executed_entry_price,
                'executable_entry_estimate': executable_entry_estimate,
                'pnl_entry_price': pnl_entry_price,
                'entry_price_source': tracked_position.entry_price_source,
                'execution_slippage_percent': _slippage_percent(
                    planned_entry_price=signal_price,
                    effective_entry_price=pnl_entry_price,
                ),
                'instrument_profile': context.instrument_profile,
                'risk_profile': context.risk_profile,
            },
        )

    def _mark_confirmation_unknown(
        self,
        context: _OrderContext,
        exc: Exception,
        *,
        now: datetime,
    ) -> None:
        reservation = self._reservations.get(context.reservation_id)
        if reservation is not None:
            reservation.state = 'confirmation_unknown'
        order_id = order_id_from_confirmation_error(exc)
        self._unknown_orders[context.reservation_id] = _UnknownOrder(
            context=context,
            order_id=order_id,
            reference_id=None,
            attempts=0,
            first_unknown_at=now,
            next_lookup_at=now,
        )
        keep_pending_waiting(
            candidate=context.candidate,
            reason='order_confirmation_unknown',
            pending_entry_manager=self.pending_entry_manager,
            trade_journal=self.trade_journal,
        )
        self.trade_journal.write(
            'order_confirmation_unknown',
            {
                'reservation_id': context.reservation_id,
                'order_id': order_id,
                'reference_id': None,
                'symbol': context.candidate.symbol,
                'candidate': context.candidate,
                'trade_plan': context.trade_plan,
                'message': str(exc),
                'risk_reserved': True,
            },
        )

    def _record_order_failure(
        self,
        context: _OrderContext,
        exc: Exception,
    ) -> None:
        keep_pending_waiting(
            candidate=context.candidate,
            reason='candidate_execution_error',
            pending_entry_manager=self.pending_entry_manager,
            trade_journal=self.trade_journal,
        )
        logger.exception(
            'Candidate execution error | symbol=%s | action=%s | error=%s',
            context.candidate.symbol,
            context.candidate.signal.action,
            exc,
        )
        payload = {
            'reservation_id': context.reservation_id,
            'symbol': context.candidate.symbol,
            'candidate': context.candidate,
            'message': str(exc),
        }
        self.trade_journal.write('order_failed', payload)
        self.trade_journal.write('candidate_execution_error', payload)

    def _record_batch_error(
        self,
        batch: _CandidateBatch,
        exc: Exception,
    ) -> None:
        self.trade_journal.write(
            'candidate_execution_error',
            {
                'stage': 'account_equity',
                'batch_id': batch.batch_id,
                'symbols': [candidate.symbol for candidate in batch.candidates],
                'message': str(exc),
            },
        )

    def _reservation_allowed(self, candidate: TradeCandidate) -> bool:
        settings = self.risk_manager.settings
        pending = list(self._reservations.values())
        if self.risk_manager.open_positions + len(pending) >= settings.max_open_positions:
            return False
        symbol_pending = sum(
            1 for item in pending if item.symbol == candidate.symbol
        )
        current_symbol = self.risk_manager.open_positions_by_symbol.get(
            candidate.symbol,
            0,
        )
        if current_symbol + symbol_pending >= settings.max_open_positions_per_symbol:
            return False
        session_pending = sum(
            1 for item in pending if item.session_key == candidate.session_key
        )
        if (
            self.risk_manager.trades_for_session(candidate.session_key)
            + session_pending
            >= settings.max_trades_per_session
        ):
            return False
        return True

    def _reserve(
        self,
        candidate: TradeCandidate,
        *,
        now: datetime,
    ) -> _Reservation:
        reservation = _Reservation(
            reservation_id=f'open-reservation:{uuid4()}',
            symbol=candidate.symbol,
            session_key=candidate.session_key,
            created_at=now,
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def _release_reservation(self, reservation_id: str) -> None:
        self._reservations.pop(reservation_id, None)

    def _write_pending_risk_rejection(
        self,
        candidate: TradeCandidate,
    ) -> None:
        plan = TradePlan(
            approved=False,
            reason='pending_execution_capacity_reserved',
            symbol=candidate.symbol,
            side=candidate.signal.action,
        )
        self.trade_journal.write(
            'decision',
            {
                'symbol': candidate.symbol,
                'snapshot': candidate.snapshot,
                'candle': candidate.candle,
                'signal': candidate.signal,
                'candidate': candidate,
                'trade_plan': plan,
                'pending_open_reservations': len(self._reservations),
            },
        )
        invalidate_pending_candidate(
            candidate=candidate,
            reason='risk_reject:pending_execution_capacity_reserved',
            pending_entry_manager=self.pending_entry_manager,
            trade_journal=self.trade_journal,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
