from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class StalePositionAction(StrEnum):
    CLOSE = 'CLOSE'


@dataclass(frozen=True)
class StalePositionConfig:
    enabled: bool = False
    max_age_minutes: int = 0
    min_favorable_move_percent: float = 0.0
    buffer_percent: float = 0.0
    action: StalePositionAction = StalePositionAction.CLOSE


@dataclass(frozen=True)
class StalePositionDecision:
    should_close: bool
    action: StalePositionAction | None
    age_minutes: float
    mfe_percent: float
    required_mfe_percent: float
    estimated_explicit_cost_percent: float


class StalePositionGuard:
    def evaluate(
        self,
        *,
        side: str,
        entry_price: float,
        highest_price: float | None,
        lowest_price: float | None,
        opened_at: datetime,
        now: datetime,
        estimated_explicit_cost_percent: float,
        config: StalePositionConfig,
    ) -> StalePositionDecision:
        age_minutes = max(0.0, (now - opened_at).total_seconds() / 60)
        mfe_percent = self._calculate_mfe_percent(
            side=side,
            entry_price=entry_price,
            highest_price=highest_price,
            lowest_price=lowest_price,
        )
        required_mfe_percent = self._required_mfe_percent(
            estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            config=config,
        )

        if not config.enabled:
            return self._no_action(
                age_minutes=age_minutes,
                mfe_percent=mfe_percent,
                required_mfe_percent=required_mfe_percent,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            )

        if config.action != StalePositionAction.CLOSE:
            return self._no_action(
                age_minutes=age_minutes,
                mfe_percent=mfe_percent,
                required_mfe_percent=required_mfe_percent,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            )

        if config.max_age_minutes <= 0 or age_minutes < config.max_age_minutes:
            return self._no_action(
                age_minutes=age_minutes,
                mfe_percent=mfe_percent,
                required_mfe_percent=required_mfe_percent,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            )

        if mfe_percent >= required_mfe_percent:
            return self._no_action(
                age_minutes=age_minutes,
                mfe_percent=mfe_percent,
                required_mfe_percent=required_mfe_percent,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            )

        return StalePositionDecision(
            should_close=True,
            action=config.action,
            age_minutes=age_minutes,
            mfe_percent=mfe_percent,
            required_mfe_percent=required_mfe_percent,
            estimated_explicit_cost_percent=estimated_explicit_cost_percent,
        )

    def _required_mfe_percent(
        self,
        *,
        estimated_explicit_cost_percent: float,
        config: StalePositionConfig,
    ) -> float:
        return max(
            config.min_favorable_move_percent,
            estimated_explicit_cost_percent + config.buffer_percent,
        )

    def _calculate_mfe_percent(
        self,
        *,
        side: str,
        entry_price: float,
        highest_price: float | None,
        lowest_price: float | None,
    ) -> float:
        if entry_price <= 0:
            raise ValueError(f'Cannot calculate MFE with invalid entry_price={entry_price}')

        if side == 'BUY':
            reference_price = highest_price or entry_price
            return max(0.0, ((reference_price - entry_price) / entry_price) * 100)

        if side == 'SELL':
            reference_price = lowest_price or entry_price
            return max(0.0, ((entry_price - reference_price) / entry_price) * 100)

        raise ValueError(f'Unsupported position side for MFE calculation: {side}')

    def _no_action(
        self,
        *,
        age_minutes: float,
        mfe_percent: float,
        required_mfe_percent: float,
        estimated_explicit_cost_percent: float,
    ) -> StalePositionDecision:
        return StalePositionDecision(
            should_close=False,
            action=None,
            age_minutes=age_minutes,
            mfe_percent=mfe_percent,
            required_mfe_percent=required_mfe_percent,
            estimated_explicit_cost_percent=estimated_explicit_cost_percent,
        )
