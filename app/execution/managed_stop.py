from dataclasses import dataclass
from enum import StrEnum

ManagedStopMetadata = dict[str, float | int | str | bool]


class ManagedProtectionType(StrEnum):
    BREAKEVEN = 'breakeven'
    TRAILING = 'trailing'


@dataclass(frozen=True)
class ManagedStopDecision:
    stop_loss: float
    protection_type: ManagedProtectionType | None
    metadata: ManagedStopMetadata | None = None


def calculate_buy_managed_stop(
    *,
    entry_price: float,
    current_stop_loss: float,
    highest_price: float,
    lowest_price: float,
    breakeven_stop_enabled: bool,
    breakeven_trigger_percent: float,
    breakeven_buffer_percent: float,
    trailing_stop_enabled: bool,
    trailing_stop_trigger_percent: float,
    trailing_stop_distance_percent: float,
    trailing_stop_net_buffer_percent: float,
    estimated_explicit_cost_percent: float,
) -> ManagedStopDecision:
    mfe_percent = _buy_mfe_percent(
        entry_price=entry_price,
        highest_price=highest_price,
    )
    stop_loss = current_stop_loss
    protection_type: ManagedProtectionType | None = None
    metadata: ManagedStopMetadata | None = None

    if breakeven_stop_enabled and mfe_percent >= breakeven_trigger_percent:
        locked_gross_percent = (
            estimated_explicit_cost_percent + breakeven_buffer_percent
        )
        candidate_stop = entry_price * (1 + locked_gross_percent / 100)
        if candidate_stop > stop_loss:
            stop_loss, protection_type, metadata = _build_decision_values(
                side='BUY',
                entry_price=entry_price,
                previous_stop_loss=stop_loss,
                candidate_stop=candidate_stop,
                protection_type=ManagedProtectionType.BREAKEVEN,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
                trailing_stop_net_buffer_percent=breakeven_buffer_percent,
                highest_price=highest_price,
                lowest_price=lowest_price,
                mfe_percent=mfe_percent,
            )

    if trailing_stop_enabled and mfe_percent >= trailing_stop_trigger_percent:
        candidate_stop = highest_price * (
            1 - trailing_stop_distance_percent / 100
        )
        if candidate_stop > stop_loss and _is_net_locked(
            side='BUY',
            entry_price=entry_price,
            candidate_stop=candidate_stop,
            estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            trailing_stop_net_buffer_percent=(
                trailing_stop_net_buffer_percent
            ),
        ):
            stop_loss, protection_type, metadata = _build_decision_values(
                side='BUY',
                entry_price=entry_price,
                previous_stop_loss=stop_loss,
                candidate_stop=candidate_stop,
                protection_type=ManagedProtectionType.TRAILING,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
                trailing_stop_net_buffer_percent=(
                    trailing_stop_net_buffer_percent
                ),
                highest_price=highest_price,
                lowest_price=lowest_price,
                mfe_percent=mfe_percent,
            )

    return ManagedStopDecision(
        stop_loss=round(stop_loss, 5),
        protection_type=protection_type,
        metadata=metadata,
    )


def calculate_sell_managed_stop(
    *,
    entry_price: float,
    current_stop_loss: float,
    highest_price: float,
    lowest_price: float,
    breakeven_stop_enabled: bool,
    breakeven_trigger_percent: float,
    breakeven_buffer_percent: float,
    trailing_stop_enabled: bool,
    trailing_stop_trigger_percent: float,
    trailing_stop_distance_percent: float,
    trailing_stop_net_buffer_percent: float,
    estimated_explicit_cost_percent: float,
) -> ManagedStopDecision:
    mfe_percent = _sell_mfe_percent(
        entry_price=entry_price,
        lowest_price=lowest_price,
    )
    stop_loss = current_stop_loss
    protection_type: ManagedProtectionType | None = None
    metadata: ManagedStopMetadata | None = None

    if breakeven_stop_enabled and mfe_percent >= breakeven_trigger_percent:
        locked_gross_percent = (
            estimated_explicit_cost_percent + breakeven_buffer_percent
        )
        candidate_stop = entry_price * (1 - locked_gross_percent / 100)
        if candidate_stop < stop_loss:
            stop_loss, protection_type, metadata = _build_decision_values(
                side='SELL',
                entry_price=entry_price,
                previous_stop_loss=stop_loss,
                candidate_stop=candidate_stop,
                protection_type=ManagedProtectionType.BREAKEVEN,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
                trailing_stop_net_buffer_percent=breakeven_buffer_percent,
                highest_price=highest_price,
                lowest_price=lowest_price,
                mfe_percent=mfe_percent,
            )

    if trailing_stop_enabled and mfe_percent >= trailing_stop_trigger_percent:
        candidate_stop = lowest_price * (
            1 + trailing_stop_distance_percent / 100
        )
        if candidate_stop < stop_loss and _is_net_locked(
            side='SELL',
            entry_price=entry_price,
            candidate_stop=candidate_stop,
            estimated_explicit_cost_percent=estimated_explicit_cost_percent,
            trailing_stop_net_buffer_percent=(
                trailing_stop_net_buffer_percent
            ),
        ):
            stop_loss, protection_type, metadata = _build_decision_values(
                side='SELL',
                entry_price=entry_price,
                previous_stop_loss=stop_loss,
                candidate_stop=candidate_stop,
                protection_type=ManagedProtectionType.TRAILING,
                estimated_explicit_cost_percent=estimated_explicit_cost_percent,
                trailing_stop_net_buffer_percent=(
                    trailing_stop_net_buffer_percent
                ),
                highest_price=highest_price,
                lowest_price=lowest_price,
                mfe_percent=mfe_percent,
            )

    return ManagedStopDecision(
        stop_loss=round(stop_loss, 5),
        protection_type=protection_type,
        metadata=metadata,
    )


def locked_gross_percent(
    *,
    side: str,
    entry_price: float,
    stop_loss: float,
) -> float:
    if side == 'BUY':
        return ((stop_loss - entry_price) / entry_price) * 100
    if side == 'SELL':
        return ((entry_price - stop_loss) / entry_price) * 100
    raise ValueError(f'Unsupported side for managed stop: {side}')


def _is_net_locked(
    *,
    side: str,
    entry_price: float,
    candidate_stop: float,
    estimated_explicit_cost_percent: float,
    trailing_stop_net_buffer_percent: float,
) -> bool:
    gross_locked_percent = locked_gross_percent(
        side=side,
        entry_price=entry_price,
        stop_loss=candidate_stop,
    )
    estimated_net_locked_percent = (
        gross_locked_percent - estimated_explicit_cost_percent
    )
    return (
        estimated_net_locked_percent
        >= trailing_stop_net_buffer_percent
    )


def _build_decision_values(
    *,
    side: str,
    entry_price: float,
    previous_stop_loss: float,
    candidate_stop: float,
    protection_type: ManagedProtectionType,
    estimated_explicit_cost_percent: float,
    trailing_stop_net_buffer_percent: float,
    highest_price: float,
    lowest_price: float,
    mfe_percent: float,
) -> tuple[float, ManagedProtectionType, ManagedStopMetadata]:
    gross_locked_percent = locked_gross_percent(
        side=side,
        entry_price=entry_price,
        stop_loss=candidate_stop,
    )
    metadata: ManagedStopMetadata = {
        'previous_stop_loss': round(previous_stop_loss, 5),
        'new_stop_loss': round(candidate_stop, 5),
        'protection_type': protection_type,
        'gross_locked_percent': round(gross_locked_percent, 4),
        'estimated_explicit_cost_percent': round(
            estimated_explicit_cost_percent,
            4,
        ),
        'estimated_net_locked_percent': round(
            gross_locked_percent - estimated_explicit_cost_percent,
            4,
        ),
        'trailing_stop_net_buffer_percent': round(
            trailing_stop_net_buffer_percent,
            4,
        ),
        'highest_price': round(highest_price, 5),
        'lowest_price': round(lowest_price, 5),
        'mfe_percent': round(mfe_percent, 4),
    }
    return candidate_stop, protection_type, metadata


def _buy_mfe_percent(*, entry_price: float, highest_price: float) -> float:
    return ((highest_price - entry_price) / entry_price) * 100


def _sell_mfe_percent(*, entry_price: float, lowest_price: float) -> float:
    return ((entry_price - lowest_price) / entry_price) * 100
