from typing import Any

DETAIL_LEVELS = {'minimal', 'normal', 'debug', 'full'}

RAW_EVENT_TYPES = frozenset({'market_snapshot', 'candle_closed'})

ERROR_EVENT_TYPES = frozenset(
    {
        'error',
        'broker_error',
        'broker_timeout',
        'broker_authorization_error',
        'candidate_execution_error',
        'position_persistence_error',
        'position_reconciliation_warning',
        'position_restore_warning',
        'raw_journal_error',
        'journal_write_error',
    }
)

DEBUG_ONLY_EVENT_TYPES = frozenset({'candidate_ranking'})

HIGH_VOLUME_RUNTIME_EVENT_TYPES = frozenset(
    {
        'market_data_event_received',
        'market_data_event_ignored',
        'rest_control_snapshot',
        'position_reconciliation_grace',
        'position_reconciliation_suspect_updated',
    }
)

MINIMAL_TRADE_EVENT_TYPES = frozenset(
    {
        'runtime_started',
        'runtime_stopped',
        'runtime_interrupted',
        'session_started',
        'session_trades_reset',
        'session_state_changed',
        'session_heartbeat',
        'order_submitted',
        'order_failed',
        'order_filled',
        'order_confirmation_unknown',
        'order_confirmation_recovered',
        'order_confirmation_manual_intervention_required',
        'position_opened',
        'position_updated',
        'position_close_requested',
        'position_close_submitted',
        'position_close_confirmation_pending',
        'position_close_confirmed',
        'position_close_submission_unknown',
        'position_close_rejected',
        'position_close_confirmation_delayed',
        'position_close_manual_intervention_required',
        'position_restored',
        'position_reconciliation_suspect',
        'position_reconciliation_recovered',
        'position_reconciled_closed',
        'force_close_requested',
        'force_close_completed',
        'force_close',
        'cooldown_blocked',
        'trade_cooldown_registered',
        'symbol_lock_registered',
        'symbol_lock_expired',
        'pending_entry_registered',
        'pending_entry_updated',
        'pending_entry_retest_detected',
        'pending_entry_confirmation_blocked',
        'pending_entry_confirmed',
        'pending_entry_invalidated',
        'pending_entry_expired',
        'rest_control_completed',
        'rest_position_fallback_completed',
        'decision_window_finalized',
        'decision_window_late_symbol',
    }
)


def normalize_detail_level(detail_level: str | None) -> str:
    normalized = (detail_level or '').strip().lower()
    if normalized not in DETAIL_LEVELS:
        allowed = ', '.join(sorted(DETAIL_LEVELS))
        raise ValueError(
            f'Unsupported journal detail level: {detail_level!r}. '
            f'Expected one of: {allowed}'
        )
    return normalized


def should_write_to_trade_journal(
    event_type: str,
    payload: dict[str, Any],
    detail_level: str | None,
) -> bool:
    level = normalize_detail_level(detail_level)
    if event_type in RAW_EVENT_TYPES or event_type in ERROR_EVENT_TYPES:
        return False
    if event_type == 'session_state':
        return False
    if is_hold_decision(event_type, payload):
        return False
    if event_type in HIGH_VOLUME_RUNTIME_EVENT_TYPES:
        return level == 'full'
    if event_type in DEBUG_ONLY_EVENT_TYPES:
        return level in {'debug', 'full'}
    if level == 'minimal':
        return event_type in MINIMAL_TRADE_EVENT_TYPES
    return True


def should_write_to_debug_journal(
    event_type: str,
    payload: dict[str, Any],
    detail_level: str | None,
) -> bool:
    level = normalize_detail_level(detail_level)
    if level not in {'debug', 'full'}:
        return False
    return (
        event_type == 'decision'
        or event_type in DEBUG_ONLY_EVENT_TYPES
        or event_type in HIGH_VOLUME_RUNTIME_EVENT_TYPES
    )


def should_write_to_errors_journal(event_type: str) -> bool:
    return event_type in ERROR_EVENT_TYPES


def is_hold_decision(event_type: str, payload: dict[str, Any]) -> bool:
    if event_type != 'decision':
        return False
    return _upper(_attribute(payload.get('signal'), 'action')) == 'HOLD'


def is_rejected_decision(event_type: str, payload: dict[str, Any]) -> bool:
    if event_type != 'decision':
        return False
    return _attribute(payload.get('trade_plan'), 'approved') is False


def decision_reason(payload: dict[str, Any]) -> str | None:
    plan_reason = _attribute(payload.get('trade_plan'), 'reason')
    if plan_reason:
        return str(plan_reason)
    signal_reason = _attribute(payload.get('signal'), 'reason')
    if signal_reason:
        return str(signal_reason)
    return None


def decision_symbol(payload: dict[str, Any]) -> str | None:
    symbol = payload.get('symbol')
    if symbol:
        return str(symbol)
    candidate_symbol = _attribute(payload.get('candidate'), 'symbol')
    return str(candidate_symbol) if candidate_symbol else None


def decision_side(payload: dict[str, Any]) -> str | None:
    signal = payload.get('signal') or _attribute(payload.get('candidate'), 'signal')
    side = _attribute(signal, 'action')
    if side:
        return str(side)
    plan_side = _attribute(payload.get('trade_plan'), 'side')
    return str(plan_side) if plan_side else None


def _attribute(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _upper(value: Any) -> str | None:
    return None if value is None else str(value).upper()
