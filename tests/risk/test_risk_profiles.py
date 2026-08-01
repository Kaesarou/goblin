from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_balanced_profile_uses_expected_trade_cooldown():
    cooldown = BalancedStrategyConfig().equity_us.risk.trade_cooldown

    assert cooldown.after_take_profit_minutes == 30
    assert cooldown.after_initial_stop_minutes == 45
    assert cooldown.after_protected_breakeven_minutes == 15
    assert cooldown.after_protected_trailing_minutes == 15
    assert cooldown.after_stale_exit_minutes == 15
    assert cooldown.after_session_force_close_minutes == 15
    assert cooldown.after_manual_or_broker_close_minutes == 15
    assert cooldown.after_unknown_confirmed_close_minutes == 15
