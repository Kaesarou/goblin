import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.journal.journal_policy import (
    decision_reason,
    decision_side,
    decision_symbol,
    is_hold_decision,
    is_rejected_decision,
    normalize_detail_level,
)
from app.journal.serialization import serialize_value

DAILY_SUMMARY_SCHEMA_VERSION = 10


class DailySummaryAggregator:
    def __init__(
        self,
        *,
        run_id=None,
        strategy=None,
        profile=None,
        journal_detail_level='normal',
        started_at=None,
        max_items=10,
    ):
        self.run_id = run_id
        self.strategy = strategy
        self.profile = profile
        self.journal_detail_level = normalize_detail_level(
            journal_detail_level
        )
        self.started_at = started_at or datetime.now(timezone.utc)
        self.ended_at = None
        self.max_items = max_items
        self.event_counts = Counter()
        self.hold_reasons = Counter()
        self.rejection_reasons = Counter()
        self.error_types = Counter()
        self.candidates_by_symbol = Counter()
        self.orders_by_symbol = Counter()
        self.cooldowns_by_symbol = Counter()
        self.entry_route_reasons = Counter()
        self.market_data_rejections_by_reason = Counter()
        self.market_context_regimes = Counter()
        self.timeframe_bars_by_timeframe = Counter()
        self.timeframe_incomplete_by_timeframe = Counter()
        self.timeframe_partial_by_timeframe = Counter()
        self.candle_gaps_by_symbol = Counter()
        self.mtf_maturity_by_timeframe: dict[str, Counter] = {}
        self.mtf_ready_alignments = Counter()
        self.mtf_inclusive_alignments = Counter()
        self.opening_range_statuses = Counter()
        self.pending_invalidations_by_reason = Counter()
        self.pending_invalidations_by_symbol = Counter()
        self.pending_confirmation_blocks_by_reason = Counter()
        self.pending_confirmation_blocks_by_symbol = Counter()
        self.pending_blocked_spread_values: list[float] = []
        self.pending_confirmation_blocks_before_first_candle = 0
        self.pending_ids: set[str] = set()
        self.pending_retest_ids: set[str] = set()
        self.pending_confirmed_ids: set[str] = set()
        self.candidate_ids: set[str] = set()
        self.total_decisions = self.hold_total = self.candidate_total = 0
        self.selection_selected_total = self.selection_rejected_total = 0
        self.strategy_rejected_total = 0
        self.orders_submitted = self.orders_failed = self.orders_filled = 0
        self.positions_opened = self.positions_closed = 0
        self.positions_restored = self.force_closed = 0
        self.broker_failures = 0
        self.market_snapshots = self.candles_closed = 0
        self.market_data_received = self.market_data_accepted = 0
        self.market_data_quarantined = self.market_data_rejected = 0
        self.route_ready_for_selection = 0
        self.route_wait_for_retest = 0
        self.route_skip = 0
        self.risk_approved = self.risk_rejected = 0
        self.orders_from_pending = 0
        self.pending_position_ids = set()
        self.gross_pnl = 0.0
        self.explicit_costs_deducted = 0.0
        self.net_pnl = 0.0
        self.pnl_from_pending = 0.0
        self.pnl_economics_complete = True
        self.pnl_by_exit_price_source: dict[str, dict[str, float | int]] = {}
        self.pnl_by_close_reason: dict[str, dict[str, float | int]] = {}
        self.closed_position_calculations: list[dict[str, Any]] = []
        self.mfe_total = 0.0
        self.mae_total = 0.0
        self.mfe_mae_count = 0
        self.top_rejected_candidates = []
        self.selected_candidates = []
        self.session_transitions = []
        self.errors = []

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_counts[event_type] += 1
        if event_type == 'market_snapshot_received':
            self.market_data_received += 1
        elif event_type == 'market_snapshot':
            self.market_snapshots += 1
            self.market_data_accepted += 1
        elif event_type == 'market_data_quarantined':
            self.market_data_quarantined += 1
            self._record_market_data_reason(payload)
        elif event_type == 'market_data_rejected':
            self.market_data_rejected += 1
            self._record_market_data_reason(payload)
        elif event_type == 'market_context_built':
            regime = _attribute(payload.get('market_context'), 'regime')
            value = _attribute(regime, 'value') or regime or 'unknown'
            self.market_context_regimes[str(value)] += 1
        elif event_type == 'candle_closed':
            self.candles_closed += 1
        elif event_type == 'timeframe_bar_closed':
            self._record_timeframe_bar(
                payload,
                self.timeframe_bars_by_timeframe,
            )
        elif event_type == 'timeframe_bar_incomplete':
            self._record_timeframe_bar(
                payload,
                self.timeframe_incomplete_by_timeframe,
            )
        elif event_type == 'timeframe_bar_partial':
            self._record_timeframe_bar(
                payload,
                self.timeframe_partial_by_timeframe,
            )
        elif event_type == 'candle_gap_detected':
            symbol = payload.get('symbol') or _attribute(
                payload.get('gap'),
                'symbol',
            )
            if symbol:
                self.candle_gaps_by_symbol[str(symbol)] += 1
        elif event_type == 'multi_timeframe_context_built':
            self._record_multi_timeframe_context(payload)
        elif event_type == 'candidate_detected':
            self._record_candidate_detected(payload)
        elif event_type == 'candidate_selection':
            self._record_candidate_selection(payload)
        elif event_type == 'decision':
            self._record_decision(payload)
        elif event_type.startswith('pending_entry_'):
            self._record_pending_event(event_type, payload)
        elif event_type == 'cooldown_blocked':
            self._record_cooldown(payload)
        elif event_type == 'order_submitted':
            self.orders_submitted += 1
            self._increment_symbol_counter(self.orders_by_symbol, payload)
            if _is_pending_candidate(payload.get('candidate')):
                self.orders_from_pending += 1
        elif event_type == 'order_failed':
            self.orders_failed += 1
            self._increment_symbol_counter(self.orders_by_symbol, payload)
        elif event_type == 'order_filled':
            self.orders_filled += 1
            self._increment_symbol_counter(self.orders_by_symbol, payload)
        elif event_type == 'position_opened':
            self.positions_opened += 1
            if _is_pending_candidate(payload.get('candidate')):
                position_id = payload.get('position_id') or _attribute(
                    payload.get('position'),
                    'position_id',
                )
                if position_id:
                    self.pending_position_ids.add(str(position_id))
        elif event_type in {
            'position_close_confirmed',
            'position_reconciled_closed',
        }:
            self.positions_closed += 1
            self._record_closed_position_pnl(payload)
        elif event_type == 'position_restored':
            self.positions_restored += 1
        elif event_type in {
            'force_close_requested',
            'force_close_completed',
            'force_close',
        }:
            self.force_closed += 1
        elif event_type == 'session_state_changed':
            self._append_limited(
                self.session_transitions,
                self._session_transition_summary(payload),
            )
        elif self._is_error_event(event_type):
            self._record_error(event_type, payload)

    def _record_market_data_reason(self, payload):
        validation = payload.get('validation')
        for reason in _as_list(_attribute(validation, 'reasons')):
            self.market_data_rejections_by_reason[str(reason)] += 1

    def _record_timeframe_bar(self, payload, counter: Counter) -> None:
        timeframe = payload.get('timeframe')
        if timeframe is None:
            bar = payload.get('timeframe_bar')
            timeframe = (
                _attribute(_attribute(bar, 'timeframe'), 'name')
                or _attribute(bar, 'timeframe')
            )
        if timeframe is not None:
            counter[str(timeframe).lower()] += 1

    def _record_multi_timeframe_context(self, payload) -> None:
        context = payload.get('multi_timeframe_context')
        ready_alignment = _attribute(context, 'ready_alignment')
        inclusive_alignment = _attribute(
            context,
            'alignment_including_provisional',
        )
        self.mtf_ready_alignments[
            str(_enum_value(ready_alignment) or 'unknown')
        ] += 1
        self.mtf_inclusive_alignments[
            str(_enum_value(inclusive_alignment) or 'unknown')
        ] += 1
        maturities = _attribute(context, 'maturity_by_timeframe') or {}
        for timeframe, maturity in maturities.items():
            counter = self.mtf_maturity_by_timeframe.setdefault(
                str(timeframe),
                Counter(),
            )
            counter[
                str(_enum_value(maturity) or 'unavailable')
            ] += 1
        opening_ranges = _attribute(context, 'opening_ranges')
        windows = _attribute(opening_ranges, 'windows') or {}
        for minutes, window in windows.items():
            status = _attribute(window, 'status')
            status_value = _enum_value(status) or 'unknown'
            self.opening_range_statuses[
                f'{minutes}m:{status_value}'
            ] += 1

    def _record_candidate_detected(self, payload):
        self.candidate_total += 1
        candidate = payload.get('candidate')
        candidate_id = payload.get('candidate_id') or _attribute(
            candidate,
            'candidate_id',
        )
        if candidate_id:
            self.candidate_ids.add(str(candidate_id))
        symbol = _attribute(candidate, 'symbol') or payload.get('symbol')
        if symbol:
            self.candidates_by_symbol[str(symbol)] += 1

    def _record_candidate_selection(self, payload):
        selected = _as_list(payload.get('selected_candidates'))
        rejected = _as_list(payload.get('rejected_candidates'))
        selected_source = (
            _as_list(payload.get('selected_evaluated_candidates'))
            or selected
        )
        rejected_source = (
            _as_list(payload.get('rejected_evaluated_candidates'))
            or rejected
        )
        self.selection_selected_total += len(selected_source)
        self.selection_rejected_total += len(rejected_source)
        for item in selected_source:
            self._record_entry_route(item)
            self._append_limited(
                self.selected_candidates,
                self._selected_candidate_summary(item),
            )
        for item in rejected_source:
            self._record_entry_route(item)
            reason = _attribute(item, 'reason') or 'unknown_rejection'
            self.rejection_reasons[str(reason)] += 1
            self._remember_top_rejected_candidate(item, str(reason))

    def _record_entry_route(self, item):
        evaluated = _attribute(item, 'evaluated_candidate') or item
        decision = _attribute(evaluated, 'entry_decision')
        if decision is None:
            return
        action = _enum_value(_attribute(decision, 'action'))
        reason = _attribute(decision, 'reason') or 'unknown_entry_route'
        self.entry_route_reasons[str(reason)] += 1
        if action == 'ready_for_selection':
            self.route_ready_for_selection += 1
        elif action == 'wait_for_retest':
            self.route_wait_for_retest += 1
        elif action == 'skip':
            self.route_skip += 1

    def _record_decision(self, payload):
        self.total_decisions += 1
        trade_plan = payload.get('trade_plan')
        if trade_plan is not None:
            if bool(_attribute(trade_plan, 'approved')):
                self.risk_approved += 1
            else:
                self.risk_rejected += 1
        reason = decision_reason(payload) or 'unknown_decision_reason'
        if is_hold_decision('decision', payload):
            self.hold_total += 1
            self.hold_reasons[reason] += 1
        elif is_rejected_decision('decision', payload):
            self.strategy_rejected_total += 1
            self.rejection_reasons[reason] += 1
            self._remember_top_rejected_decision(payload, reason)

    def _record_pending_event(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        pending_id = payload.get('pending_entry_id') or _attribute(
            payload.get('pending_entry'),
            'pending_entry_id',
        )
        if pending_id:
            normalized_id = str(pending_id)
            self.pending_ids.add(normalized_id)
            if event_type == 'pending_entry_retest_detected':
                self.pending_retest_ids.add(normalized_id)
            elif event_type == 'pending_entry_confirmed':
                self.pending_confirmed_ids.add(normalized_id)

        reason = str(payload.get('reason') or 'unknown')
        symbol = payload.get('symbol') or _attribute(
            payload.get('pending_entry'),
            'symbol',
        )
        if event_type == 'pending_entry_confirmation_blocked':
            self.pending_confirmation_blocks_by_reason[reason] += 1
            if symbol:
                self.pending_confirmation_blocks_by_symbol[
                    str(symbol)
                ] += 1
            spread = payload.get('spread_percent')
            if spread is not None:
                self.pending_blocked_spread_values.append(float(spread))
            if int(payload.get('observed_candles') or 0) <= 1:
                self.pending_confirmation_blocks_before_first_candle += 1
            return

        if event_type != 'pending_entry_invalidated':
            return
        self.pending_invalidations_by_reason[reason] += 1
        if symbol:
            self.pending_invalidations_by_symbol[str(symbol)] += 1

    def finalize(self):
        self.ended_at = datetime.now(timezone.utc)
        return self.to_dict()

    def write(self, path):
        summary_path = Path(path)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(
                serialize_value(self.to_dict()),
                ensure_ascii=False,
                indent=2,
            )
            + '\n',
            encoding='utf-8',
        )

    def to_dict(self):
        closed_total = sum(self.timeframe_bars_by_timeframe.values())
        incomplete_total = sum(
            self.timeframe_incomplete_by_timeframe.values()
        )
        partial_total = sum(
            self.timeframe_partial_by_timeframe.values()
        )
        gap_total = sum(self.candle_gaps_by_symbol.values())
        blocked_spread_count = len(self.pending_blocked_spread_values)
        return {
            'schema_version': DAILY_SUMMARY_SCHEMA_VERSION,
            'run_id': self.run_id,
            'strategy': self.strategy,
            'profile': self.profile,
            'journal_detail_level': self.journal_detail_level,
            'started_at': self.started_at,
            'ended_at': self.ended_at,
            'events': {'by_type': dict(self.event_counts)},
            'market_data': {
                'snapshots': self.market_snapshots,
                'candles_closed': self.candles_closed,
                'received': self.market_data_received,
                'accepted': self.market_data_accepted,
                'quarantined': self.market_data_quarantined,
                'rejected': self.market_data_rejected,
                'rejections_by_reason': dict(
                    self.market_data_rejections_by_reason
                ),
            },
            'market_context': {
                'by_regime': dict(self.market_context_regimes)
            },
            'multi_timeframe': {
                'closed_total': closed_total,
                'closed_by_timeframe': dict(
                    self.timeframe_bars_by_timeframe
                ),
                'incomplete_total': incomplete_total,
                'incomplete_by_timeframe': dict(
                    self.timeframe_incomplete_by_timeframe
                ),
                'partial_total': partial_total,
                'partial_by_timeframe': dict(
                    self.timeframe_partial_by_timeframe
                ),
                'gap_total': gap_total,
                'gaps_by_symbol': dict(self.candle_gaps_by_symbol),
                'maturity_by_timeframe': {
                    timeframe: dict(counter)
                    for timeframe, counter
                    in self.mtf_maturity_by_timeframe.items()
                },
                'ready_alignment': dict(self.mtf_ready_alignments),
                'alignment_including_provisional': dict(
                    self.mtf_inclusive_alignments
                ),
                'opening_range_statuses': dict(
                    self.opening_range_statuses
                ),
            },
            'entry_routing': {
                'ready_for_selection': self.route_ready_for_selection,
                'wait_for_retest': self.route_wait_for_retest,
                'skip': self.route_skip,
                'by_reason': dict(self.entry_route_reasons),
            },
            'decision_pipeline': {
                'unique_candidates': len(self.candidate_ids),
                'candidates_detected': self.candidate_total,
                'selection_selected': self.selection_selected_total,
                'selection_rejected': self.selection_rejected_total,
                'risk_approved': self.risk_approved,
                'risk_rejected': self.risk_rejected,
                'orders_submitted': self.orders_submitted,
                'orders_filled': self.orders_filled,
                'orders_failed': self.orders_failed,
            },
            'strategy_decisions': {
                'total': self.total_decisions,
                'hold_total': self.hold_total,
                'rejected_total': self.strategy_rejected_total,
            },
            'hold_reasons': dict(self.hold_reasons),
            'rejections': {
                'total': sum(self.rejection_reasons.values()),
                'by_reason': dict(self.rejection_reasons),
                'top_rejected_candidates': (
                    self.top_rejected_candidates
                ),
            },
            'selected_candidates': self.selected_candidates,
            'orders': {
                'submitted': self.orders_submitted,
                'filled': self.orders_filled,
                'failed': self.orders_failed,
                'by_symbol': dict(self.orders_by_symbol),
                'from_pending': self.orders_from_pending,
            },
            'positions': {
                'opened': self.positions_opened,
                'closed': self.positions_closed,
                'restored': self.positions_restored,
                'force_closed': self.force_closed,
            },
            'pending_entries': {
                'unique': len(self.pending_ids),
                'registered_events': self.event_counts[
                    'pending_entry_registered'
                ],
                'confirmed_unique': len(self.pending_confirmed_ids),
                'confirmed_events': self.event_counts[
                    'pending_entry_confirmed'
                ],
                'expired_events': self.event_counts[
                    'pending_entry_expired'
                ],
                'invalidated_events': self.event_counts[
                    'pending_entry_invalidated'
                ],
                'updated_events': self.event_counts[
                    'pending_entry_updated'
                ],
                'retest_unique': len(self.pending_retest_ids),
                'retest_events': self.event_counts[
                    'pending_entry_retest_detected'
                ],
                'confirmation_blocked_events': self.event_counts[
                    'pending_entry_confirmation_blocked'
                ],
                'invalidations_by_reason': dict(
                    self.pending_invalidations_by_reason
                ),
                'invalidations_by_symbol': dict(
                    self.pending_invalidations_by_symbol
                ),
                'confirmation_blocks_by_reason': dict(
                    self.pending_confirmation_blocks_by_reason
                ),
                'confirmation_blocks_by_symbol': dict(
                    self.pending_confirmation_blocks_by_symbol
                ),
                'confirmation_blocks_before_first_candle': (
                    self.pending_confirmation_blocks_before_first_candle
                ),
                'blocked_spread_observations': {
                    'count': blocked_spread_count,
                    'average': (
                        round(
                            sum(self.pending_blocked_spread_values)
                            / blocked_spread_count,
                            6,
                        )
                        if blocked_spread_count
                        else None
                    ),
                    'maximum': (
                        round(
                            max(self.pending_blocked_spread_values),
                            6,
                        )
                        if blocked_spread_count
                        else None
                    ),
                },
            },
            'pnl': {
                'gross_total': round(self.gross_pnl, 4),
                'explicit_costs_deducted': round(
                    self.explicit_costs_deducted,
                    4,
                ),
                'net_total': (
                    round(self.net_pnl, 4)
                    if self.pnl_economics_complete
                    else None
                ),
                'economics_complete': self.pnl_economics_complete,
                'from_pending': round(self.pnl_from_pending, 4),
                'by_exit_price_source': self._rounded_pnl_breakdown(
                    self.pnl_by_exit_price_source
                ),
                'by_close_reason': self._rounded_pnl_breakdown(
                    self.pnl_by_close_reason
                ),
                'average_mfe_percent': (
                    round(self.mfe_total / self.mfe_mae_count, 4)
                    if self.mfe_mae_count
                    else None
                ),
                'average_mae_percent': (
                    round(self.mae_total / self.mfe_mae_count, 4)
                    if self.mfe_mae_count
                    else None
                ),
                'position_calculations': self.closed_position_calculations,
            },
            'cooldown': {
                'blocked_total': sum(self.cooldowns_by_symbol.values()),
                'by_symbol': dict(self.cooldowns_by_symbol),
            },
            'runtime': {
                'session_transitions': self.session_transitions
            },
            'errors': {
                'total': sum(self.error_types.values()),
                'by_type': dict(self.error_types),
                'samples': self.errors,
            },
        }

    def _record_cooldown(self, payload):
        symbol = payload.get('symbol') or _attribute(
            payload.get('candidate'),
            'symbol',
        )
        if symbol:
            self.cooldowns_by_symbol[str(symbol)] += 1
        reason = (
            payload.get('reason')
            or _attribute(payload.get('trade_plan'), 'reason')
            or 'cooldown_blocked'
        )
        self.rejection_reasons[str(reason)] += 1

    def _record_closed_position_pnl(self, payload):
        closed = payload.get('closed_position')
        gross = _attribute(closed, 'gross_pnl')
        if gross is None:
            self.pnl_economics_complete = False
            return
        gross = float(gross)
        self.gross_pnl += gross
        cost = _attribute(closed, 'explicit_costs_deducted')
        net = _attribute(closed, 'net_pnl')
        if cost is None or net is None:
            self.pnl_economics_complete = False
            return
        cost = float(cost)
        net = float(net)
        self.explicit_costs_deducted += cost
        self.net_pnl += net
        exit_source = str(
            _enum_value(_attribute(closed, 'exit_price_source'))
            or 'unavailable'
        )
        close_reason = str(
            _enum_value(_attribute(closed, 'close_reason'))
            or 'unknown'
        )
        self._add_pnl_breakdown(
            self.pnl_by_exit_price_source,
            exit_source,
            gross=gross,
            cost=cost,
            net=net,
        )
        self._add_pnl_breakdown(
            self.pnl_by_close_reason,
            close_reason,
            gross=gross,
            cost=cost,
            net=net,
        )
        mfe = _attribute(closed, 'mfe_percent')
        mae = _attribute(closed, 'mae_percent')
        if mfe is not None and mae is not None:
            self.mfe_total += float(mfe)
            self.mae_total += float(mae)
            self.mfe_mae_count += 1
        self.closed_position_calculations.append(
            {
                'position_id': _attribute(closed, 'position_id'),
                'symbol': _attribute(closed, 'symbol'),
                'side': _attribute(closed, 'side'),
                'close_reason': close_reason,
                'signal_price': _attribute(closed, 'signal_price'),
                'executable_entry_estimate': _attribute(
                    closed,
                    'executable_entry_estimate',
                ),
                'broker_entry_fill_price': _attribute(
                    closed,
                    'broker_entry_fill_price',
                ),
                'pnl_entry_price': _attribute(closed, 'pnl_entry_price'),
                'entry_price_source': _enum_value(
                    _attribute(closed, 'entry_price_source')
                ),
                'detection_last_execution_price': _attribute(
                    closed,
                    'detection_last_execution_price',
                ),
                'executable_exit_estimate': _attribute(
                    closed,
                    'executable_exit_estimate',
                ),
                'broker_exit_fill_price': _attribute(
                    closed,
                    'broker_exit_fill_price',
                ),
                'pnl_exit_price': _attribute(closed, 'pnl_exit_price'),
                'exit_price_source': exit_source,
                'bid_at_detection': _attribute(closed, 'bid_at_detection'),
                'ask_at_detection': _attribute(closed, 'ask_at_detection'),
                'observed_spread_percent': _attribute(
                    closed,
                    'observed_spread_percent',
                ),
                'gross_pnl': gross,
                'explicit_costs_deducted': cost,
                'net_pnl': net,
                'mfe_percent': mfe,
                'mae_percent': mae,
            }
        )
        position_id = _attribute(closed, 'position_id')
        if (
            position_id is not None
            and str(position_id) in self.pending_position_ids
        ):
            self.pnl_from_pending += float(net)
            self.pending_position_ids.discard(str(position_id))

    @staticmethod
    def _add_pnl_breakdown(
        target: dict[str, dict[str, float | int]],
        key: str,
        *,
        gross: float,
        cost: float,
        net: float,
    ) -> None:
        item = target.setdefault(
            key,
            {'count': 0, 'gross': 0.0, 'explicit_costs': 0.0, 'net': 0.0},
        )
        item['count'] = int(item['count']) + 1
        item['gross'] = float(item['gross']) + gross
        item['explicit_costs'] = float(item['explicit_costs']) + cost
        item['net'] = float(item['net']) + net

    @staticmethod
    def _rounded_pnl_breakdown(
        value: dict[str, dict[str, float | int]],
    ) -> dict[str, dict[str, float | int]]:
        return {
            key: {
                'count': int(item['count']),
                'gross': round(float(item['gross']), 4),
                'explicit_costs': round(float(item['explicit_costs']), 4),
                'net': round(float(item['net']), 4),
            }
            for key, item in value.items()
        }

    def _record_error(self, event_type, payload):
        self.error_types[event_type] += 1
        if event_type.startswith('broker_'):
            self.broker_failures += 1
        self._append_limited(
            self.errors,
            {
                'event_type': event_type,
                'symbol': payload.get('symbol'),
                'message': payload.get('message'),
            },
        )

    def _remember_top_rejected_decision(self, payload, reason):
        self._append_ranked(
            self.top_rejected_candidates,
            {
                'symbol': decision_symbol(payload),
                'side': decision_side(payload),
                'score': _candidate_score(payload.get('candidate')),
                'reason': reason,
            },
        )

    def _remember_top_rejected_candidate(self, item, reason):
        summary = _candidate_summary(
            _candidate_from_selection_item(item)
        )
        summary['reason'] = reason
        self._append_ranked(self.top_rejected_candidates, summary)

    def _selected_candidate_summary(self, item):
        return _candidate_summary(_candidate_from_selection_item(item))

    def _session_transition_summary(self, payload):
        return {
            'symbol': payload.get('symbol'),
            'from': payload.get('previous_state'),
            'to': payload.get('new_state'),
            'reason': payload.get('reason'),
            'session_key': payload.get('session_key'),
        }

    def _increment_symbol_counter(self, counter, payload):
        if payload.get('symbol'):
            counter[str(payload['symbol'])] += 1

    def _append_limited(self, items, item):
        if len(items) < self.max_items:
            items.append(item)

    def _append_ranked(self, items, item):
        items.append(item)
        items.sort(
            key=lambda value: value.get('score') or 0.0,
            reverse=True,
        )
        del items[self.max_items:]

    def _is_error_event(self, event_type):
        return (
            event_type == 'error'
            or event_type.endswith('_error')
            or event_type.endswith('_warning')
            or event_type.startswith('broker_')
        )


def _is_pending_candidate(candidate):
    return bool(_attribute(candidate, 'pending_entry_id'))


def _candidate_from_selection_item(item):
    direct = _attribute(item, 'candidate')
    if direct is not None:
        return direct
    evaluated = _attribute(item, 'evaluated_candidate')
    if evaluated is not None:
        return _attribute(evaluated, 'candidate')
    return item


def _candidate_summary(candidate):
    signal = _attribute(candidate, 'signal')
    return {
        'candidate_id': _attribute(candidate, 'candidate_id'),
        'origin_candidate_id': _attribute(
            candidate,
            'origin_candidate_id',
        ),
        'pending_entry_id': _attribute(candidate, 'pending_entry_id'),
        'symbol': _attribute(candidate, 'symbol'),
        'side': _attribute(signal, 'action'),
        'score': _candidate_score(candidate),
        'reason': _attribute(candidate, 'rank_reason'),
    }


def _candidate_score(candidate):
    score = _attribute(candidate, 'probability_score')
    if score is None:
        score = _attribute(candidate, 'score')
    return None if score is None else float(score)


def _enum_value(value):
    return _attribute(value, 'value') or value


def _attribute(value, name):
    if value is None:
        return None
    return (
        value.get(name)
        if isinstance(value, dict)
        else getattr(value, name, None)
    )


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return list(value)
