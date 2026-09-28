from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from uuid import uuid4

import requests

from app.brokers.alpaca.order_store import AlpacaOrderStore
from app.brokers.alpaca.schema import TERMINAL_STATUSES, number, text, timestamp
from app.brokers.alpaca.stream import AlpacaStream
from app.brokers.base import (
    BrokerAccountPreflight,
    BrokerClient,
    BrokerCloseExecution,
    BrokerOpenOrder,
    BrokerPositionReconciliation,
    ClosePositionRejectedError,
    ClosePositionSubmission,
    ClosePositionSubmissionUnknownError,
    OpenPositionRejectedError,
    OpenPositionResult,
)


class AlpacaBrokerClient(BrokerClient):
    account_equity_source = "alpaca_account_equity"
    requires_external_activity_ack = True

    def __init__(
        self,
        http,
        store: AlpacaOrderStore,
        *,
        api_key: str,
        secret_key: str,
        fill_timeout_seconds: float = 20.0,
        instrument_cache=None,
    ) -> None:
        if getattr(http, "environment", store.environment) != store.environment:
            raise ValueError("Alpaca HTTP and journal environments differ")
        self.http, self.store = http, store
        self.instrument_cache = instrument_cache
        self.fill_timeout_seconds = fill_timeout_seconds
        self._mutation_lock = threading.RLock()
        self._updated = threading.Event()
        self._stream_error: Exception | None = None
        self._account_verified = False
        self.trade_stream = AlpacaStream(
            api_key=api_key,
            secret_key=secret_key,
            on_message=self._on_trade_update,
            environment=store.environment,
        )

    def _account(self) -> dict:
        account = self.http.request("GET", "/v2/account")
        if not isinstance(account, dict) or account.get("currency") != "USD":
            raise ValueError("Alpaca requires an authoritative USD account")
        self.store.bind_account(account.get("id"))
        self._account_verified = True
        return account

    def get_account_equity(self) -> float:
        return float(number(self._account().get("equity"), positive=True))

    def get_account_identity(self) -> str:
        return text(self._account().get("id"))

    def _validate_asset(self, symbol: str, *, trading: bool) -> None:
        asset = (self.instrument_cache.get_asset(symbol) if self.instrument_cache
                 else self.http.request("GET", "/v2/assets/" + symbol))
        if (not isinstance(asset, dict) or asset.get("symbol") != symbol
                or asset.get("class") != "us_equity" or asset.get("status") != "active"
                or (trading and (asset.get("tradable") is not True
                                 or asset.get("fractionable") is not True))):
            raise ValueError(f"Alpaca asset is not an active {'fractional ' if trading else ''}US equity: {symbol}")

    def validate_universe(self, symbols: list[str], *, context_symbols: list[str]) -> None:
        trading = set(symbols)
        for symbol in sorted(trading | set(context_symbols)):
            self._validate_asset(symbol, trading=symbol in trading)

    def _assert_mutation_allowed(self) -> None:
        self.store.check_health()
        if self._stream_error:
            raise RuntimeError(
                "Alpaca order stream evidence is inconsistent"
            ) from self._stream_error
        account = self._account()
        if (
            account.get("status") != "ACTIVE"
            or account.get("trading_blocked") is not False
            or account.get("account_blocked") is not False
        ):
            raise ValueError("Alpaca account is not authorized to trade")
        clock = self.http.request("GET", "/v2/clock")
        if not isinstance(clock, dict) or clock.get("is_open") is not True:
            raise ValueError("Alpaca market is closed; do not queue a market order")

    def _lookup(self, client_id: str) -> dict | None:
        self.store.check_health()
        if not self._account_verified:
            self._account()
        try:
            payload = self.http.request(
                "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_id}
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None  # Absence is not a rejection after an uncertain POST.
            raise
        if not isinstance(payload, dict) or payload.get("client_order_id") != client_id:
            raise ValueError("Alpaca lookup returned a different order")
        return self.store.observe(payload)

    def _refresh_pending(self) -> None:
        self.store.check_health()
        if self._stream_error:
            raise RuntimeError(
                "Alpaca order stream evidence is inconsistent"
            ) from self._stream_error
        for row in self.store.pending():
            self._lookup(row["client_id"])

    def _broker_positions(self) -> dict[str, Decimal]:
        payload = self.http.request("GET", "/v2/positions")
        if not isinstance(payload, list):
            raise ValueError("Missing Alpaca positions collection")
        positions = {}
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Invalid Alpaca position")
            symbol = text(item.get("symbol"))
            if symbol in positions or item.get("side") != "long":
                raise ValueError("Unsupported or duplicate Alpaca position")
            positions[symbol] = number(item.get("qty"), positive=True)
        return positions

    def _reconciled_positions(
        self, *, allow_external: bool = False, close_order_ids: dict[str, str] | None = None,
    ):
        if not self._account_verified:
            self._account()
        self._refresh_pending()
        broker = self._broker_positions()
        legs, close_fills = self.store.position_snapshot(close_order_ids)
        expected: dict[str, Decimal] = {}
        for symbol, qty in legs.values():
            expected[symbol] = expected.get(symbol, Decimal(0)) + qty
        external = {}
        for symbol in broker.keys() | expected.keys():
            difference = broker.get(symbol, Decimal(0)) - expected.get(symbol, Decimal(0))
            if abs(difference) <= Decimal("0.000000001"):
                continue
            if not allow_external or expected.get(symbol, 0):
                raise ValueError(f"Alpaca aggregate position mismatch for {symbol}")
            external[f"alpaca-external:{symbol}"] = float(abs(difference))
        return legs, external, close_fills

    def get_account_preflight(self) -> BrokerAccountPreflight:
        self._account()
        legs, external, _ = self._reconciled_positions(allow_external=True)
        orders = self._open_orders()
        known = {row["client_id"] for row in self.store.rows()}
        pending = [
            item["client_order_id"]
            for item in orders
            if item["client_order_id"] not in known or item["side"] == "buy"
        ]
        # A request whose response was lost might not yet appear in open orders.
        unresolved = self.store.pending()
        pending.extend(
            row["client_id"]
            for row in unresolved
            if row["side"] == "buy"
        )
        return BrokerAccountPreflight(
            {**{key: float(qty) for key, (_, qty) in legs.items() if qty}, **external},
            tuple(sorted(set(pending))),
            {row["client_id"]: row["position_id"] for row in unresolved if row["side"] == "sell"},
            {row["client_id"]: self._open_order(row)
             for row in self.store.rows() if row["side"] == "buy"},
        )

    def _open_orders(self) -> list[dict]:
        orders = self.http.request("GET", "/v2/orders", params={"status": "open", "limit": 500})
        if not isinstance(orders, list) or len(orders) >= 500:
            raise ValueError("Alpaca pending orders are invalid or possibly truncated")
        for item in orders:
            if not isinstance(item, dict):
                raise ValueError("Invalid Alpaca pending order")
            text(item.get("client_order_id"))
            text(item.get("symbol"))
            if item.get("side") not in {"buy", "sell"}:
                raise ValueError("Invalid Alpaca pending side")
        return orders

    def prepare_open_order_id(self, action_id: str) -> str:
        return "goblin-" + uuid4().hex

    def open_position(
        self, symbol, side, amount, stop_loss, take_profit, *, client_order_id=None,
    ) -> OpenPositionResult:
        with self._mutation_lock:
            # V3 controls exits; bracket orders would alter its strategy lifecycle.
            try:
                if side.upper() != "BUY":
                    raise ValueError("Alpaca V3 supports long inventory only")
                notional = number(amount, positive=True).quantize(Decimal("0.01"), ROUND_DOWN)
                if notional < 1:
                    raise ValueError("Alpaca fractional order must be at least USD 1")
                self._assert_mutation_allowed()
                self._validate_asset(symbol, trading=True)
                preflight = self.get_account_preflight()
                if (
                    preflight.pending_open_orders
                    or any(not row["response"] for row in self.store.pending())
                    or any(key.startswith("alpaca-external:") for key in preflight.position_units)
                ):
                    raise ValueError("Alpaca account contains external or pending exposure")
            except (ValueError, InvalidOperation) as exc:
                raise OpenPositionRejectedError(str(exc)) from exc
            client_id = text(client_order_id) if client_order_id is not None else "goblin-" + uuid4().hex
            request = {
                "symbol": symbol,
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "notional": str(notional),
                "client_order_id": client_id,
                "extended_hours": False,
            }
            self.store.reserve(request, position_id=client_id)
            row = self.store.get(client_id)
            # Exactly one POST. Any exception keeps the durable reservation unknown.
            payload = self.http.request("POST", "/v2/orders", json=request)
            observed = self.store.observe(payload)
            deadline = time.monotonic() + self.fill_timeout_seconds
            while True:
                execution = self._open_execution(row, observed)
                if execution is not None:
                    return execution
                if time.monotonic() >= deadline:
                    raise TimeoutError("Alpaca BUY outcome pending; reservation retained")
                self._updated.wait(timeout=0.5)
                self._updated.clear()
                observed = self._lookup(client_id)

    @staticmethod
    def _open_order(row: dict, payload: dict | None = None) -> BrokerOpenOrder:
        request = json.loads(row["request"])
        observed = payload or (json.loads(row["response"]) if row["response"] else {})
        return BrokerOpenOrder(
            row["client_id"], row["position_id"], row["symbol"],
            float(number(request["notional"], positive=True)), observed.get("status"),
        )

    def _open_execution(self, row: dict, payload: dict | None) -> OpenPositionResult | None:
        if payload is None or payload["status"] not in TERMINAL_STATUSES:
            return None
        qty = number(payload["filled_qty"])
        if not qty:
            raise OpenPositionRejectedError("Alpaca confirmed a terminal unfilled BUY")
        price = number(payload["filled_avg_price"], positive=True)
        notional = number(price * qty, positive=True)
        return OpenPositionResult(
            row["position_id"], float(price), float(qty), float(notional),
            self._open_order(row, payload),
        )

    def get_open_execution(self, order_id, symbol, requested_notional):
        row = self.store.get(order_id)
        requested = number(requested_notional, positive=True).quantize(Decimal("0.01"), ROUND_DOWN)
        if (row["side"] != "buy" or row["position_id"] != order_id
                or row["symbol"] != symbol
                or number(json.loads(row["request"])["notional"]) != requested):
            raise ValueError("Alpaca open identity does not match the requested BUY")
        return self._open_execution(row, self._lookup(order_id))

    def prepare_close_order_id(self, action_id: str) -> str:
        return "goblin-" + uuid4().hex

    def close_position(
        self, position_id, units_to_deduct=None, *, client_order_id=None,
    ) -> ClosePositionSubmission:
        with self._mutation_lock:
            try:
                self._assert_mutation_allowed()
                legs, _, _ = self._reconciled_positions()
                if position_id not in legs:
                    raise ValueError("Unknown Alpaca opening leg")
                symbol, remaining = legs[position_id]
                known = {row["client_id"] for row in self.store.rows()}
                if any(
                    item["symbol"] == symbol and item["client_order_id"] not in known
                    for item in self._open_orders()
                ):
                    raise ValueError("External pending Alpaca order on the closing symbol")
                qty = (
                    remaining
                    if units_to_deduct is None
                    else number(units_to_deduct, positive=True).quantize(
                        Decimal("0.000000001"), ROUND_DOWN
                    )
                )
                if qty <= 0 or qty > remaining:
                    raise ValueError("Alpaca close exceeds remaining leg quantity")
                client_id = text(client_order_id) if client_order_id is not None else "goblin-" + uuid4().hex
                request = {
                    "symbol": symbol,
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day",
                    "qty": format(qty, "f"),
                    "client_order_id": client_id,
                    "extended_hours": False,
                    "position_intent": "sell_to_close",
                }
                self.store.reserve(request, position_id=position_id)
            except (ValueError, InvalidOperation) as exc:
                raise ClosePositionRejectedError(position_id=position_id, message=str(exc)) from exc
            submitted_at = datetime.now(UTC)
            try:
                payload = self.http.request("POST", "/v2/orders", json=request)
                if self.store.observe(payload) is None:
                    raise ValueError("Alpaca returned an unrelated order")
            except Exception as exc:
                raise ClosePositionSubmissionUnknownError(
                    position_id=position_id,
                    submitted_at=submitted_at,
                    cause=exc,
                    close_order_id=client_id,
                    reference_id=client_id,
                ) from exc
            return ClosePositionSubmission(
                position_id, client_id, client_id, submitted_at, datetime.now(UTC), payload
            )

    def get_close_execution(self, close_order_id, position_id) -> BrokerCloseExecution | None:
        row = self.store.get(close_order_id)
        if row["side"] != "sell" or row["position_id"] != position_id:
            raise ValueError("Alpaca close identity does not own the requested leg")
        payload = self._lookup(close_order_id)
        if payload is None or payload["status"] not in TERMINAL_STATUSES:
            return None
        qty = number(payload["filled_qty"])
        if not qty:
            raise ClosePositionRejectedError(
                position_id=position_id,
                message="Alpaca confirmed a terminal unfilled SELL",
                broker_response=payload,
            )
        price = number(payload["filled_avg_price"], positive=True)
        return BrokerCloseExecution(
            position_id,
            close_order_id,
            float(price),
            timestamp(payload.get("filled_at") or payload["updated_at"]),
            float(qty),
            1.0,
            float(price * qty),
            payload,
            broker_execution_position_id=position_id,
        )

    def get_open_position_units(self, position_ids) -> dict[str, float | None]:
        legs, _, _ = self._reconciled_positions()
        return {key: float(legs[key][1]) if key in legs else None for key in position_ids}

    def get_position_reconciliation(self, position_ids, *, close_order_ids):
        legs, _, fills = self._reconciled_positions(close_order_ids=close_order_ids)
        return BrokerPositionReconciliation(
            {key: float(legs[key][1]) if key in legs else None for key in position_ids},
            {key: float(qty) for key, qty in fills.items()},
        )

    def is_position_open(self, position_id: str) -> bool:
        units = self.get_open_position_units([position_id])[position_id]
        if units is None:
            raise ValueError("Alpaca leg identity is unknown")
        return units > 0

    def remember_position_instrument(self, position_id: str, symbol: str) -> None:
        row = self.store.get(position_id)
        if row["side"] != "buy" or row["symbol"] != symbol:
            raise ValueError("Alpaca persisted leg does not match V3 inventory")

    def _on_trade_update(self, message: dict) -> None:
        try:
            self.store.observe(message.get("order"))
        except Exception as exc:
            self._stream_error = exc
            self.store.record_fault(type(exc).__name__)
        finally:
            self._updated.set()

    def get_rate_limit_metrics(self) -> dict[str, object]:
        return {
            "alpaca": {
                "requests": self.http.calls,
                "rate_limits": self.http.rate_limits,
                "trade_updates": self.trade_stream.diagnostics(),
            }
        }
