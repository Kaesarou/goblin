from collections import Counter
from typing import Any

from app.journal.daily_summary import DailySummaryAggregator

ANALYSIS_READY_SUMMARY_SCHEMA_VERSION = 14


class AnalysisReadySummaryAggregator(DailySummaryAggregator):
    """Summary schema focused on post-run calibration."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.validated_accepted_total = 0
        self.tp_hard_rejection_components = Counter()
        self.effective_sl_tp_sources = Counter()
        self.market_context_score_buckets = Counter()
        self.multi_timeframe_score_buckets = Counter()
        self.tp_feasibility_score_buckets = Counter()
        self.tp_feasibility_contribution_buckets = Counter()
        self.entry_freshness_score_buckets = Counter()
        self.probability_score_buckets = Counter()
        self.touch_probability_buckets = Counter()
        self.direction_probability_buckets = Counter()
        self.direction_edge_buckets = Counter()
        self.outcome_probability_profiles = Counter()
        self.outcome_probability_training_domain = Counter()
        self.entry_horizon_rejections_by_profile = Counter()
        self.entry_horizon_rejections_by_reason = Counter()
        self.managed_stop_updates_by_type = Counter()
        self.managed_v2_evaluated_by_segment = Counter()
        self.managed_v2_gate_outcomes_by_segment: dict[str, Counter] = {}
        self.managed_v2_gate_rejections_by_reason = Counter()
        self.managed_v2_shadow_selected_by_segment = Counter()
        self.managed_v2_shadow_rejected_by_segment = Counter()
        self.managed_v2_shadow_rejections_by_reason = Counter()
        self.managed_v2_missing_features = Counter()
        self.managed_v2_shadow_policy_versions = Counter()

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        super().record(event_type, payload)
        if event_type == 'market_data_event_received':
            self.market_data_received += 1
        elif event_type == 'market_price_changed':
            self.market_snapshots += 1
            self.market_data_accepted += 1
        elif event_type == 'candle_finalized':
            self.candles_closed += 1
        elif event_type == 'market_batch_validated':
            batch = payload.get('batch')
            accepted = _attribute(batch, 'accepted') or {}
            self.validated_accepted_total += len(accepted)
        elif event_type == 'candidate_tp_feasibility':
            self._record_candidate_scores(payload)
        elif event_type == 'candidate_outcome_probability':
            self._record_outcome_probabilities(payload)
        elif event_type == 'entry_horizon_rejected':
            self.entry_horizon_rejections_by_reason[
                str(payload.get('reason') or 'unknown')
            ] += 1
            self.entry_horizon_rejections_by_profile[
                str(payload.get('profile_key') or 'unknown')
            ] += 1
        elif event_type == 'managed_stop_updated':
            self.managed_stop_updates_by_type[
                str(payload.get('protection_type') or 'unknown')
            ] += 1
        elif event_type == 'candidate_managed_v2_shadow':
            self._record_managed_v2_estimates(payload)
        elif event_type == 'candidate_managed_v2_shadow_selection':
            self._record_managed_v2_shadow_selection(payload)

    def _record_decision(self, payload):
        approved_before = self.risk_approved
        rejected_before = self.risk_rejected
        super()._record_decision(payload)
        if payload.get('candidate') is None:
            self.risk_approved = approved_before
            self.risk_rejected = rejected_before

    def _record_candidate_scores(self, payload: dict[str, Any]) -> None:
        for item in _as_list(payload.get('evaluated_candidates')):
            analysis = _attribute(item, 'tp_feasibility')
            candidate = _attribute(item, 'candidate')
            effective_sl_tp = _attribute(item, 'effective_sl_tp')
            for component in _as_list(
                _attribute(analysis, 'hard_rejection_components')
            ):
                self.tp_hard_rejection_components[str(component)] += 1
            source = _attribute(effective_sl_tp, 'source') or _attribute(
                analysis,
                'sl_tp_source',
            )
            if source:
                self.effective_sl_tp_sources[str(source)] += 1
            self._record_bucket(
                self.market_context_score_buckets,
                _attribute(candidate, 'market_context_score'),
                width=5.0,
            )
            self._record_bucket(
                self.multi_timeframe_score_buckets,
                _attribute(candidate, 'multi_timeframe_score'),
                width=2.0,
            )
            self._record_bucket(
                self.tp_feasibility_score_buckets,
                _attribute(analysis, 'feasibility_score'),
                width=10.0,
            )
            self._record_bucket(
                self.tp_feasibility_contribution_buckets,
                _attribute(analysis, 'score_contribution'),
                width=5.0,
            )
            self._record_bucket(
                self.entry_freshness_score_buckets,
                _attribute(analysis, 'entry_freshness_score'),
                width=10.0,
            )

    def _record_outcome_probabilities(
        self,
        payload: dict[str, Any],
    ) -> None:
        for item in _as_list(payload.get('evaluated_candidates')):
            estimate = _attribute(item, 'outcome_probability')
            candidate = _attribute(item, 'candidate')
            profile = _attribute(estimate, 'profile_key')
            if profile:
                self.outcome_probability_profiles[str(profile)] += 1
            in_training_domain = bool(
                _attribute(estimate, 'in_training_domain')
            )
            domain_key = (
                'in_training_domain'
                if in_training_domain
                else 'outside_training_domain'
            )
            self.outcome_probability_training_domain[domain_key] += 1
            self._record_bucket(
                self.probability_score_buckets,
                _attribute(candidate, 'probability_score'),
                width=10.0,
            )
            self._record_bucket(
                self.touch_probability_buckets,
                _attribute(candidate, 'touch_probability'),
                width=0.10,
            )
            self._record_bucket(
                self.direction_probability_buckets,
                _attribute(candidate, 'direction_probability'),
                width=0.10,
            )
            self._record_bucket(
                self.direction_edge_buckets,
                _attribute(candidate, 'direction_edge'),
                width=0.05,
            )

    def _record_bucket(
        self,
        counter: Counter,
        value: Any,
        *,
        width: float,
    ) -> None:
        if value is None:
            counter['unavailable'] += 1
            return
        numeric = float(value)
        lower = (numeric // width) * width
        upper = lower + width
        counter[f'[{lower:.2f},{upper:.2f})'] += 1

    def _record_managed_v2_estimates(
        self,
        payload: dict[str, Any],
    ) -> None:
        for item in _as_list(payload.get('evaluated_candidates')):
            candidate = _attribute(item, 'candidate')
            metadata = _attribute(candidate, 'managed_v2_metadata') or {}
            segment = _managed_v2_segment(candidate, metadata)
            if segment == 'unknown':
                continue
            self.managed_v2_evaluated_by_segment[segment] += 1
            gate_outcome = str(
                _attribute(metadata, 'gate_outcome') or 'unknown'
            )
            counter = self.managed_v2_gate_outcomes_by_segment.setdefault(
                segment,
                Counter(),
            )
            counter[gate_outcome] += 1
            gate_reason = _attribute(metadata, 'gate_rejection_reason')
            if gate_reason:
                self.managed_v2_gate_rejections_by_reason[
                    str(gate_reason)
                ] += 1
            for component in ('opportunity', 'path', 'economics'):
                component_metadata = _attribute(metadata, component) or {}
                for feature in _as_list(
                    _attribute(component_metadata, 'missing_features')
                ):
                    self.managed_v2_missing_features[
                        f'{segment}:{component}:{feature}'
                    ] += 1

    def _record_managed_v2_shadow_selection(
        self,
        payload: dict[str, Any],
    ) -> None:
        version = payload.get('selection_policy_version')
        if version:
            self.managed_v2_shadow_policy_versions[str(version)] += 1
        for item in _as_list(payload.get('selected_evaluated_candidates')):
            candidate = _attribute(item, 'candidate')
            segment = _managed_v2_segment(
                candidate,
                _attribute(candidate, 'managed_v2_metadata') or {},
            )
            self.managed_v2_shadow_selected_by_segment[segment] += 1
        for item in _as_list(payload.get('rejected_evaluated_candidates')):
            evaluated = _attribute(item, 'evaluated_candidate') or item
            candidate = _attribute(evaluated, 'candidate')
            segment = _managed_v2_segment(
                candidate,
                _attribute(candidate, 'managed_v2_metadata') or {},
            )
            self.managed_v2_shadow_rejected_by_segment[segment] += 1
            reason = _attribute(item, 'reason') or 'unknown_rejection'
            self.managed_v2_shadow_rejections_by_reason[str(reason)] += 1

    def to_dict(self) -> dict[str, Any]:
        summary = super().to_dict()
        summary['schema_version'] = ANALYSIS_READY_SUMMARY_SCHEMA_VERSION
        market_data = summary['market_data']
        trading_snapshots = market_data.get('accepted', 0)
        market_data['trading_snapshots_processed'] = trading_snapshots
        accepted_total = self.validated_accepted_total or trading_snapshots
        market_data['accepted'] = accepted_total
        market_data['context_snapshots_accepted'] = max(
            0,
            accepted_total - trading_snapshots,
        )
        summary['score_contributions'] = {
            'market_context': dict(self.market_context_score_buckets),
            'multi_timeframe': dict(self.multi_timeframe_score_buckets),
            'tp_feasibility_score': dict(self.tp_feasibility_score_buckets),
            'tp_feasibility_contribution': dict(
                self.tp_feasibility_contribution_buckets
            ),
            'entry_freshness_score': dict(self.entry_freshness_score_buckets),
            'probability_score': dict(self.probability_score_buckets),
            'touch_probability': dict(self.touch_probability_buckets),
            'direction_probability': dict(
                self.direction_probability_buckets
            ),
            'direction_edge': dict(self.direction_edge_buckets),
        }
        summary['tp_feasibility'] = {
            'hard_rejection_components': dict(
                self.tp_hard_rejection_components
            ),
        }
        summary['effective_sl_tp'] = {
            'by_source': dict(self.effective_sl_tp_sources),
        }
        summary['outcome_probability'] = {
            'by_profile': dict(self.outcome_probability_profiles),
            'training_domain': dict(
                self.outcome_probability_training_domain
            ),
        }
        summary['entry_horizon'] = {
            'rejections_by_reason': dict(
                self.entry_horizon_rejections_by_reason
            ),
            'rejections_by_profile': dict(
                self.entry_horizon_rejections_by_profile
            ),
        }
        summary['managed_stops'] = {
            'updates_by_type': dict(self.managed_stop_updates_by_type),
        }
        summary['managed_v2'] = {
            'evaluated_by_segment': dict(
                self.managed_v2_evaluated_by_segment
            ),
            'gate_outcomes_by_segment': {
                segment: dict(counter)
                for segment, counter
                in self.managed_v2_gate_outcomes_by_segment.items()
            },
            'gate_rejections_by_reason': dict(
                self.managed_v2_gate_rejections_by_reason
            ),
            'shadow_selected_by_segment': dict(
                self.managed_v2_shadow_selected_by_segment
            ),
            'shadow_rejected_by_segment': dict(
                self.managed_v2_shadow_rejected_by_segment
            ),
            'shadow_rejections_by_reason': dict(
                self.managed_v2_shadow_rejections_by_reason
            ),
            'missing_features': dict(self.managed_v2_missing_features),
            'selection_policy_versions': dict(
                self.managed_v2_shadow_policy_versions
            ),
        }
        return summary


def _attribute(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return list(value)


def _managed_v2_segment(candidate: Any, metadata: Any) -> str:
    segment = _attribute(metadata, 'segment') or _attribute(
        candidate,
        'segment',
    )
    return str(_attribute(segment, 'value') or segment or 'unknown')
