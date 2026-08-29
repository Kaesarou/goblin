from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Iterable, Mapping

from app.v3.models import BrokerLeg, InventoryState, InventoryStatus, PortfolioState
from app.v3.persistence import InventoryEvent


_CLOSE_TOLERANCE = 1e-9


class InventoryBook:
    """In-memory projection of the append-only V3 inventory ledger."""

    def __init__(self) -> None:
        self._inventories: dict[str, InventoryState] = {}
        self._inventory_by_position_id: dict[str, str] = {}

    @classmethod
    def from_events(cls, events: Iterable[InventoryEvent]) -> "InventoryBook":
        book = cls()
        for event in events:
            if event.event_type == "ENTRY_FILLED":
                payload = event.payload
                units = float(payload["units"])
                price = float(payload["price"])
                book.apply_entry_fill(
                    inventory_id=event.inventory_id,
                    symbol=str(payload["symbol"]),
                    position_id=str(payload["position_id"]),
                    units=units,
                    price=price,
                    account_notional=float(
                        payload.get("notional", units * price)
                    ),
                    fee=float(payload.get("fee", 0.0)),
                    filled_at=event.occurred_at,
                )
            elif event.event_type == "BROKER_QUANTITY_RECONCILED":
                payload = event.payload
                book.reconcile_broker_leg_units(
                    position_id=str(payload["position_id"]),
                    broker_units=float(payload["broker_units"]),
                    observed_at=event.occurred_at,
                )
            elif event.event_type == "EXIT_ECONOMICS_CONFIRMED":
                payload = event.payload
                book.apply_exit_economics(
                    inventory_id=event.inventory_id,
                    position_id=str(payload["position_id"]),
                    exit_price=float(payload["price"]),
                    units=float(payload["units"]),
                    entry_price_basis=float(payload["entry_price_basis"]),
                    fee=float(payload.get("fee", 0.0)),
                )
            elif event.event_type == "EXIT_FILLED":
                payload = event.payload
                book.apply_exit_fill(
                    position_id=str(payload["position_id"]),
                    exit_price=float(payload["price"]),
                    units=(
                        None
                        if payload.get("units") is None
                        else float(payload["units"])
                    ),
                    fee=float(payload.get("fee", 0.0)),
                    filled_at=event.occurred_at,
                )
        return book

    @property
    def inventories(self) -> tuple[InventoryState, ...]:
        return tuple(self._inventories.values())

    def active_for_symbol(self, symbol: str) -> InventoryState | None:
        normalized = symbol.strip().upper()
        for inventory in self._inventories.values():
            if inventory.symbol == normalized and inventory.status != InventoryStatus.CLOSED:
                return inventory
        return None

    def active_broker_position_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._inventory_by_position_id))

    def apply_entry_fill(
        self,
        *,
        inventory_id: str,
        symbol: str,
        position_id: str,
        units: float,
        price: float,
        fee: float,
        filled_at: datetime,
        account_notional: float | None = None,
    ) -> InventoryState:
        if position_id in self._inventory_by_position_id:
            raise ValueError(f"Broker position already applied: {position_id}")
        if units <= 0 or price <= 0:
            raise ValueError("Entry fill requires positive units and price")
        actual_account_notional = (
            units * price
            if account_notional is None
            else float(account_notional)
        )
        if actual_account_notional <= 0:
            raise ValueError("Entry fill requires positive account notional")
        normalized = symbol.strip().upper()
        existing = self._inventories.get(inventory_id)
        leg = BrokerLeg(
            position_id,
            units,
            price,
            filled_at,
            side="BUY",
            account_notional=actual_account_notional,
        )
        if existing is None:
            if self.active_for_symbol(normalized) is not None:
                raise ValueError(f"Multiple active inventories for {normalized}")
            updated = InventoryState(
                inventory_id=inventory_id,
                symbol=normalized,
                opened_at=filled_at,
                total_units=units,
                average_entry_price=price,
                entry_fill_count=1,
                last_entry_at=filled_at,
                last_entry_price=price,
                total_notional=actual_account_notional,
                wallet_exposure_pct=0.0,
                initial_entry_units=units,
                fees_paid=fee,
                last_fill_at=filled_at,
                min_price_since_last_entry=price,
                max_price_since_last_entry=price,
                min_price_since_open=price,
                max_price_since_open=price,
                broker_legs=(leg,),
            )
        else:
            if existing.status == InventoryStatus.CLOSED:
                raise ValueError(f"Cannot add a fill to closed inventory {inventory_id}")
            old_cost = sum(item.units * item.entry_price for item in existing.broker_legs)
            total_units = existing.total_units + units
            total_cost = old_cost + units * price
            updated = replace(
                existing,
                total_units=total_units,
                average_entry_price=total_cost / total_units,
                entry_fill_count=existing.entry_fill_count + 1,
                last_entry_at=filled_at,
                last_entry_price=price,
                total_notional=(
                    existing.total_notional + actual_account_notional
                ),
                fees_paid=existing.fees_paid + fee,
                last_fill_at=filled_at,
                trailing_min_since_open=None,
                trailing_max_since_min=None,
                trailing_max_since_open=None,
                trailing_min_since_max=None,
                min_price_since_last_entry=price,
                max_price_since_last_entry=price,
                broker_legs=existing.broker_legs + (leg,),
            )
        self._inventories[inventory_id] = updated
        self._inventory_by_position_id[position_id] = inventory_id
        return updated

    def reconcile_broker_leg_units(
        self,
        *,
        position_id: str,
        broker_units: float,
        observed_at: datetime,
    ) -> InventoryState:
        """Apply broker-authoritative remaining units without inventing economics.

        This operation is intentionally reduce-only. It is used when the broker
        portfolio proves that an already-known position contains fewer units than
        the append-only economic ledger. The missing quantity may represent a
        previously executed close whose price/fees are still unknown. Exposure is
        reconciled immediately, while realized PnL is finalized later by a
        separate ``EXIT_ECONOMICS_CONFIRMED`` event.
        """

        try:
            inventory_id = self._inventory_by_position_id[position_id]
        except KeyError as exc:
            raise KeyError(f"Unknown broker position reconciliation: {position_id}") from exc
        inventory = self._inventories[inventory_id]
        leg = next(item for item in inventory.broker_legs if item.position_id == position_id)
        actual_units = float(broker_units)
        if actual_units < -_CLOSE_TOLERANCE:
            raise ValueError("Broker reconciliation requires non-negative units")
        if actual_units > leg.units + _CLOSE_TOLERANCE:
            raise ValueError(
                f"Broker reconciliation cannot increase units: position_id={position_id}, "
                f"broker_units={actual_units}, leg_units={leg.units}"
            )
        actual_units = max(0.0, min(actual_units, leg.units))
        if abs(actual_units - leg.units) <= _CLOSE_TOLERANCE:
            return inventory

        leg_account_notional = _leg_account_notional(leg)
        if actual_units <= _CLOSE_TOLERANCE:
            self._inventory_by_position_id.pop(position_id, None)
            remaining_legs = tuple(
                item for item in inventory.broker_legs if item.position_id != position_id
            )
        else:
            remaining_leg = replace(
                leg,
                units=actual_units,
                account_notional=(leg_account_notional * actual_units / leg.units),
            )
            remaining_legs = tuple(
                remaining_leg if item.position_id == position_id else item
                for item in inventory.broker_legs
            )

        if not remaining_legs:
            updated = replace(
                inventory,
                total_units=0.0,
                total_notional=0.0,
                wallet_exposure_pct=0.0,
                last_fill_at=observed_at,
                broker_legs=(),
                status=InventoryStatus.CLOSED,
            )
        else:
            total_units = sum(item.units for item in remaining_legs)
            cost = sum(item.units * item.entry_price for item in remaining_legs)
            account_notional = sum(
                _leg_account_notional(item) for item in remaining_legs
            )
            updated = replace(
                inventory,
                total_units=total_units,
                average_entry_price=cost / total_units,
                total_notional=account_notional,
                last_fill_at=observed_at,
                broker_legs=remaining_legs,
                trailing_min_since_open=None,
                trailing_max_since_min=None,
                trailing_max_since_open=None,
                trailing_min_since_max=None,
            )
        self._inventories[inventory_id] = updated
        return updated

    def apply_exit_economics(
        self,
        *,
        inventory_id: str,
        position_id: str,
        exit_price: float,
        units: float,
        entry_price_basis: float,
        fee: float,
    ) -> InventoryState:
        """Finalize PnL for quantity that was already reconciled from the broker."""

        inventory = self._inventories.get(inventory_id)
        if inventory is None:
            raise KeyError(f"Unknown inventory economics confirmation: {inventory_id}")
        actual_units = float(units)
        actual_exit_price = float(exit_price)
        actual_entry_price = float(entry_price_basis)
        if actual_units <= 0 or actual_exit_price <= 0 or actual_entry_price <= 0:
            raise ValueError("Exit economics confirmation requires positive units/prices")
        realized = actual_units * (actual_exit_price - actual_entry_price)
        updated = replace(
            inventory,
            realized_pnl=inventory.realized_pnl + realized,
            fees_paid=inventory.fees_paid + float(fee),
        )
        self._inventories[inventory_id] = updated
        return updated

    def apply_exit_fill(
        self,
        *,
        position_id: str,
        exit_price: float,
        fee: float,
        filled_at: datetime,
        units: float | None = None,
    ) -> InventoryState:
        try:
            inventory_id = self._inventory_by_position_id[position_id]
        except KeyError as exc:
            raise KeyError(f"Unknown broker position close: {position_id}") from exc
        inventory = self._inventories[inventory_id]
        leg = next(item for item in inventory.broker_legs if item.position_id == position_id)
        close_units = leg.units if units is None else float(units)
        if close_units <= 0:
            raise ValueError("Exit fill requires positive units")
        if close_units > leg.units + _CLOSE_TOLERANCE:
            raise ValueError(
                f"Exit fill exceeds broker leg: position_id={position_id}, "
                f"close_units={close_units}, leg_units={leg.units}"
            )
        close_units = min(close_units, leg.units)
        remaining_units = max(0.0, leg.units - close_units)
        realized = close_units * (exit_price - leg.entry_price)
        leg_account_notional = _leg_account_notional(leg)

        if remaining_units <= _CLOSE_TOLERANCE:
            self._inventory_by_position_id.pop(position_id, None)
            remaining_legs = tuple(
                item for item in inventory.broker_legs if item.position_id != position_id
            )
        else:
            remaining_leg = replace(
                leg,
                units=remaining_units,
                account_notional=(
                    leg_account_notional * remaining_units / leg.units
                ),
            )
            remaining_legs = tuple(
                remaining_leg if item.position_id == position_id else item
                for item in inventory.broker_legs
            )

        if not remaining_legs:
            updated = replace(
                inventory,
                total_units=0.0,
                total_notional=0.0,
                wallet_exposure_pct=0.0,
                realized_pnl=inventory.realized_pnl + realized,
                fees_paid=inventory.fees_paid + fee,
                last_fill_at=filled_at,
                broker_legs=(),
                status=InventoryStatus.CLOSED,
            )
        else:
            total_units = sum(item.units for item in remaining_legs)
            cost = sum(item.units * item.entry_price for item in remaining_legs)
            account_notional = sum(
                _leg_account_notional(item) for item in remaining_legs
            )
            updated = replace(
                inventory,
                total_units=total_units,
                average_entry_price=cost / total_units,
                total_notional=account_notional,
                realized_pnl=inventory.realized_pnl + realized,
                fees_paid=inventory.fees_paid + fee,
                last_fill_at=filled_at,
                broker_legs=remaining_legs,
                trailing_min_since_open=None,
                trailing_max_since_min=None,
                trailing_max_since_open=None,
                trailing_min_since_max=None,
            )
        self._inventories[inventory_id] = updated
        return updated

    def observe_candle(self, *, symbol: str, high: float, low: float, close: float) -> None:
        inventory = self.active_for_symbol(symbol)
        if inventory is None:
            return
        tmin = inventory.trailing_min_since_open
        tmaxmin = inventory.trailing_max_since_min
        tmax = inventory.trailing_max_since_open
        tminmax = inventory.trailing_min_since_max
        if tmin is None or low < tmin:
            tmin = low
            tmaxmin = close
        else:
            tmaxmin = max(tmaxmin if tmaxmin is not None else close, high)
        if tmax is None or high > tmax:
            tmax = high
            tminmax = close
        else:
            tminmax = min(tminmax if tminmax is not None else close, low)
        min_last = min(
            value for value in (inventory.min_price_since_last_entry, low) if value is not None
        )
        max_last = max(
            value for value in (inventory.max_price_since_last_entry, high) if value is not None
        )
        min_open = min(
            value for value in (inventory.min_price_since_open, low) if value is not None
        )
        max_open = max(
            value for value in (inventory.max_price_since_open, high) if value is not None
        )
        self._inventories[inventory.inventory_id] = replace(
            inventory,
            trailing_min_since_open=tmin,
            trailing_max_since_min=tmaxmin,
            trailing_max_since_open=tmax,
            trailing_min_since_max=tminmax,
            min_price_since_last_entry=min_last,
            max_price_since_last_entry=max_last,
            min_price_since_open=min_open,
            max_price_since_open=max_open,
            mfe_pct=max(inventory.mfe_pct, max_open / inventory.average_entry_price - 1.0),
            mae_pct=min(inventory.mae_pct, min_open / inventory.average_entry_price - 1.0),
        )

    def portfolio(
        self,
        *,
        equity: float,
        symbol_betas: Mapping[str, float] | None = None,
    ) -> PortfolioState:
        active = tuple(
            replace(
                inventory,
                wallet_exposure_pct=(
                    inventory.total_notional / equity if equity > 0 else float("inf")
                ),
            )
            for inventory in self._inventories.values()
            if inventory.status != InventoryStatus.CLOSED
        )
        return PortfolioState(
            equity=equity,
            inventories=active,
            symbol_betas=dict(symbol_betas or {}),
        )


def _leg_account_notional(leg: BrokerLeg) -> float:
    value = leg.account_notional
    return leg.units * leg.entry_price if value is None else float(value)
