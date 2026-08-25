from __future__ import annotations

from dataclasses import dataclass

from app.v3.models import DecisionReason


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: DecisionReason | None = None
    approved_exposure_pct: float = 0.0
    projected_symbol_exposure_pct: float | None = None
    projected_portfolio_exposure_pct: float | None = None


class InventoryRiskPolicy:
    """Bound risk while preserving Passivbot-style quantity cropping.

    A request that exceeds remaining exposure is reduced to the remaining budget;
    it is not rejected wholesale unless no positive budget remains. Hard limits
    therefore remain strict without turning a sizing boundary into a new signal.
    """

    def __init__(self, config) -> None:
        self.config = config

    def allow_initial_entry(self, portfolio, *, requested_exposure_pct: float) -> RiskDecision:
        if portfolio.active_inventory_count >= self.config.max_inventories:
            return RiskDecision(False, DecisionReason.PORTFOLIO_EXPOSURE_CAP)
        portfolio_room = max(
            0.0,
            self.config.max_portfolio_exposure_pct - portfolio.gross_long_exposure_pct,
        )
        symbol_room = self.config.max_symbol_exposure_pct
        approved = min(max(0.0, requested_exposure_pct), portfolio_room, symbol_room)
        if approved <= 1e-12:
            return RiskDecision(False, DecisionReason.PORTFOLIO_EXPOSURE_CAP)
        return RiskDecision(
            True,
            approved_exposure_pct=approved,
            projected_symbol_exposure_pct=approved,
            projected_portfolio_exposure_pct=portfolio.gross_long_exposure_pct + approved,
        )

    def allow_reentry(self, portfolio, inventory, *, requested_exposure_pct: float) -> RiskDecision:
        if inventory.entry_fill_count >= self.config.max_entry_fills:
            return RiskDecision(False, DecisionReason.MAX_ENTRY_FILLS)

        symbol_room = max(
            0.0,
            self.config.max_symbol_exposure_pct - inventory.wallet_exposure_pct,
        )
        portfolio_room = max(
            0.0,
            self.config.max_portfolio_exposure_pct - portfolio.gross_long_exposure_pct,
        )
        approved = min(max(0.0, requested_exposure_pct), symbol_room, portfolio_room)
        if approved <= 1e-12:
            reason = (
                DecisionReason.SYMBOL_EXPOSURE_CAP
                if symbol_room <= portfolio_room
                else DecisionReason.PORTFOLIO_EXPOSURE_CAP
            )
            return RiskDecision(False, reason)

        return RiskDecision(
            True,
            approved_exposure_pct=approved,
            projected_symbol_exposure_pct=inventory.wallet_exposure_pct + approved,
            projected_portfolio_exposure_pct=portfolio.gross_long_exposure_pct + approved,
        )
