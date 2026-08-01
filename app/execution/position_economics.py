from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionPnl:
    gross_pnl: float
    gross_pnl_percent: float
    explicit_costs_deducted: float
    explicit_costs_deducted_percent: float
    net_pnl: float
    net_pnl_percent: float


def calculate_position_pnl(
    *,
    side: str,
    amount: float,
    entry_price: float,
    exit_price: float,
    explicit_cost: float,
    explicit_cost_percent: float,
) -> PositionPnl:
    """Price-to-price P&L with only explicit post-trade costs deducted."""
    if entry_price <= 0:
        raise ValueError(f"Invalid P&L entry price: {entry_price}")
    if exit_price <= 0:
        raise ValueError(f"Invalid P&L exit price: {exit_price}")
    if side == "BUY":
        gross_percent = (exit_price - entry_price) / entry_price * 100
    elif side == "SELL":
        gross_percent = (entry_price - exit_price) / entry_price * 100
    else:
        raise ValueError(f"Unsupported position side: {side}")

    gross = amount * gross_percent / 100
    return PositionPnl(
        gross_pnl=gross,
        gross_pnl_percent=gross_percent,
        explicit_costs_deducted=explicit_cost,
        explicit_costs_deducted_percent=explicit_cost_percent,
        net_pnl=gross - explicit_cost,
        net_pnl_percent=gross_percent - explicit_cost_percent,
    )
