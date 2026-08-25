from __future__ import annotations

from dataclasses import dataclass

from app.v3.models import (
    DecisionBatch,
    DecisionReason,
    DecisionRecord,
    ExecutionStyle,
    HedgeState,
    IntentPurpose,
    OrderIntent,
    PortfolioState,
)


@dataclass(frozen=True)
class HedgeDecision:
    desired_notional: float
    current_notional: float
    beta_before: float
    beta_after: float
    action: str


class PortfolioHedgeManager:
    """Plan a non-speculative SELL hedge for aggregate long beta.

    The hedge is not an alpha strategy. It may only move portfolio beta exposure
    closer to the configured target and it may never create a net speculative
    short exposure.
    """

    def __init__(self, config) -> None:
        self.config = config

    def plan(self, *, portfolio: PortfolioState, hedge_price: float, asof) -> DecisionBatch:
        if not self.config.enabled or portfolio.equity <= 0 or hedge_price <= 0:
            return DecisionBatch()

        beta_before = portfolio.beta_notional
        current = portfolio.hedge.notional if portfolio.hedge else 0.0
        beta_pct = beta_before / portfolio.equity

        if beta_before <= 0:
            desired = 0.0
        elif current <= 0 and beta_pct < self.config.open_above_beta_exposure_pct:
            desired = 0.0
        elif current > 0 and beta_pct <= self.config.close_below_beta_exposure_pct:
            desired = 0.0
        else:
            target_beta = max(0.0, self.config.target_beta_exposure_pct * portfolio.equity)
            raw_desired = max(0.0, beta_before - target_beta + current)
            max_notional = max(0.0, self.config.max_hedge_notional_pct * portfolio.equity)
            # Never hedge beyond the portfolio's long beta notional.
            desired = min(raw_desired, max_notional, max(0.0, beta_before + current))

        delta = desired - current
        min_adjustment = self.config.min_adjustment_notional_pct * portfolio.equity
        deadband = self.config.rebalance_deadband_pct * portfolio.equity

        if current > 0 and desired > 0 and abs(delta) <= max(min_adjustment, deadband):
            return self._decision(
                portfolio=portfolio,
                asof=asof,
                reason=DecisionReason.HEDGE_NOT_REQUIRED,
                detail={"current_notional": current, "desired_notional": desired},
            )
        if current == 0 and desired <= min_adjustment:
            return self._decision(
                portfolio=portfolio,
                asof=asof,
                reason=DecisionReason.HEDGE_NOT_REQUIRED,
                detail={"current_notional": current, "desired_notional": desired},
            )

        beta_after = beta_before - delta
        target_abs = abs(self.config.target_beta_exposure_pct * portfolio.equity)
        before_distance = abs(beta_before - target_abs)
        after_distance = abs(beta_after - target_abs)

        if after_distance >= before_distance - 1e-12:
            return self._decision(
                portfolio=portfolio,
                asof=asof,
                reason=DecisionReason.HEDGE_NOT_REQUIRED,
                detail={
                    "invariant": "hedge_must_reduce_beta_distance",
                    "beta_before": beta_before,
                    "beta_after": beta_after,
                },
            )

        if desired <= 1e-12 and current > 0:
            purpose = IntentPurpose.HEDGE_CLOSE
            side = "BUY"
            notional = current
            reduce_only = True
        elif delta > 0:
            purpose = IntentPurpose.HEDGE_OPEN if current <= 0 else IntentPurpose.HEDGE_ADJUST
            side = "SELL"
            notional = delta
            reduce_only = False
        else:
            purpose = IntentPurpose.HEDGE_ADJUST
            side = "BUY"
            notional = abs(delta)
            reduce_only = True

        intent = OrderIntent(
            intent_id=(
                f"hedge:{purpose.value}:{portfolio.hedge.symbol if portfolio.hedge else self.config.hedge_symbol}:"
                f"{asof.isoformat()}"
            ),
            purpose=purpose,
            symbol=(portfolio.hedge.symbol if portfolio.hedge else self.config.hedge_symbol),
            side=side,
            notional=notional,
            created_at=asof,
            execution_style=ExecutionStyle.MARKET,
            reduce_only=reduce_only,
            metadata={
                "beta_before": beta_before,
                "beta_after": beta_after,
                "target_beta_notional": target_abs,
                "current_hedge_notional": current,
                "desired_hedge_notional": desired,
            },
        )
        record = DecisionRecord(
            symbol=intent.symbol,
            reason=DecisionReason.HEDGE_REQUIRED,
            asof=asof,
            detail=dict(intent.metadata),
        )
        return DecisionBatch(intents=(intent,), decisions=(record,))

    def _decision(self, *, portfolio, asof, reason, detail) -> DecisionBatch:
        symbol = portfolio.hedge.symbol if portfolio.hedge else self.config.hedge_symbol
        return DecisionBatch(
            decisions=(DecisionRecord(symbol=symbol, reason=reason, asof=asof, detail=detail),)
        )
