from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.candidate_selector import (
    EvaluatedCandidateSelectionResult,
    RejectedEvaluatedCandidateSelection,
    rank_evaluated_trade_candidates,
)
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_SELECTION_POLICY_VERSION,
)
from app.execution.sl_tp_profile import EffectiveSlTpResolver
from app.risk.models import TradePlan
from app.runtime.async_candidate_execution import (
    AsyncCandidateExecutionCoordinator,
    _as_utc,
    _CandidateBatch,
    _OrderContext,
    _UnknownOrder,
)
from app.runtime.broker_queries import (
    order_id_from_confirmation_error,
    reference_id_from_confirmation_error,
    submitted_at_from_confirmation_error,
)
from app.runtime.candidate_flow import (
    _candidate_selection_result_from_evaluated,
    _evaluate_risk_manager,
    _resolve_runtime_effective_sl_tp,
    apply_trade_cooldown_guard,
    evaluate_candidate_batch,
    select_evaluated_trade_candidates_with_strategy_profile,
    select_trade_candidates_with_strategy_profile,
)
from app.runtime.managed_v2_shadow import (
    annotate_managed_v2_shadow_shared_rejections,
    apply_managed_v2_shadow_selection,
    candidate_selection_contract_metadata,
)
from app.runtime.pending_candidate_lifecycle import (
    invalidate_pending_candidate,
    keep_pending_waiting,
    reconcile_pending_selection_rejections,
)

logger = logging.getLogger(__name__)


class ResilientCandidateExecutionCoordinator(
    AsyncCandidateExecutionCoordinator
):
    def submit_candidates(
        self,
        candidates,
        *,
        now: datetime,
    ) -> None:
        if not candidates:
            return
        batch = _CandidateBatch(
            batch_id=f'candidate-batch:{uuid4()}',
            candidates=tuple(candidates),
            created_at=_as_utc(now),
        )
        self._queued_batches.append(batch)
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
            if self.cooldown_guard is not None:
                candidates = apply_trade_cooldown_guard(
                    candidates=candidates,
                    risk_manager=self.risk_manager,
                    cooldown_guard=self.cooldown_guard,
                    trade_journal=self.trade_journal,
                )
            selection_result = select_trade_candidates_with_strategy_profile(
                candidates,
                self.risk_manager,
                self.strategy_profile,
            )
            ranked_candidates = selection_result.selected_candidates
        else:
            evaluated = evaluate_candidate_batch(
                candidates=candidates,
                equity=equity,
                economics_estimator=self.candidate_economics_estimator,
                risk_manager=self.risk_manager,
                trade_journal=self.trade_journal,
            )
            allowed_evaluated, cooldown_rejections = (
                self._filter_evaluated_candidates_by_cooldown(
                    evaluated,
                    now=now,
                )
            )
            shadow_selection = None
            if self.strategy_profile is not None:
                allowed_evaluated, shadow_selection = (
                    apply_managed_v2_shadow_selection(
                        evaluated_candidates=allowed_evaluated,
                        risk_manager=self.risk_manager,
                        strategy_profile=self.strategy_profile,
                    )
                )
                cooldown_rejections = (
                    annotate_managed_v2_shadow_shared_rejections(
                        cooldown_rejections
                    )
                )
                self.trade_journal.write(
                    'candidate_managed_v2_shadow_selection',
                    {
                        'selection_policy_version': (
                            MANAGED_V2_SELECTION_POLICY_VERSION
                        ),
                        'selected_evaluated_candidates': (
                            shadow_selection.selected_candidates
                        ),
                        'rejected_evaluated_candidates': [
                            *cooldown_rejections,
                            *shadow_selection.rejected_candidates,
                        ],
                    },
                )
            evaluated_selection = (
                EvaluatedCandidateSelectionResult(
                    rank_evaluated_trade_candidates(allowed_evaluated),
                    [],
                )
                if self.strategy_profile is None
                else select_evaluated_trade_candidates_with_strategy_profile(
                    allowed_evaluated,
                    self.risk_manager,
                    self.strategy_profile,
                )
            )
            reconcile_pending_selection_rejections(
                rejected_candidates=evaluated_selection.rejected_candidates,
                pending_entry_manager=self.pending_entry_manager,
                trade_journal=self.trade_journal,
            )
            complete_selection = EvaluatedCandidateSelectionResult(
                selected_candidates=evaluated_selection.selected_candidates,
                rejected_candidates=[
                    *cooldown_rejections,
                    *evaluated_selection.rejected_candidates,
                ],
            )
            selected_evaluated = complete_selection.selected_candidates
            rejected_evaluated = complete_selection.rejected_candidates
            selection_result = _candidate_selection_result_from_evaluated(
                complete_selection
            )
            ranked_candidates = selection_result.selected_candidates

        self.trade_journal.write(
            'candidate_selection',
            {
                **candidate_selection_contract_metadata(
                    managed_evaluation_enabled=(
                        self.candidate_economics_estimator is not None
                    ),
                ),
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

    def _filter_evaluated_candidates_by_cooldown(
        self,
        evaluated_candidates: list[EvaluatedTradeCandidate],
        *,
        now: datetime,
    ) -> tuple[
        list[EvaluatedTradeCandidate],
        list[RejectedEvaluatedCandidateSelection],
    ]:
        if self.cooldown_guard is None:
            return evaluated_candidates, []

        actual_now = _as_utc(now)
        self.cooldown_guard.store.delete_expired(actual_now)
        by_candidate_id = {
            id(item.candidate): item for item in evaluated_candidates
        }
        cooldown_result = self.cooldown_guard.filter_candidates(
            candidates=[item.candidate for item in evaluated_candidates],
            risk_manager=self.risk_manager,
            now=actual_now,
        )
        selected = [
            by_candidate_id[id(candidate)]
            for candidate in cooldown_result.selected_candidates
        ]
        rejected: list[RejectedEvaluatedCandidateSelection] = []
        for blocked in cooldown_result.rejected_candidates:
            evaluated = by_candidate_id[id(blocked.candidate)]
            decision = blocked.decision
            reason = decision.reason or 'trade_cooldown_active'
            plan = TradePlan(
                approved=False,
                reason=reason,
                symbol=blocked.candidate.symbol,
                side=blocked.candidate.signal.action,
            )
            self.trade_journal.write(
                'cooldown_blocked',
                {
                    'symbol': blocked.candidate.symbol,
                    'snapshot': blocked.candidate.snapshot,
                    'candle': blocked.candidate.candle,
                    'signal': blocked.candidate.signal,
                    'candidate': blocked.candidate,
                    'evaluated_candidate': evaluated,
                    'candidate_economics': evaluated.economics,
                    'effective_sl_tp': evaluated.effective_sl_tp,
                    'tp_feasibility': evaluated.tp_feasibility,
                    'outcome_probability': (
                        evaluated.outcome_probability
                    ),
                    'entry_decision': evaluated.entry_decision,
                    'trade_plan': plan,
                    'cooldown': decision.active_cooldown,
                    'cooldown_remaining_seconds': decision.remaining_seconds,
                    'lock_scope': decision.lock_scope,
                    'blocked_sides': list(decision.blocked_sides),
                    'instrument_profile': (
                        self.risk_manager.instrument_profile_for(
                            blocked.candidate.symbol
                        )
                    ),
                    'risk_profile': self.risk_manager.risk_profile_for(
                        blocked.candidate.symbol
                    ),
                },
            )
            rejected.append(
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated,
                    reason=reason,
                    selection_threshold_source='trade_cooldown',
                )
            )
        return selected, rejected

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
        reference_id = reference_id_from_confirmation_error(exc)
        submitted_at = submitted_at_from_confirmation_error(
            exc,
            context.submitted_at,
        )
        unknown_context = _OrderContext(
            reservation_id=context.reservation_id,
            candidate=context.candidate,
            trade_plan=context.trade_plan,
            economics=context.economics,
            effective_sl_tp=context.effective_sl_tp,
            instrument_profile=context.instrument_profile,
            risk_profile=context.risk_profile,
            submitted_at=submitted_at,
        )
        self._unknown_orders[context.reservation_id] = _UnknownOrder(
            context=unknown_context,
            order_id=order_id,
            reference_id=reference_id,
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
                'reference_id': reference_id,
                'symbol': context.candidate.symbol,
                'candidate': context.candidate,
                'trade_plan': context.trade_plan,
                'submitted_at': submitted_at,
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
        logger.error(
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
