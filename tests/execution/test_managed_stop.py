from app.execution.managed_stop import (
    ManagedProtectionType,
    calculate_buy_managed_stop,
    calculate_sell_managed_stop,
)


def test_buy_trailing_is_rejected_when_net_locked_is_negative():
    decision = calculate_buy_managed_stop(
        entry_price=100.0,
        current_stop_loss=98.0,
        highest_price=101.0,
        lowest_price=100.0,
        breakeven_stop_enabled=False,
        breakeven_trigger_percent=0.0,
        breakeven_buffer_percent=0.0,
        trailing_stop_enabled=True,
        trailing_stop_trigger_percent=1.0,
        trailing_stop_distance_percent=0.8,
        trailing_stop_net_buffer_percent=0.1,
        estimated_explicit_cost_percent=0.35,
    )
    assert decision.stop_loss == 98.0
    assert decision.protection_type is None
    assert decision.metadata is None


def test_buy_trailing_is_accepted_when_net_locked_covers_buffer():
    decision = calculate_buy_managed_stop(
        entry_price=100.0,
        current_stop_loss=100.45,
        highest_price=102.0,
        lowest_price=100.0,
        breakeven_stop_enabled=False,
        breakeven_trigger_percent=0.0,
        breakeven_buffer_percent=0.0,
        trailing_stop_enabled=True,
        trailing_stop_trigger_percent=1.0,
        trailing_stop_distance_percent=0.8,
        trailing_stop_net_buffer_percent=0.1,
        estimated_explicit_cost_percent=0.35,
    )
    assert decision.stop_loss == 101.184
    assert decision.protection_type is ManagedProtectionType.TRAILING
    assert decision.metadata['gross_locked_percent'] == 1.184
    assert decision.metadata['estimated_net_locked_percent'] == 0.834


def test_sell_trailing_is_accepted_when_net_locked_covers_buffer():
    decision = calculate_sell_managed_stop(
        entry_price=100.0,
        current_stop_loss=99.55,
        highest_price=100.0,
        lowest_price=98.0,
        breakeven_stop_enabled=False,
        breakeven_trigger_percent=0.0,
        breakeven_buffer_percent=0.0,
        trailing_stop_enabled=True,
        trailing_stop_trigger_percent=1.0,
        trailing_stop_distance_percent=0.8,
        trailing_stop_net_buffer_percent=0.1,
        estimated_explicit_cost_percent=0.35,
    )
    assert decision.stop_loss == 98.784
    assert decision.protection_type is ManagedProtectionType.TRAILING
    assert decision.metadata['gross_locked_percent'] == 1.216
    assert decision.metadata['estimated_net_locked_percent'] == 0.866


def test_net_breakeven_locks_costs_plus_configured_net_buffer():
    decision = calculate_buy_managed_stop(
        entry_price=100.0,
        current_stop_loss=98.0,
        highest_price=101.0,
        lowest_price=100.0,
        breakeven_stop_enabled=True,
        breakeven_trigger_percent=0.9,
        breakeven_buffer_percent=0.05,
        trailing_stop_enabled=False,
        trailing_stop_trigger_percent=0.0,
        trailing_stop_distance_percent=0.0,
        trailing_stop_net_buffer_percent=0.1,
        estimated_explicit_cost_percent=0.3,
    )
    assert decision.stop_loss == 100.35
    assert decision.protection_type is ManagedProtectionType.BREAKEVEN
    assert decision.metadata['protection_type'] == 'breakeven'
    assert decision.metadata['estimated_net_locked_percent'] == 0.05
