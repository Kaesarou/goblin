from dataclasses import replace
from datetime import UTC, datetime

from app.v3.close_recovery import (
    _CloseContext,
    _ReconciledCloseQuantity,
    confirmed_economics_attributable,
    reconciled_reduction_attributable,
)
from app.v3.live_execution import _migration_units_close, _units_close
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent


def _context(*, pre_close_units: float | None) -> _CloseContext:
    return _CloseContext(
        action_id="close:p1",
        intent=OrderIntent(
            intent_id="close", purpose=IntentPurpose.PROFIT_EXIT, symbol="AAPL",
            side="SELL", notional=84.0, created_at=datetime(2026, 9, 1, tzinfo=UTC),
            execution_style=ExecutionStyle.MARKET, reduce_only=True,
        ),
        inventory_id="inv", position_id="p1", trigger_price=105.0,
        requested_units=0.84, full_close=False, pre_close_units=pre_close_units,
    )


def _quantity(*, confident: bool) -> _ReconciledCloseQuantity:
    return _ReconciledCloseQuantity(
        action_id="close:p1", inventory_id="inv", position_id="p1",
        reconciled_book_units=0.84, broker_units=0.16,
        entry_price_basis=100.0, attribution_confident=confident,
    )


def test_legacy_close_never_promotes_uncertain_attribution_from_late_economics():
    context = _context(pre_close_units=None)
    comparators = dict(units_close=_units_close, migration_units_close=_migration_units_close)

    # Legacy ledgers tolerate approximate entry units during initial attribution.
    assert reconciled_reduction_attributable(
        context, previous_units=1.0003, reconciled_book_units=0.8403,
        **comparators,
    )
    assert not confirmed_economics_attributable(
        context, _quantity(confident=False), executed_units=0.84, **comparators,
    )
    assert confirmed_economics_attributable(
        context, _quantity(confident=True), executed_units=0.84, **comparators,
    )


def test_modern_close_corrects_historical_false_flag_only_with_matching_quantities():
    context = _context(pre_close_units=1.0)
    quantity = _quantity(confident=False)
    comparators = dict(units_close=_units_close, migration_units_close=_migration_units_close)

    assert reconciled_reduction_attributable(
        context, previous_units=1.0, reconciled_book_units=0.84,
        **comparators,
    )
    assert not reconciled_reduction_attributable(
        context, previous_units=1.1, reconciled_book_units=0.84,
        **comparators,
    )
    assert confirmed_economics_attributable(
        context, quantity, executed_units=0.84, **comparators,
    )
    assert not confirmed_economics_attributable(
        context, replace(quantity, broker_units=0.15), executed_units=0.84,
        **comparators,
    )
    assert not confirmed_economics_attributable(
        context, quantity, executed_units=0.839, **comparators,
    )
