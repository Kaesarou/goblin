from decimal import Decimal

import pytest
import requests

from app.brokers.alpaca.client import AlpacaBrokerClient
from app.brokers.alpaca.order_store import AlpacaOrderStore
from app.brokers.base import (
    ClosePositionRejectedError,
    ClosePositionSubmissionUnknownError,
    OpenPositionRejectedError,
)


class TradingApi:
    def __init__(self):
        self.orders = {}
        self.submissions = []
        self.external = {}
        self.account = {
            "id": "paper-account",
            "currency": "USD",
            "equity": "100000",
            "status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
        }
        self.open = True
        self.next_status = "filled"
        self.next_qty = None
        self.timeout_after_post = False
        self.tick = 0

    def request(self, method, path, *, params=None, json=None):
        if method == "POST":
            self.submissions.append(json)
            key = json["client_order_id"]
            self.orders[key] = {
                **json,
                "id": "order-" + str(len(self.orders)),
                "asset_class": "us_equity",
            }
            qty = (
                self.next_qty
                if self.next_qty is not None
                else Decimal(json.get("qty", "0")) or Decimal(json["notional"]) / 100
            )
            self.fill(key, qty if self.next_status != "new" else 0, self.next_status)
            if self.timeout_after_post:
                raise requests.Timeout("Response lost after broker received order")
            return dict(self.orders[key])
        if path == "/v2/account":
            return dict(self.account)
        if path == "/v2/clock":
            return {"is_open": self.open}
        if path.startswith("/v2/assets/"):
            return {
                "symbol": path.rsplit("/", 1)[1],
                "class": "us_equity",
                "status": "active",
                "tradable": True,
                "fractionable": True,
            }
        if path == "/v2/orders:by_client_order_id":
            if params["client_order_id"] not in self.orders:
                response = requests.Response()
                response.status_code = 404
                raise requests.HTTPError(response=response)
            return dict(self.orders[params["client_order_id"]])
        if path == "/v2/orders":
            return [
                dict(order)
                for order in self.orders.values()
                if order["status"] in {"new", "partially_filled"}
            ]
        if path == "/v2/positions":
            positions = dict(self.external)
            for order in self.orders.values():
                sign = 1 if order["side"] == "buy" else -1
                positions[order["symbol"]] = positions.get(
                    order["symbol"], Decimal(0)
                ) + sign * Decimal(order["filled_qty"])
            return [
                {"symbol": key, "side": "long", "qty": str(qty)}
                for key, qty in positions.items()
                if qty
            ]
        raise AssertionError((method, path, params, json))

    def fill(self, key, qty, status="filled", price="100"):
        self.tick += 1
        self.orders[key].update(
            status=status,
            filled_qty=str(qty),
            filled_avg_price=price,
            updated_at=f"2026-09-25T14:00:{self.tick:02}Z",
            filled_at=f"2026-09-25T14:00:{self.tick:02}Z" if qty else None,
        )


def broker(tmp_path, api=None):
    api = api or TradingApi()
    return AlpacaBrokerClient(
        api,
        AlpacaOrderStore(str(tmp_path / "alpaca.sqlite")),
        api_key="key",
        secret_key="secret",
        fill_timeout_seconds=0,
    ), api


def buy(client, amount=300):
    return client.open_position("AAPL", "BUY", amount, 50, 1000)


def test_multiple_buys_partial_closes_duplicates_and_restart(tmp_path):
    client, api = broker(tmp_path)
    first, second = buy(client), buy(client, 200)
    assert first.position_id != second.position_id
    assert first.executed_units == 3
    assert first.executed_notional == 300
    assert api.submissions[0]["notional"] == "300.00"
    assert "qty" not in api.submissions[0]
    one = client.close_position(first.position_id, 2.52)
    two = client.close_position(second.position_id, 1.68)
    assert api.submissions[-1]["position_intent"] == "sell_to_close"
    for submission in (one, two):
        execution = client.get_close_execution(submission.close_order_id, submission.position_id)
        assert execution.executed_exit_price == 100
        assert (
            client.get_close_execution(submission.close_order_id, submission.position_id)
            == execution
        )
        client._on_trade_update({"event": "fill", "order": api.orders[submission.close_order_id]})
    restarted, _ = broker(tmp_path, api)
    restarted.remember_position_instrument(first.position_id, "AAPL")
    assert restarted.get_open_position_units([first.position_id, second.position_id]) == {
        first.position_id: 0.48,
        second.position_id: 0.32,
    }
    restarted.close_position(first.position_id)
    assert Decimal(api.submissions[-1]["qty"]) == Decimal("0.48")
    assert restarted.get_open_position_units([first.position_id, second.position_id]) == {
        first.position_id: 0,
        second.position_id: 0.32,
    }


def test_uncertain_sell_is_recovered_by_client_id_without_another_post(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    api.next_status = "new"
    api.timeout_after_post = True
    with pytest.raises(ClosePositionSubmissionUnknownError) as error:
        client.close_position(position, 1)
    key = error.value.close_order_id
    assert key in api.orders
    restarted, _ = broker(tmp_path, api)
    assert restarted.get_close_execution(key, position) is None
    api.fill(key, 1)
    assert restarted.get_close_execution(key, position).units == 1
    assert restarted.get_open_position_units([position]) == {position: 2}
    assert len(api.submissions) == 2


def test_uncertain_buy_reservation_survives_restart_and_blocks_another_buy(tmp_path):
    client, api = broker(tmp_path)
    api.next_status = "new"
    api.timeout_after_post = True
    with pytest.raises(requests.Timeout):
        buy(client)
    restarted, _ = broker(tmp_path, api)
    pending = restarted.get_account_preflight().pending_open_orders
    assert pending == (api.submissions[0]["client_order_id"],)
    with pytest.raises(OpenPositionRejectedError, match="pending"):
        buy(restarted)
    assert len(api.submissions) == 1
    api.fill(pending[0], 3)
    assert restarted.get_account_preflight().position_units == {pending[0]: 3}


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_terminal_buy_uses_actual_partial_fill_or_explicit_no_fill(tmp_path, status):
    client, api = broker(tmp_path)
    api.next_status, api.next_qty = status, Decimal("1.2")
    result = buy(client)
    assert result.executed_units == 1.2
    assert result.executed_notional == 120
    api.next_qty = 0
    with pytest.raises(OpenPositionRejectedError, match="terminal unfilled"):
        buy(client)


def test_sell_pending_partial_fill_is_not_reported_as_complete(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    submission = client.close_position(position, 1)
    assert client.get_close_execution(submission.close_order_id, position) is None
    with pytest.raises(ClosePositionRejectedError, match="Unresolved"):
        client.close_position(position, 1)
    api.fill(submission.close_order_id, "0.5", "canceled")
    assert client.get_close_execution(submission.close_order_id, position).units == 0.5
    assert client.get_open_position_units([position]) == {position: 2.5}


def test_terminal_sell_rejection_preserves_position(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    api.next_status, api.next_qty = "rejected", 0
    submission = client.close_position(position, 1)
    with pytest.raises(ClosePositionRejectedError, match="terminal unfilled"):
        client.get_close_execution(submission.close_order_id, position)
    assert client.get_open_position_units([position]) == {position: 3}


@pytest.mark.parametrize("bad_quantity", [0, -1, float("nan"), True, 3.1])
def test_no_sell_can_exceed_or_invent_leg_quantity(tmp_path, bad_quantity):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    with pytest.raises(ClosePositionRejectedError):
        client.close_position(position, bad_quantity)
    assert len(api.submissions) == 1


def test_external_positions_are_reported_and_never_allocated_to_known_legs(tmp_path):
    client, api = broker(tmp_path)
    api.external = {"MSFT": Decimal(2)}
    assert client.get_account_preflight().position_units == {"alpaca-external:MSFT": 2}
    with pytest.raises(OpenPositionRejectedError, match="external"):
        buy(client)
    api.external = {}
    position = buy(client).position_id
    api.external = {"AAPL": Decimal("-1")}
    with pytest.raises(ValueError, match="aggregate position mismatch"):
        client.get_open_position_units([position])
    assert client.store.positions()[position][1] == 3


def test_account_switch_cannot_reuse_persisted_leg_journal(tmp_path):
    client, api = broker(tmp_path)
    buy(client)
    api.account["id"] = "different-paper-account"
    restarted, _ = broker(tmp_path, api)
    with pytest.raises(ValueError, match="another account"):
        restarted.get_account_preflight()


def test_market_closed_does_not_queue_next_day_order(tmp_path):
    client, api = broker(tmp_path)
    api.open = False
    with pytest.raises(OpenPositionRejectedError, match="market is closed"):
        buy(client)
    assert not api.submissions


def test_stale_events_do_not_rewind_cumulative_fill_and_wrong_identity_fails(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    payload = api.orders[position]
    old = {**payload, "updated_at": "2026-09-25T13:00:00Z", "filled_qty": "0", "status": "new"}
    assert client.store.observe(old) == payload
    with pytest.raises(ValueError, match="identity conflicts"):
        client.store.observe({**payload, "symbol": "MSFT"})
    with pytest.raises(ValueError, match="regressed"):
        client.store.observe({**payload, "filled_qty": "1"})
    assert client.store.positions()[position][1] == 3


def test_missing_order_after_timeout_is_not_proof_of_rejection(tmp_path):
    class LostRequestApi(TradingApi):
        def request(self, method, path, **kwargs):
            if method == "POST":
                self.submissions.append(kwargs["json"])
                raise requests.Timeout("No evidence whether Alpaca accepted the order")
            return super().request(method, path, **kwargs)

    client, api = broker(tmp_path, LostRequestApi())
    with pytest.raises(requests.Timeout):
        buy(client)
    restarted, _ = broker(tmp_path, api)
    assert restarted.get_account_preflight().pending_open_orders
    with pytest.raises(OpenPositionRejectedError, match="pending"):
        buy(restarted)
    assert len(api.submissions) == 1


def test_stream_execution_conflict_survives_restart_as_safety_fault(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    client._on_trade_update(
        {
            "event": "fill",
            "order": {
                **api.orders[position],
                "filled_avg_price": "105",
            },
        }
    )
    restarted, _ = broker(tmp_path, api)
    with pytest.raises(RuntimeError, match="manual reconciliation"):
        restarted.get_account_preflight()
    with pytest.raises(RuntimeError, match="manual reconciliation"):
        buy(restarted)
    assert len(api.submissions) == 1


def test_sell_quantity_rounds_down_to_nine_decimals(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    client.close_position(position, 1.1234567899)
    assert Decimal(api.submissions[-1]["qty"]) == Decimal("1.123456789")


def test_full_close_of_tiny_residual_uses_decimal_not_exponent_notation(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    client.close_position(position, 2.999999999)
    client.close_position(position)
    assert api.submissions[-1]["qty"] == "0.000000001"
    assert client.get_open_position_units([position]) == {position: 0}


def test_external_pending_sell_prevents_conflicting_close(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    api.orders["manual-sell"] = {
        "id": "external",
        "client_order_id": "manual-sell",
        "symbol": "AAPL",
        "side": "sell",
        "status": "new",
        "filled_qty": "0",
    }
    with pytest.raises(ClosePositionRejectedError, match="External pending"):
        client.close_position(position, 1)
    assert len(api.submissions) == 1


def test_transactional_reservation_rechecks_remaining_quantity(tmp_path):
    client, api = broker(tmp_path)
    position = buy(client).position_id
    client.close_position(position, 2)
    with pytest.raises(ValueError, match="exceeds remaining"):
        client.store.reserve(
            {"client_order_id": "stale-close", "side": "sell", "symbol": "AAPL", "qty": "2"},
            position_id=position,
        )
    assert len(api.submissions) == 2


def test_pending_order_collection_cannot_be_silently_truncated(tmp_path, monkeypatch):
    client, api = broker(tmp_path)
    request = api.request

    def truncated(method, path, **kwargs):
        return [{}] * 500 if path == "/v2/orders" else request(method, path, **kwargs)

    monkeypatch.setattr(api, "request", truncated)
    with pytest.raises(ValueError, match="truncated"):
        client.get_account_preflight()


@pytest.mark.parametrize("amount", [float("inf"), float("nan"), True, 0.5, 1e100])
def test_invalid_notional_never_reaches_broker(tmp_path, amount):
    client, api = broker(tmp_path)
    with pytest.raises(OpenPositionRejectedError):
        buy(client, amount)
    assert not api.submissions
