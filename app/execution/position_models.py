from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.execution.managed_stop import ManagedProtectionType, ManagedStopMetadata
from app.execution.position_close_reason import PositionCloseReason

POSITION_ECONOMICS_CONTRACT_VERSION = "position_economics_v2"


class EntryPriceSource(StrEnum):
    BROKER_FILL = "broker_fill"
    EXECUTABLE_ESTIMATE = "executable_estimate"


class ExitPriceSource(StrEnum):
    BROKER_FILL = "broker_fill"
    EXECUTABLE_ESTIMATE = "executable_estimate"
    UNAVAILABLE = "unavailable"


class ExplicitCostSource(StrEnum):
    RISK_PROFILE_ESTIMATE = "risk_profile_estimate"


@dataclass(frozen=True)
class TrackedPosition:
    position_id: str
    symbol: str
    side: str
    amount: float
    signal_price: float
    executable_entry_estimate: float
    broker_entry_fill_price: float | None
    pnl_entry_price: float
    entry_price_source: EntryPriceSource
    stop_loss: float
    take_profit: float
    opened_at: datetime
    initial_stop_loss: float | None = None
    highest_executable_price: float | None = None
    lowest_executable_price: float | None = None
    highest_last_execution_price: float | None = None
    lowest_last_execution_price: float | None = None
    breakeven_stop_enabled: bool = False
    breakeven_trigger_percent: float = 0.0
    breakeven_buffer_percent: float = 0.0
    trailing_stop_enabled: bool = False
    trailing_stop_trigger_percent: float = 0.0
    trailing_stop_distance_percent: float = 0.0
    trailing_stop_net_buffer_percent: float = 0.0
    managed_stop_protection_type: ManagedProtectionType | None = None
    last_stop_update_metadata: ManagedStopMetadata | None = None
    estimated_open_fee: float = 0.0
    estimated_close_fee: float = 0.0
    estimated_fixed_fees: float = 0.0
    estimated_explicit_cost: float = 0.0
    estimated_explicit_cost_percent: float = 0.0
    pretrade_estimated_spread_cost: float = 0.0
    pretrade_observed_spread_percent: float = 0.0
    pretrade_estimated_total_cost: float = 0.0
    pretrade_estimated_total_cost_percent: float = 0.0
    explicit_cost_source: ExplicitCostSource = ExplicitCostSource.RISK_PROFILE_ESTIMATE
    stale_position_enabled: bool = False
    stale_position_max_age_minutes: int = 0
    stale_position_min_favorable_move_percent: float = 0.0
    stale_position_buffer_percent: float = 0.0


@dataclass(frozen=True)
class ManagedStopUpdate:
    previous_position: TrackedPosition
    position: TrackedPosition
    observed_at: datetime


@dataclass(frozen=True)
class PositionCloseSignal:
    position_id: str
    symbol: str
    side: str
    reason: PositionCloseReason
    detected_at: datetime
    last_execution_price: float
    executable_estimate: float
    bid_at_detection: float
    ask_at_detection: float
    observed_spread_percent: float
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClosedPosition:
    position_id: str
    symbol: str
    side: str
    amount: float
    signal_price: float
    executable_entry_estimate: float
    broker_entry_fill_price: float | None
    pnl_entry_price: float
    entry_price_source: EntryPriceSource
    detection_last_execution_price: float
    executable_exit_estimate: float
    broker_exit_fill_price: float | None
    pnl_exit_price: float
    exit_price_source: ExitPriceSource
    bid_at_detection: float
    ask_at_detection: float
    observed_spread_percent: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    close_detected_at: datetime
    closed_at: datetime
    duration_seconds: float
    close_reason: PositionCloseReason
    gross_pnl: float
    gross_pnl_percent: float
    explicit_costs_deducted: float
    explicit_costs_deducted_percent: float
    explicit_cost_source: ExplicitCostSource
    net_pnl: float
    net_pnl_percent: float
    mfe_percent: float
    mae_percent: float
    highest_executable_price: float
    lowest_executable_price: float
    highest_last_execution_price: float
    lowest_last_execution_price: float
    pretrade_estimated_spread_cost: float
    pretrade_observed_spread_percent: float
    pretrade_estimated_total_cost: float
    pretrade_estimated_total_cost_percent: float
