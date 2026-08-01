from dataclasses import dataclass

TRADE_COST_CONTRACT_VERSION = 'executable_fills_explicit_costs_v2'


@dataclass(frozen=True)
class TradeCostConfig:
    open_fee_percent: float = 0.0
    close_fee_percent: float = 0.0
    fixed_open_fee: float = 0.0
    fixed_close_fee: float = 0.0
    include_spread_cost: bool = True
    min_expected_net_profit_percent: float = 0.0


@dataclass(frozen=True)
class TradeCostEstimate:
    position_value: float
    expected_gross_profit: float
    open_fee: float
    close_fee: float
    fixed_fees: float
    explicit_cost: float
    explicit_cost_percent: float
    spread_cost: float
    total_estimated_cost: float
    total_estimated_cost_percent: float
    expected_net_profit: float
    expected_net_profit_percent: float
    min_expected_net_profit_percent: float
    required_min_expected_net_profit_amount: float


class TradeCostModel:
    def estimate(
        self,
        *,
        position_value: float,
        expected_move_percent: float,
        spread_percent: float | None,
        config: TradeCostConfig,
    ) -> TradeCostEstimate:
        if position_value <= 0:
            return TradeCostEstimate(
                position_value=position_value,
                expected_gross_profit=0.0,
                open_fee=0.0,
                close_fee=0.0,
                fixed_fees=0.0,
                explicit_cost=0.0,
                explicit_cost_percent=0.0,
                spread_cost=0.0,
                total_estimated_cost=0.0,
                total_estimated_cost_percent=0.0,
                expected_net_profit=0.0,
                expected_net_profit_percent=0.0,
                min_expected_net_profit_percent=config.min_expected_net_profit_percent,
                required_min_expected_net_profit_amount=0.0,
            )

        expected_gross_profit = position_value * (expected_move_percent / 100)

        open_fee = position_value * (config.open_fee_percent / 100)
        close_fee = position_value * (config.close_fee_percent / 100)
        fixed_fees = config.fixed_open_fee + config.fixed_close_fee
        explicit_cost = open_fee + close_fee + fixed_fees
        spread_cost = 0.0
        if config.include_spread_cost and spread_percent is not None:
            spread_cost = position_value * (spread_percent / 100)

        total_estimated_cost = explicit_cost + spread_cost

        return self._build_estimate(
            position_value=position_value,
            expected_gross_profit=expected_gross_profit,
            open_fee=open_fee,
            close_fee=close_fee,
            fixed_fees=fixed_fees,
            explicit_cost=explicit_cost,
            spread_cost=spread_cost,
            total_estimated_cost=total_estimated_cost,
            min_expected_net_profit_percent=config.min_expected_net_profit_percent,
        )

    def _build_estimate(
        self,
        *,
        position_value: float,
        expected_gross_profit: float,
        open_fee: float,
        close_fee: float,
        fixed_fees: float,
        explicit_cost: float,
        spread_cost: float,
        total_estimated_cost: float,
        min_expected_net_profit_percent: float,
    ) -> TradeCostEstimate:
        expected_net_profit = expected_gross_profit - total_estimated_cost

        return TradeCostEstimate(
            position_value=position_value,
            expected_gross_profit=expected_gross_profit,
            open_fee=open_fee,
            close_fee=close_fee,
            fixed_fees=fixed_fees,
            explicit_cost=explicit_cost,
            explicit_cost_percent=(explicit_cost / position_value) * 100,
            spread_cost=spread_cost,
            total_estimated_cost=total_estimated_cost,
            total_estimated_cost_percent=(total_estimated_cost / position_value) * 100,
            expected_net_profit=expected_net_profit,
            expected_net_profit_percent=(expected_net_profit / position_value) * 100,
            min_expected_net_profit_percent=min_expected_net_profit_percent,
            required_min_expected_net_profit_amount=(
                position_value * min_expected_net_profit_percent / 100
            ),
        )
