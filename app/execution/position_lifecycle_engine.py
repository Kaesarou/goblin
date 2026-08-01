from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.execution.managed_stop import (
    ManagedProtectionType,
    calculate_buy_managed_stop,
    calculate_sell_managed_stop,
)
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import PositionCloseSignal, TrackedPosition
from app.market.models import MarketSnapshot
from app.risk.stale_position_guard import StalePositionConfig, StalePositionGuard
from app.utils.commons import spread_percent

POSITION_LIFECYCLE_CONTRACT_VERSION = "executable_position_lifecycle_v2"


@dataclass(frozen=True)
class PositionLifecycleResult:
    previous_position: TrackedPosition
    position: TrackedPosition
    close_signal: PositionCloseSignal | None

    @property
    def managed_stop_changed(self) -> bool:
        return (
            self.position.stop_loss != self.previous_position.stop_loss
            or self.position.managed_stop_protection_type
            != self.previous_position.managed_stop_protection_type
        )


class PositionLifecycleEngine:
    """Pure, deterministic position lifecycle shared by live and replay."""

    def __init__(self, stale_position_guard: StalePositionGuard | None = None):
        self.stale_position_guard = stale_position_guard or StalePositionGuard()

    def evaluate(
        self,
        *,
        position: TrackedPosition,
        snapshot: MarketSnapshot,
        force_close: bool = False,
        force_close_metadata: dict[str, Any] | None = None,
    ) -> PositionLifecycleResult:
        executable_price = snapshot.executable_exit_price(position.side)
        updated = self._update_extremes(
            position,
            executable_price=executable_price,
            last_execution_price=snapshot.last,
        )
        updated = self._apply_managed_stop(updated)

        close_reason = self._price_close_reason(
            updated,
            executable_price=executable_price,
        )
        metadata: dict[str, Any] | None = None
        if close_reason in {
            PositionCloseReason.PROTECTED_BREAKEVEN,
            PositionCloseReason.PROTECTED_TRAILING,
        }:
            metadata = self._managed_stop_close_metadata(
                updated,
                executable_price=executable_price,
            )

        if close_reason is None:
            close_reason, metadata = self._stale_close_reason(
                updated,
                snapshot=snapshot,
            )
        if close_reason is None and force_close:
            close_reason = PositionCloseReason.SESSION_FORCE_CLOSE
            metadata = dict(force_close_metadata or {})

        close_signal = None
        if close_reason is not None:
            close_signal = PositionCloseSignal(
                position_id=updated.position_id,
                symbol=updated.symbol,
                side=updated.side,
                reason=close_reason,
                detected_at=snapshot.timestamp,
                last_execution_price=snapshot.last,
                executable_estimate=executable_price,
                bid_at_detection=snapshot.bid,
                ask_at_detection=snapshot.ask,
                observed_spread_percent=spread_percent(snapshot),
                metadata=metadata,
            )
        return PositionLifecycleResult(
            previous_position=position,
            position=updated,
            close_signal=close_signal,
        )

    def _update_extremes(
        self,
        position: TrackedPosition,
        *,
        executable_price: float,
        last_execution_price: float,
    ) -> TrackedPosition:
        return replace(
            position,
            highest_executable_price=max(
                position.highest_executable_price or position.pnl_entry_price,
                executable_price,
            ),
            lowest_executable_price=min(
                position.lowest_executable_price or position.pnl_entry_price,
                executable_price,
            ),
            highest_last_execution_price=max(
                position.highest_last_execution_price or position.signal_price,
                last_execution_price,
            ),
            lowest_last_execution_price=min(
                position.lowest_last_execution_price or position.signal_price,
                last_execution_price,
            ),
        )

    def _apply_managed_stop(self, position: TrackedPosition) -> TrackedPosition:
        highest = position.highest_executable_price or position.pnl_entry_price
        lowest = position.lowest_executable_price or position.pnl_entry_price
        common = {
            "entry_price": position.pnl_entry_price,
            "current_stop_loss": position.stop_loss,
            "highest_price": highest,
            "lowest_price": lowest,
            "breakeven_stop_enabled": position.breakeven_stop_enabled,
            "breakeven_trigger_percent": position.breakeven_trigger_percent,
            "breakeven_buffer_percent": position.breakeven_buffer_percent,
            "trailing_stop_enabled": position.trailing_stop_enabled,
            "trailing_stop_trigger_percent": (position.trailing_stop_trigger_percent),
            "trailing_stop_distance_percent": (position.trailing_stop_distance_percent),
            "trailing_stop_net_buffer_percent": (position.trailing_stop_net_buffer_percent),
            "estimated_explicit_cost_percent": (position.estimated_explicit_cost_percent),
        }
        if position.side == "BUY":
            decision = calculate_buy_managed_stop(**common)
        elif position.side == "SELL":
            decision = calculate_sell_managed_stop(**common)
        else:
            raise ValueError(f"Unsupported tracked position side: {position.side}")
        if decision.protection_type is None:
            return replace(position, stop_loss=decision.stop_loss)
        return replace(
            position,
            stop_loss=decision.stop_loss,
            managed_stop_protection_type=decision.protection_type,
            last_stop_update_metadata=decision.metadata,
        )

    def _price_close_reason(
        self,
        position: TrackedPosition,
        *,
        executable_price: float,
    ) -> PositionCloseReason | None:
        active_stop_hit = (
            executable_price <= position.stop_loss
            if position.side == "BUY"
            else executable_price >= position.stop_loss
        )
        if active_stop_hit and position.managed_stop_protection_type is not None:
            if position.managed_stop_protection_type == ManagedProtectionType.BREAKEVEN:
                return PositionCloseReason.PROTECTED_BREAKEVEN
            if position.managed_stop_protection_type == ManagedProtectionType.TRAILING:
                return PositionCloseReason.PROTECTED_TRAILING

        take_profit_hit = (
            executable_price >= position.take_profit
            if position.side == "BUY"
            else executable_price <= position.take_profit
        )
        if take_profit_hit:
            return PositionCloseReason.TAKE_PROFIT
        if active_stop_hit:
            return PositionCloseReason.INITIAL_STOP
        return None

    def _stale_close_reason(
        self,
        position: TrackedPosition,
        *,
        snapshot: MarketSnapshot,
    ) -> tuple[PositionCloseReason | None, dict[str, Any] | None]:
        decision = self.stale_position_guard.evaluate(
            side=position.side,
            entry_price=position.pnl_entry_price,
            highest_price=position.highest_executable_price,
            lowest_price=position.lowest_executable_price,
            opened_at=position.opened_at,
            now=snapshot.timestamp,
            estimated_explicit_cost_percent=(position.estimated_explicit_cost_percent),
            config=StalePositionConfig(
                enabled=position.stale_position_enabled,
                max_age_minutes=position.stale_position_max_age_minutes,
                min_favorable_move_percent=(position.stale_position_min_favorable_move_percent),
                buffer_percent=position.stale_position_buffer_percent,
            ),
        )
        if not decision.should_close:
            return None, None
        return PositionCloseReason.STALE_EXIT, {
            "stale_position_action": "CLOSE",
            "stale_position_age_minutes": round(decision.age_minutes, 4),
            "stale_position_mfe_percent": round(decision.mfe_percent, 4),
            "stale_position_required_mfe_percent": round(
                decision.required_mfe_percent,
                4,
            ),
            "estimated_explicit_cost_percent": round(
                decision.estimated_explicit_cost_percent,
                4,
            ),
            "stale_position_max_age_minutes": (position.stale_position_max_age_minutes),
            "stale_position_min_favorable_move_percent": (
                position.stale_position_min_favorable_move_percent
            ),
            "stale_position_buffer_percent": position.stale_position_buffer_percent,
        }

    @staticmethod
    def _managed_stop_close_metadata(
        position: TrackedPosition,
        *,
        executable_price: float,
    ) -> dict[str, Any] | None:
        if position.last_stop_update_metadata is None:
            return None
        return {
            **position.last_stop_update_metadata,
            "managed_stop_protection_type": (
                position.managed_stop_protection_type.value
                if position.managed_stop_protection_type is not None
                else None
            ),
            "stop_loss": round(position.stop_loss, 5),
            "pnl_entry_price": round(position.pnl_entry_price, 5),
            "executable_price": round(executable_price, 5),
        }


def position_mfe_percent(position: TrackedPosition) -> float:
    entry = position.pnl_entry_price
    if entry <= 0:
        raise ValueError(f"Invalid entry fill price: {entry}")
    if position.side == "BUY":
        high = position.highest_executable_price or entry
        return max(0.0, (high - entry) / entry * 100)
    if position.side == "SELL":
        low = position.lowest_executable_price or entry
        return max(0.0, (entry - low) / entry * 100)
    raise ValueError(f"Unsupported tracked position side: {position.side}")


def position_mae_percent(position: TrackedPosition) -> float:
    entry = position.pnl_entry_price
    if entry <= 0:
        raise ValueError(f"Invalid entry fill price: {entry}")
    if position.side == "BUY":
        low = position.lowest_executable_price or entry
        return max(0.0, (entry - low) / entry * 100)
    if position.side == "SELL":
        high = position.highest_executable_price or entry
        return max(0.0, (high - entry) / entry * 100)
    raise ValueError(f"Unsupported tracked position side: {position.side}")
