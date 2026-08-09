from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.brokers.base import BrokerCloseExecution
from app.execution.position_economics import calculate_position_pnl
from app.execution.position_lifecycle_engine import (
    PositionLifecycleEngine,
    position_mae_percent,
    position_mfe_percent,
)
from app.execution.position_models import (
    ClosedPosition,
    EntryPriceSource,
    ExitPriceSource,
    ManagedStopUpdate,
    PositionCloseSignal,
    TrackedPosition,
)
from app.market.models import MarketSnapshot
from app.risk.models import TradePlan


class PositionTracker:
    def __init__(
        self,
        lifecycle_engine: PositionLifecycleEngine | None = None,
    ) -> None:
        self.positions: dict[str, TrackedPosition] = {}
        self.lifecycle_engine = lifecycle_engine or PositionLifecycleEngine()
        self._managed_stop_updates: list[ManagedStopUpdate] = []

    def restore_open_position(self, position: TrackedPosition) -> None:
        self.positions[position.position_id] = self._normalize_restored_position(
            position
        )

    def open_positions_snapshot(self) -> list[TrackedPosition]:
        return list(self.positions.values())

    def remove_position(self, position_id: str) -> TrackedPosition | None:
        return self.positions.pop(position_id, None)

    def consume_managed_stop_updates(self) -> tuple[ManagedStopUpdate, ...]:
        updates = tuple(self._managed_stop_updates)
        self._managed_stop_updates.clear()
        return updates

    def record_open_position(
        self,
        position_id: str,
        trade_plan: TradePlan,
        signal_price: float,
        executable_entry_estimate: float,
        broker_entry_fill_price: float | None,
        opened_at: datetime | None = None,
    ) -> TrackedPosition:
        pnl_entry_price, entry_price_source = self._resolve_entry_price(
            executable_entry_estimate=executable_entry_estimate,
            broker_entry_fill_price=broker_entry_fill_price,
        )
        self._validate_open_plan(trade_plan, signal_price, pnl_entry_price)
        amount = float(trade_plan.amount)
        explicit_cost = float(trade_plan.estimated_explicit_cost or 0.0)
        explicit_cost_percent = float(
            trade_plan.estimated_explicit_cost_percent or 0.0
        )
        position = TrackedPosition(
            position_id=position_id,
            symbol=str(trade_plan.symbol),
            side=str(trade_plan.side),
            amount=amount,
            signal_price=signal_price,
            executable_entry_estimate=executable_entry_estimate,
            broker_entry_fill_price=broker_entry_fill_price,
            pnl_entry_price=pnl_entry_price,
            entry_price_source=entry_price_source,
            stop_loss=float(trade_plan.stop_loss),
            take_profit=float(trade_plan.take_profit),
            opened_at=opened_at or datetime.now(timezone.utc),
            initial_stop_loss=float(trade_plan.stop_loss),
            highest_executable_price=pnl_entry_price,
            lowest_executable_price=pnl_entry_price,
            highest_last_execution_price=signal_price,
            lowest_last_execution_price=signal_price,
            breakeven_stop_enabled=trade_plan.breakeven_stop_enabled,
            breakeven_trigger_percent=trade_plan.breakeven_trigger_percent,
            breakeven_buffer_percent=trade_plan.breakeven_buffer_percent,
            trailing_stop_enabled=trade_plan.trailing_stop_enabled,
            trailing_stop_trigger_percent=(
                trade_plan.trailing_stop_trigger_percent
            ),
            trailing_stop_distance_percent=(
                trade_plan.trailing_stop_distance_percent
            ),
            trailing_stop_net_buffer_percent=(
                trade_plan.trailing_stop_net_buffer_percent
            ),
            estimated_open_fee=float(trade_plan.estimated_open_fee or 0.0),
            estimated_close_fee=float(trade_plan.estimated_close_fee or 0.0),
            estimated_fixed_fees=float(
                trade_plan.estimated_fixed_fees or 0.0
            ),
            estimated_explicit_cost=explicit_cost,
            estimated_explicit_cost_percent=explicit_cost_percent,
            pretrade_estimated_spread_cost=float(
                trade_plan.estimated_spread_cost or 0.0
            ),
            pretrade_observed_spread_percent=float(
                trade_plan.spread_percent or 0.0
            ),
            pretrade_estimated_total_cost=float(
                trade_plan.estimated_total_cost or 0.0
            ),
            pretrade_estimated_total_cost_percent=float(
                trade_plan.estimated_total_cost_percent or 0.0
            ),
            stale_position_enabled=trade_plan.stale_position_enabled,
            stale_position_max_age_minutes=(
                trade_plan.stale_position_max_age_minutes
            ),
            stale_position_min_favorable_move_percent=(
                trade_plan.stale_position_min_favorable_move_percent
            ),
            stale_position_buffer_percent=(
                trade_plan.stale_position_buffer_percent
            ),
        )
        self.positions[position_id] = position
        return position

    def evaluate_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        force_close: bool = False,
        force_close_metadata: dict[str, Any] | None = None,
        position_ids: set[str] | None = None,
    ) -> list[PositionCloseSignal]:
        close_signals: list[PositionCloseSignal] = []
        self._managed_stop_updates.clear()
        for previous in list(self.positions.values()):
            if previous.symbol != snapshot.symbol:
                continue
            if (
                position_ids is not None
                and previous.position_id not in position_ids
            ):
                continue
            result = self.lifecycle_engine.evaluate(
                position=previous,
                snapshot=snapshot,
                force_close=force_close,
                force_close_metadata=force_close_metadata,
            )
            self.positions[previous.position_id] = result.position
            if result.managed_stop_changed:
                self._managed_stop_updates.append(
                    ManagedStopUpdate(
                        previous_position=previous,
                        position=result.position,
                        observed_at=snapshot.timestamp,
                    )
                )
            if result.close_signal is not None:
                close_signals.append(result.close_signal)
        return close_signals

    def record_closed_position(
        self,
        close_signal: PositionCloseSignal,
        *,
        broker_execution: BrokerCloseExecution | None = None,
        confirmed_at: datetime | None = None,
    ) -> ClosedPosition | None:
        position = self.positions.pop(close_signal.position_id, None)
        if position is None:
            return None
        if (
            broker_execution is not None
            and broker_execution.position_id != position.position_id
        ):
            raise ValueError(
                'Broker close execution position mismatch: '
                f'{broker_execution.position_id} != {position.position_id}'
            )

        broker_price = (
            broker_execution.executed_exit_price
            if broker_execution is not None
            else None
        )
        if broker_price is not None and broker_price > 0:
            pnl_exit_price = broker_price
            exit_price_source = ExitPriceSource.BROKER_FILL
            position = self._include_exit_fill_in_extremes(
                position,
                broker_price,
            )
        else:
            pnl_exit_price = close_signal.executable_estimate
            exit_price_source = ExitPriceSource.EXECUTABLE_ESTIMATE

        closed_at = (
            broker_execution.executed_at
            if broker_execution is not None
            and broker_execution.executed_at is not None
            else confirmed_at or close_signal.detected_at
        )
        duration_seconds = max(
            0.0,
            (closed_at - position.opened_at).total_seconds(),
        )
        pnl = calculate_position_pnl(
            side=position.side,
            amount=position.amount,
            entry_price=position.pnl_entry_price,
            exit_price=pnl_exit_price,
            explicit_cost=position.estimated_explicit_cost,
            explicit_cost_percent=position.estimated_explicit_cost_percent,
        )

        return ClosedPosition(
            position_id=position.position_id,
            symbol=position.symbol,
            side=position.side,
            amount=position.amount,
            signal_price=position.signal_price,
            executable_entry_estimate=position.executable_entry_estimate,
            broker_entry_fill_price=position.broker_entry_fill_price,
            pnl_entry_price=position.pnl_entry_price,
            entry_price_source=position.entry_price_source,
            detection_last_execution_price=(
                close_signal.last_execution_price
            ),
            executable_exit_estimate=close_signal.executable_estimate,
            broker_exit_fill_price=broker_price,
            pnl_exit_price=pnl_exit_price,
            exit_price_source=exit_price_source,
            bid_at_detection=close_signal.bid_at_detection,
            ask_at_detection=close_signal.ask_at_detection,
            observed_spread_percent=close_signal.observed_spread_percent,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            opened_at=position.opened_at,
            close_detected_at=close_signal.detected_at,
            closed_at=closed_at,
            duration_seconds=round(duration_seconds, 3),
            close_reason=close_signal.reason,
            gross_pnl=round(pnl.gross_pnl, 4),
            gross_pnl_percent=round(pnl.gross_pnl_percent, 4),
            explicit_costs_deducted=round(
                position.estimated_explicit_cost,
                4,
            ),
            explicit_costs_deducted_percent=round(
                position.estimated_explicit_cost_percent,
                4,
            ),
            explicit_cost_source=position.explicit_cost_source,
            net_pnl=round(pnl.net_pnl, 4),
            net_pnl_percent=round(pnl.net_pnl_percent, 4),
            mfe_percent=round(position_mfe_percent(position), 4),
            mae_percent=round(position_mae_percent(position), 4),
            highest_executable_price=float(
                position.highest_executable_price
                or position.pnl_entry_price
            ),
            lowest_executable_price=float(
                position.lowest_executable_price
                or position.pnl_entry_price
            ),
            highest_last_execution_price=float(
                position.highest_last_execution_price
                or position.signal_price
            ),
            lowest_last_execution_price=float(
                position.lowest_last_execution_price
                or position.signal_price
            ),
            pretrade_estimated_spread_cost=(
                position.pretrade_estimated_spread_cost
            ),
            pretrade_observed_spread_percent=(
                position.pretrade_observed_spread_percent
            ),
            pretrade_estimated_total_cost=(
                position.pretrade_estimated_total_cost
            ),
            pretrade_estimated_total_cost_percent=(
                position.pretrade_estimated_total_cost_percent
            ),
        )

    def has_open_positions(self) -> bool:
        return bool(self.positions)

    @staticmethod
    def _normalize_restored_position(
        position: TrackedPosition,
    ) -> TrackedPosition:
        return replace(
            position,
            initial_stop_loss=position.initial_stop_loss or position.stop_loss,
            highest_executable_price=(
                position.highest_executable_price or position.pnl_entry_price
            ),
            lowest_executable_price=(
                position.lowest_executable_price or position.pnl_entry_price
            ),
            highest_last_execution_price=(
                position.highest_last_execution_price
                or position.signal_price
            ),
            lowest_last_execution_price=(
                position.lowest_last_execution_price
                or position.signal_price
            ),
        )

    @staticmethod
    def _include_exit_fill_in_extremes(
        position: TrackedPosition,
        exit_fill_price: float,
    ) -> TrackedPosition:
        return replace(
            position,
            highest_executable_price=max(
                position.highest_executable_price or position.pnl_entry_price,
                exit_fill_price,
            ),
            lowest_executable_price=min(
                position.lowest_executable_price or position.pnl_entry_price,
                exit_fill_price,
            ),
        )

    @staticmethod
    def _validate_open_plan(
        trade_plan: TradePlan,
        signal_price: float,
        pnl_entry_price: float,
    ) -> None:
        for name in ('symbol', 'side', 'amount', 'stop_loss', 'take_profit'):
            if getattr(trade_plan, name) is None:
                raise ValueError(
                    f'Cannot track position without {name}: {trade_plan}'
                )
        if signal_price <= 0:
            raise ValueError(f'Invalid signal price: {signal_price}')
        if pnl_entry_price <= 0:
            raise ValueError(f'Invalid P&L entry price: {pnl_entry_price}')

    @staticmethod
    def _resolve_entry_price(
        *,
        executable_entry_estimate: float,
        broker_entry_fill_price: float | None,
    ) -> tuple[float, EntryPriceSource]:
        if broker_entry_fill_price is not None:
            if broker_entry_fill_price <= 0:
                raise ValueError(
                    'Invalid broker entry fill price: '
                    f'{broker_entry_fill_price}'
                )
            return broker_entry_fill_price, EntryPriceSource.BROKER_FILL
        if executable_entry_estimate <= 0:
            raise ValueError(
                'Invalid executable entry estimate: '
                f'{executable_entry_estimate}'
            )
        return (
            executable_entry_estimate,
            EntryPriceSource.EXECUTABLE_ESTIMATE,
        )
