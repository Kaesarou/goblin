from dataclasses import dataclass

from app.v3.config import EconomicsPolicy
from app.v3.models import CostEstimate


ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST = 1.0
ALPACA_FIXED_FEE_ASSUMPTION_PER_REQUEST = 0.0


@dataclass(frozen=True)
class BrokerCostSchedule:
    commission_pct_per_fill: float = 0
    fixed_fee_per_fill: float = 0
    slippage_pct_per_fill: float = 0
    annual_financing_pct: float = 0
    annual_borrow_pct: float = 0


def broker_cost_schedule(broker: str) -> BrokerCostSchedule:
    """Return the research cost schedule for the selected execution broker.

    These values are strategy-economics assumptions, not broker-confirmed fees.
    Actual fill fees must be journaled separately when the adapter exposes them.
    """

    normalized = broker.strip().lower().replace("-", "_")
    if normalized in {"alpaca_demo", "alpaca_live"}:
        return BrokerCostSchedule(
            fixed_fee_per_fill=ALPACA_FIXED_FEE_ASSUMPTION_PER_REQUEST,
        )
    if normalized in {"paper", "etoro_demo", "etoro_live"}:
        return BrokerCostSchedule(
            fixed_fee_per_fill=ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST,
        )
    raise ValueError(f"Unsupported broker cost schedule: {broker}")


@dataclass(frozen=True)
class EconomicsDecision:
    allowed: bool
    estimated_gross_capture: float
    estimated_net_capture: float
    costs: CostEstimate


class EconomicsModel:
    def __init__(self, config, schedule):
        self.config = config
        self.schedule = schedule

    def estimate(
        self,
        *,
        market,
        notional,
        expected_gross_capture_pct,
        side,
        expected_holding_days=None,
    ):
        # Signed close thresholds may be <=0. Economics uses a conservative
        # positive capture floor; trigger semantics stay signed in the planner.
        capture = max(1e-6, float(expected_gross_capture_pct))
        days = (
            self.config.expected_holding_days
            if expected_holding_days is None
            else max(0, expected_holding_days)
        )
        costs = CostEstimate(
            notional * market.spread_pct,
            notional * self.schedule.commission_pct_per_fill * 2,
            self.schedule.fixed_fee_per_fill * 2,
            notional * self.schedule.slippage_pct_per_fill * 2,
            notional * self.schedule.annual_financing_pct * days / 365,
            (
                notional * self.schedule.annual_borrow_pct * days / 365
                if side.upper() == "SELL"
                else 0
            ),
        )
        gross = notional * capture
        net = gross - costs.total
        allowed = (
            gross > 0
            if self.config.policy == EconomicsPolicy.RESEARCH_GROSS_EDGE
            else net > 0
        )
        return EconomicsDecision(allowed, gross, net, costs)
