import pytest

from app.execution.position_economics import calculate_position_pnl


def test_executable_prices_deduct_only_explicit_costs_after_buy_fill():
    pnl = calculate_position_pnl(
        side="BUY",
        amount=1_000.0,
        entry_price=100.1,
        exit_price=100.4,
        explicit_cost=1.0,
        explicit_cost_percent=0.1,
    )

    assert pnl.gross_pnl == pytest.approx(2.997003)
    assert pnl.net_pnl == pytest.approx(1.997003)
    assert pnl.net_pnl_percent == pytest.approx(0.1997003)


def test_executable_prices_deduct_only_explicit_costs_after_sell_fill():
    pnl = calculate_position_pnl(
        side="SELL",
        amount=1_000.0,
        entry_price=99.9,
        exit_price=99.6,
        explicit_cost=1.0,
        explicit_cost_percent=0.1,
    )

    assert pnl.gross_pnl == pytest.approx(3.003003)
    assert pnl.net_pnl == pytest.approx(2.003003)
    assert pnl.net_pnl_percent == pytest.approx(0.2003003)
