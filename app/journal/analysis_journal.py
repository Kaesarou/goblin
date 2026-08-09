from datetime import datetime, timedelta, timezone
from typing import Any

from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.journal.analysis_ready_summary import AnalysisReadySummaryAggregator
from app.journal.journal_policy import (
    normalize_detail_level,
    should_write_to_debug_journal,
    should_write_to_errors_journal,
    should_write_to_trade_journal,
)
from app.journal.jsonl_journal import JsonlJournal

PARTIAL_SUMMARY_INTERVAL_MINUTES = 15
WRITE_PARTIAL_SUMMARY = True
ENTRY_DECISION_SCHEMA_VERSION = 2


class AnalysisJournal:
    def __init__(
        self,
        *,
        trade_journal: JsonlJournal,
        errors_journal: JsonlJournal,
        summary_path: str,
        detail_level: str = 'normal',
        debug_decisions_journal: JsonlJournal | None = None,
        partial_summary_path: str | None = None,
        partial_summary_interval_minutes: int = (
            PARTIAL_SUMMARY_INTERVAL_MINUTES
        ),
        write_partial_summary: bool = WRITE_PARTIAL_SUMMARY,
        run_id: str | None = None,
        strategy: str | None = None,
        profile: str | None = None,
    ):
        self.run_id = run_id
        self.trade_journal = trade_journal
        self.errors_journal = errors_journal
        self.debug_decisions_journal = debug_decisions_journal
        self.summary_path = summary_path
        self.partial_summary_path = partial_summary_path
        self.write_partial_summary = write_partial_summary
        self.detail_level = normalize_detail_level(detail_level)
        self.partial_summary_interval = timedelta(
            minutes=max(1, partial_summary_interval_minutes)
        )
        self._last_partial_summary_at = datetime.now(timezone.utc)
        self._session_state_by_symbol: dict[str, tuple[Any, ...]] = {}
        self.summary = AnalysisReadySummaryAggregator(
            run_id=run_id,
            strategy=strategy,
            profile=profile,
            journal_detail_level=self.detail_level,
        )

    def write(self, event_type: str, payload: dict[str, Any]) -> None:
        routed = self._normalize_event(event_type, payload)
        if routed is None:
            return
        routed_event_type, routed_payload = routed
        self.summary.record(routed_event_type, routed_payload)
        if should_write_to_errors_journal(routed_event_type):
            self.errors_journal.write(routed_event_type, routed_payload)
        if (
            self.debug_decisions_journal is not None
            and should_write_to_debug_journal(
                routed_event_type,
                routed_payload,
                self.detail_level,
            )
        ):
            self.debug_decisions_journal.write(
                routed_event_type,
                routed_payload,
            )
        if should_write_to_trade_journal(
            routed_event_type,
            routed_payload,
            self.detail_level,
        ):
            written = self.trade_journal.write(
                routed_event_type,
                routed_payload,
            )
            if written is False:
                self._record_trade_journal_write_failure(
                    routed_event_type,
                    routed_payload,
                )
        if routed_event_type == 'candidate_selection':
            self._write_counterfactual_entry_decisions(routed_payload)
        self._maybe_write_partial_summary()

    def _write_counterfactual_entry_decisions(
        self,
        payload: dict[str, Any],
    ) -> None:
        for evaluated, selection_outcome, selection_reason in (
            _evaluated_selection_items(payload)
        ):
            record = _entry_decision_record(
                evaluated=evaluated,
                selection_outcome=selection_outcome,
                selection_reason=selection_reason,
                strategy_profile=self.summary.profile,
            )
            if record is None:
                continue
            written = self.trade_journal.write('entry_decision', record)
            if written is False:
                self._record_trade_journal_write_failure(
                    'entry_decision',
                    record,
                )

    def record_raw_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        written: bool,
    ) -> None:
        if written:
            self.summary.record(event_type, payload)
            self._maybe_write_partial_summary()
            return
        self.write(
            'raw_journal_error',
            {
                'event_type': event_type,
                'symbol': payload.get('symbol'),
                'message': 'Raw journal event could not be written.',
            },
        )

    def runtime_metrics(self) -> dict[str, Any]:
        summary = self.summary.to_dict()
        return {
            'market_snapshots': summary['market_data']['snapshots'],
            'candles_closed': summary['market_data']['candles_closed'],
            'candidates': summary['decision_pipeline']['candidates_detected'],
            'selected': summary['decision_pipeline']['selection_selected'],
            'orders_submitted': summary['orders']['submitted'],
            'positions_opened': summary['positions']['opened'],
            'positions_closed': summary['positions']['closed'],
            'errors': summary['errors']['total'],
        }

    def finalize(self) -> dict[str, Any]:
        summary = self.summary.finalize()
        self.summary.write(self.summary_path)
        if self.partial_summary_path:
            self.summary.write(self.partial_summary_path)
        return summary

    def _record_trade_journal_write_failure(
        self,
        failed_event_type: str,
        payload: dict[str, Any],
    ) -> None:
        failure_payload = {
            'failed_event_type': failed_event_type,
            'symbol': payload.get('symbol'),
            'message': 'Trade journal event could not be written.',
        }
        self.summary.record('journal_write_error', failure_payload)
        self.errors_journal.write('journal_write_error', failure_payload)

    def _normalize_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        if event_type != 'session_state':
            return event_type, payload
        return self._session_state_change_event(payload)

    def _session_state_change_event(
        self,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        symbol = payload.get('symbol')
        session_decision = payload.get('session_decision')
        if symbol is None or session_decision is None:
            return 'session_state_changed', payload
        new_state = _session_state_name(session_decision)
        signature = (
            new_state,
            _attribute(session_decision, 'reason'),
            _attribute(session_decision, 'session_key'),
            _attribute(session_decision, 'session_active'),
            _attribute(session_decision, 'collect_snapshots'),
            _attribute(session_decision, 'new_entries_allowed'),
            _attribute(session_decision, 'force_close_required'),
        )
        previous_signature = self._session_state_by_symbol.get(str(symbol))
        if previous_signature == signature:
            return None
        self._session_state_by_symbol[str(symbol)] = signature
        return (
            'session_state_changed',
            {
                'symbol': symbol,
                'previous_state': (
                    previous_signature[0]
                    if previous_signature is not None
                    else None
                ),
                'new_state': new_state,
                'reason': _attribute(session_decision, 'reason'),
                'session_key': _attribute(
                    session_decision,
                    'session_key',
                ),
                'session_decision': session_decision,
            },
        )

    def _maybe_write_partial_summary(self) -> None:
        if not self.write_partial_summary or not self.partial_summary_path:
            return
        now = datetime.now(timezone.utc)
        if (
            now - self._last_partial_summary_at
            < self.partial_summary_interval
        ):
            return
        self.summary.write(self.partial_summary_path)
        self._last_partial_summary_at = now


def _entry_decision_record(
    *,
    evaluated: Any,
    selection_outcome: str,
    selection_reason: Any,
    strategy_profile: Any,
) -> dict[str, Any] | None:
    candidate = _attribute(evaluated, 'candidate')
    decision = _attribute(evaluated, 'entry_decision')
    if candidate is None or decision is None:
        return None
    signal = _attribute(candidate, 'signal')
    snapshot = _attribute(candidate, 'snapshot')
    economics = _attribute(evaluated, 'economics')
    effective_sl_tp = _attribute(evaluated, 'effective_sl_tp')
    tp_feasibility = _attribute(evaluated, 'tp_feasibility')
    outcome_probability = _attribute(
        evaluated,
        'outcome_probability',
    )
    market_context = _attribute(candidate, 'market_context')
    multi_timeframe_context = _attribute(
        candidate,
        'multi_timeframe_context',
    )
    spread_context = _attribute(market_context, 'spread')
    return {
        'schema_version': ENTRY_DECISION_SCHEMA_VERSION,
        'candidate_id': _attribute(candidate, 'candidate_id'),
        'origin_candidate_id': _attribute(candidate, 'origin_candidate_id'),
        'pending_entry_id': _attribute(candidate, 'pending_entry_id'),
        'candidate_timestamp': _attribute(snapshot, 'timestamp'),
        'symbol': _attribute(candidate, 'symbol'),
        'side': _attribute(signal, 'action'),
        'segment': _enum_value(_attribute(candidate, 'segment')),
        'entry_reference_price': _attribute(snapshot, 'last'),
        'bid': _attribute(snapshot, 'bid'),
        'ask': _attribute(snapshot, 'ask'),
        'last': _attribute(snapshot, 'last'),
        'spread': _snapshot_spread_percent(snapshot),
        'executable_entry_price': _executable_entry_price(
            snapshot,
            _attribute(signal, 'action'),
        ),
        'profile_key': _profile_key(
            effective_sl_tp,
            outcome_probability,
        ),
        'sl_tp_source': _attribute(effective_sl_tp, 'source'),
        'probability_score': _attribute(
            candidate,
            'probability_score',
        ),
        'base_score': _attribute(candidate, 'base_score'),
        'directional_score': _attribute(candidate, 'directional_score'),
        'market_context_score': _attribute(
            candidate,
            'market_context_score',
        ),
        'market_context_components': _attribute(
            candidate,
            'market_context_components',
        ),
        'multi_timeframe_score': _attribute(
            candidate,
            'multi_timeframe_score',
        ),
        'multi_timeframe_components': _attribute(
            candidate,
            'multi_timeframe_components',
        ),
        'tp_feasibility_score': _attribute(
            candidate,
            'tp_feasibility_score',
        ),
        'tp_feasibility_contribution': _attribute(
            candidate,
            'tp_feasibility_contribution',
        ),
        'movement_consumed_to_tp_ratio': _attribute(
            tp_feasibility,
            'movement_consumed_to_tp_ratio',
        ),
        'entry_freshness_score': _attribute(
            tp_feasibility,
            'entry_freshness_score',
        ),
        'effective_stop_loss_percent': _attribute(
            effective_sl_tp,
            'stop_loss_percent',
        ),
        'effective_take_profit_percent': _attribute(
            effective_sl_tp,
            'take_profit_percent',
        ),
        'estimated_total_cost_percent': _attribute(
            economics,
            'estimated_total_cost_percent',
        ),
        'expected_net_profit_percent': _attribute(
            economics,
            'expected_net_profit_percent',
        ),
        'touch_probability': _attribute(
            candidate,
            'touch_probability',
        ),
        'direction_probability': _attribute(
            candidate,
            'direction_probability',
        ),
        'tp_probability': _attribute(
            candidate,
            'tp_probability',
        ),
        'sl_probability': _attribute(
            candidate,
            'sl_probability',
        ),
        'neither_probability': _attribute(
            candidate,
            'neither_probability',
        ),
        'direction_break_even_probability': _attribute(
            candidate,
            'direction_break_even_probability',
        ),
        'direction_edge': _attribute(candidate, 'direction_edge'),
        'managed_protection_probability': _attribute(
            candidate,
            'managed_protection_probability',
        ),
        'managed_positive_probability': _attribute(
            candidate,
            'managed_positive_probability',
        ),
        'managed_expected_net_return_percent': _attribute(
            candidate,
            'managed_expected_net_return_percent',
        ),
        'managed_edge': _attribute(candidate, 'managed_edge'),
        'managed_outcome_model_version': _attribute(
            candidate,
            'managed_outcome_model_version',
        ),
        'observed_spread_percent': _snapshot_spread_percent(snapshot),
        'relative_spread_ratio': _attribute(
            spread_context,
            'relative_to_median',
        ),
        'relative_spread_percentile': _attribute(
            spread_context,
            'reference_percentile',
        ),
        'relative_spread_recent_change': _attribute(
            spread_context,
            'recent_change_ratio',
        ),
        'relative_spread_available': _attribute(
            spread_context,
            'available',
        ),
        'entry_route_action': _enum_value(_attribute(decision, 'action')),
        'entry_route_reason': _attribute(decision, 'reason'),
        'selection_outcome': selection_outcome,
        'selection_reason': selection_reason,
        'candidate': candidate,
        'market_context': market_context,
        'multi_timeframe_context': multi_timeframe_context,
        'candidate_economics': economics,
        'tp_feasibility': tp_feasibility,
        'outcome_probability': outcome_probability,
        'effective_sl_tp': effective_sl_tp,
        'entry_decision': decision,
        'market_context_version': _attribute(market_context, 'version'),
        'market_context_score_model_version': _attribute(
            _attribute(candidate, 'market_context_score_metadata'),
            'model_version',
        ),
        'multi_timeframe_model_version': _attribute(
            multi_timeframe_context,
            'model_version',
        ),
        'multi_timeframe_score_model_version': _attribute(
            _attribute(candidate, 'multi_timeframe_score_metadata'),
            'model_version',
        ),
        'tp_feasibility_model_version': _attribute(
            tp_feasibility,
            'model_version',
        ),
        'outcome_probability_model_version': _attribute(
            candidate,
            'outcome_probability_model_version',
        ),
        'selection_policy_version': MANAGED_SELECTION_POLICY_VERSION,
        'entry_route_model_version': _attribute(
            decision,
            'model_version',
        ),
        'strategy_profile': strategy_profile,
    }


def _profile_key(
    effective_sl_tp: Any,
    outcome_probability: Any,
) -> Any:
    calibrated_profile = _attribute(
        outcome_probability,
        'profile_key',
    )
    if calibrated_profile:
        return calibrated_profile
    return _attribute(effective_sl_tp, 'source')


def _evaluated_selection_items(payload: dict[str, Any]):
    selected = payload.get('selected_evaluated_candidates') or []
    rejected = payload.get('rejected_evaluated_candidates') or []
    for evaluated in selected:
        yield evaluated, 'selected', None
    for rejection in rejected:
        evaluated = _attribute(rejection, 'evaluated_candidate')
        if evaluated is not None:
            yield evaluated, 'rejected', _attribute(rejection, 'reason')


def _session_state_name(session_decision: Any) -> str:
    if not _attribute(session_decision, 'session_active'):
        return 'closed'
    if _attribute(session_decision, 'force_close_required'):
        return 'force_close'
    if not _attribute(session_decision, 'new_entries_allowed'):
        return 'no_new_entries'
    return 'active'


def _enum_value(value: Any) -> Any:
    return _attribute(value, 'value') or value


def _attribute(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _snapshot_spread_percent(snapshot: Any) -> float | None:
    bid = _attribute(snapshot, 'bid')
    ask = _attribute(snapshot, 'ask')
    if bid is None or ask is None:
        return None
    midpoint = (float(bid) + float(ask)) / 2
    if midpoint <= 0:
        return None
    return round((float(ask) - float(bid)) / midpoint * 100, 4)


def _executable_entry_price(snapshot: Any, side: Any) -> float | None:
    normalized_side = str(side or '').upper()
    if normalized_side == 'BUY':
        value = _attribute(snapshot, 'ask')
    elif normalized_side == 'SELL':
        value = _attribute(snapshot, 'bid')
    else:
        return None
    return None if value is None else float(value)
