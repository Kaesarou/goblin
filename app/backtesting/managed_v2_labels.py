from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_economics import calculate_position_pnl
from app.execution.position_lifecycle_engine import (
    PositionLifecycleEngine,
    position_mae_percent,
    position_mfe_percent,
)
from app.execution.position_models import TrackedPosition
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_LABEL_CONTRACT_VERSION,
)
from app.market.models import MarketSnapshot


@dataclass(frozen=True)
class ManagedV2LifecycleLabels:
    candidate_id: str
    label_contract_version: str
    opportunity: int
    path_quality: int | None
    net_return_percent: float | None
    close_reason: str | None
    mfe_percent: float
    mae_percent: float
    protection_threshold_percent: float
    first_protection_at: datetime | None
    first_initial_stop_at: datetime | None
    economics_closed_at: datetime | None
    observation_ended_at: datetime
    time_to_protection_seconds: float | None


@dataclass
class _LabelState:
    candidate_id: str
    position: TrackedPosition
    horizon_at: datetime
    protection_price: float
    first_protection_at: datetime | None = None
    first_initial_stop_at: datetime | None = None
    actual_close_reason: PositionCloseReason | None = None
    actual_close_at: datetime | None = None
    actual_net_return_percent: float | None = None
    actual_mfe_percent: float = 0.0
    actual_mae_percent: float = 0.0


class ManagedV2LifecycleLabeler:
    """Build V2 labels while delegating economic exits to the live engine.

    Opportunity observes the full configured horizon, including the path after
    a hypothetical initial stop. Economics stops exactly when the shared live
    lifecycle stops. That separation prevents Opportunity and Path Quality
    from collapsing into the same target.
    """

    def __init__(
        self,
        lifecycle_engine: PositionLifecycleEngine | None = None,
    ) -> None:
        self.lifecycle_engine = lifecycle_engine or PositionLifecycleEngine()
        self._states_by_symbol: dict[str, dict[str, _LabelState]] = {}
        self._completed: list[ManagedV2LifecycleLabels] = []

    def add(
        self,
        *,
        candidate_id: str,
        position: TrackedPosition,
    ) -> None:
        if not candidate_id:
            raise ValueError('MANAGED V2 label candidate_id is required.')
        states = self._states_by_symbol.setdefault(position.symbol, {})
        if candidate_id in states:
            raise ValueError(
                f'Duplicate MANAGED V2 label candidate: {candidate_id}.'
            )
        trigger = position.breakeven_trigger_percent
        protection_price = (
            position.pnl_entry_price * (1 + trigger / 100)
            if position.side == 'BUY'
            else position.pnl_entry_price * (1 - trigger / 100)
        )
        states[candidate_id] = _LabelState(
            candidate_id=candidate_id,
            position=position,
            horizon_at=position.opened_at
            + timedelta(minutes=position.stale_position_max_age_minutes),
            protection_price=protection_price,
        )

    def on_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        force_close: bool = False,
    ) -> None:
        states = self._states_by_symbol.get(snapshot.symbol)
        if not states:
            return
        for candidate_id, state in list(states.items()):
            if snapshot.timestamp <= state.position.opened_at:
                continue
            executable = snapshot.executable_exit_price(state.position.side)
            if state.first_protection_at is None and _favorable_threshold_hit(
                state.position.side,
                executable,
                state.protection_price,
            ):
                state.first_protection_at = snapshot.timestamp
            initial_stop = (
                state.position.initial_stop_loss or state.position.stop_loss
            )
            if state.first_initial_stop_at is None and _initial_stop_hit(
                state.position.side,
                executable,
                initial_stop,
            ):
                state.first_initial_stop_at = snapshot.timestamp

            if state.actual_close_reason is None:
                result = self.lifecycle_engine.evaluate(
                    position=state.position,
                    snapshot=snapshot,
                    force_close=force_close,
                )
                state.position = result.position
                if result.close_signal is not None:
                    pnl = calculate_position_pnl(
                        side=state.position.side,
                        amount=state.position.amount,
                        entry_price=state.position.pnl_entry_price,
                        exit_price=result.close_signal.executable_estimate,
                        explicit_cost=state.position.estimated_explicit_cost,
                        explicit_cost_percent=(
                            state.position.estimated_explicit_cost_percent
                        ),
                    )
                    state.actual_close_reason = result.close_signal.reason
                    state.actual_close_at = result.close_signal.detected_at
                    state.actual_net_return_percent = pnl.net_pnl_percent
                    state.actual_mfe_percent = position_mfe_percent(
                        state.position
                    )
                    state.actual_mae_percent = position_mae_percent(
                        state.position
                    )

            if force_close or snapshot.timestamp >= state.horizon_at:
                self._completed.append(
                    _finalize_labels(state, snapshot.timestamp)
                )
                del states[candidate_id]
        if not states:
            self._states_by_symbol.pop(snapshot.symbol, None)

    def consume_completed(self) -> tuple[ManagedV2LifecycleLabels, ...]:
        completed = tuple(self._completed)
        self._completed.clear()
        return completed

    def pending_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            candidate_id
            for states in self._states_by_symbol.values()
            for candidate_id in states
        )


def _finalize_labels(
    state: _LabelState,
    observation_ended_at: datetime,
) -> ManagedV2LifecycleLabels:
    opportunity = int(state.first_protection_at is not None)
    path_quality = None
    if opportunity:
        path_quality = int(
            state.first_initial_stop_at is None
            or state.first_protection_at < state.first_initial_stop_at
        )
    time_to_protection = (
        None
        if state.first_protection_at is None
        else max(
            0.0,
            (
                state.first_protection_at - state.position.opened_at
            ).total_seconds(),
        )
    )
    return ManagedV2LifecycleLabels(
        candidate_id=state.candidate_id,
        label_contract_version=MANAGED_V2_LABEL_CONTRACT_VERSION,
        opportunity=opportunity,
        path_quality=path_quality,
        net_return_percent=state.actual_net_return_percent,
        close_reason=(
            state.actual_close_reason.value
            if state.actual_close_reason is not None
            else None
        ),
        mfe_percent=state.actual_mfe_percent,
        mae_percent=state.actual_mae_percent,
        protection_threshold_percent=(
            state.position.breakeven_trigger_percent
        ),
        first_protection_at=state.first_protection_at,
        first_initial_stop_at=state.first_initial_stop_at,
        economics_closed_at=state.actual_close_at,
        observation_ended_at=observation_ended_at,
        time_to_protection_seconds=time_to_protection,
    )


def _favorable_threshold_hit(
    side: str,
    price: float,
    threshold: float,
) -> bool:
    return price >= threshold if side == 'BUY' else price <= threshold


def _initial_stop_hit(side: str, price: float, stop: float) -> bool:
    return price <= stop if side == 'BUY' else price >= stop
