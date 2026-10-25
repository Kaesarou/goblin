"""Fail-closed dispatch-time authority for V3 BUYs.

A planner decision is not authority to mutate a broker account. Re-evaluate
the immutable intent against the *current* ledger book, exact equity and
unresolved, durable open reservations at the point of broker submission.
Normal Point-M signal/size calculation and reduce-only exits are untouched.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.v3.models import IntentPurpose


EQUITY_MAX_AGE_SECONDS = 180.0
EXPOSURE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class EntryRiskVerdict:
    allowed: bool
    reason: str
    committed_notional: float = 0.0
    reserved_notional: float = 0.0


def validate_entry_dispatch(
    *, intent, book, equity, observed_at, now: datetime, config,
    reservations: dict[str, tuple[str, float]],  # action -> (symbol, USD notional)
) -> EntryRiskVerdict:
    if intent.side.upper() != "BUY" or intent.purpose not in (
        IntentPurpose.INITIAL_ENTRY, IntentPurpose.REENTRY
    ):
        return EntryRiskVerdict(False, "invalid_new_risk_intent")
    if not isinstance(equity, (int, float)) or isinstance(equity, bool) or (
        not math.isfinite(equity) or equity <= 0
    ):
        return EntryRiskVerdict(False, "broker_equity_unavailable")
    if (not isinstance(observed_at, datetime) or observed_at.tzinfo is None
            or now.tzinfo is None):
        return EntryRiskVerdict(False, "broker_equity_timestamp_unavailable")
    age = (now.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds()
    if not -5.0 <= age <= EQUITY_MAX_AGE_SECONDS:
        return EntryRiskVerdict(False, "broker_equity_stale")
    notional = intent.notional
    if not isinstance(notional, (int, float)) or isinstance(notional, bool) or (
        not math.isfinite(notional) or notional <= 0
    ):
        return EntryRiskVerdict(False, "invalid_entry_notional")

    symbol = intent.symbol.strip().upper()
    active = book.active_for_symbol(symbol)
    inventories = tuple(book.portfolio(equity=equity).inventories)
    committed = sum(float(i.total_notional) for i in inventories)
    reserved = sum(v for _, v in reservations.values())
    symbol_reserved = sum(v for s, v in reservations.values() if s == symbol)
    symbol_committed = float(active.total_notional) if active else 0.0

    # Never allow additional risk while any pre-existing symbol or aggregate
    # exposure is already beyond the safety boundary. SELLs remain independent.
    if (committed + reserved) / equity > config.max_portfolio_exposure_pct + EXPOSURE_TOLERANCE:
        return EntryRiskVerdict(False, "existing_portfolio_exposure_breach", committed, reserved)
    if any(
        (float(i.total_notional) + sum(v for s, v in reservations.values() if s == i.symbol))
        / equity > config.max_symbol_exposure_pct + EXPOSURE_TOLERANCE
        for i in inventories
    ):
        return EntryRiskVerdict(False, "existing_symbol_exposure_breach", committed, reserved)
    if (intent.purpose == IntentPurpose.INITIAL_ENTRY):
        if active is not None:
            return EntryRiskVerdict(False, "stale_initial_inventory_exists", committed, reserved)
        active_count = len(inventories) + len({
            s for s, _ in reservations.values()
            if not any(i.symbol == s for i in inventories)
        })
        if active_count >= config.max_inventories:
            return EntryRiskVerdict(False, "max_inventories", committed, reserved)
    else:
        if active is None or intent.inventory_id != active.inventory_id:
            return EntryRiskVerdict(False, "stale_reentry_inventory", committed, reserved)
        expected_fills = intent.metadata.get("inventory_entry_fill_count")
        if expected_fills is not None and expected_fills != active.entry_fill_count:
            return EntryRiskVerdict(False, "stale_reentry_fill_count", committed, reserved)
        inflight_count = sum(1 for s, _ in reservations.values() if s == symbol)
        if active.entry_fill_count + inflight_count >= config.max_entry_fills:
            return EntryRiskVerdict(False, "max_entry_fills", committed, reserved)

    if (symbol_committed + symbol_reserved + notional) / equity > (
        config.max_symbol_exposure_pct + EXPOSURE_TOLERANCE
    ):
        return EntryRiskVerdict(False, "symbol_exposure_cap", committed, reserved)
    if (committed + reserved + notional) / equity > (
        config.max_portfolio_exposure_pct + EXPOSURE_TOLERANCE
    ):
        return EntryRiskVerdict(False, "portfolio_exposure_cap", committed, reserved)
    return EntryRiskVerdict(True, "approved", committed, reserved)
