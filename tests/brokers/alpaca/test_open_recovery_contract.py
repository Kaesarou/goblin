from decimal import Decimal

import pytest
import requests

from app.brokers.base import BrokerOpenOrder, OpenPositionRejectedError
from app.brokers.cached_broker import CachedBrokerClient
from tests.brokers.alpaca.test_execution import broker, buy


def test_preassigned_buy_identity_and_partial_execution_survive_restart(tmp_path):
    client, api = broker(tmp_path)
    order_id = client.prepare_open_order_id("action")
    assert not api.submissions
    api.next_status, api.next_qty = "partially_filled", Decimal("0.5")
    api.timeout_after_post = True
    with pytest.raises(requests.Timeout):
        client.open_position("AAPL", "BUY", 300, 50, 1000, client_order_id=order_id)
    restarted, _ = broker(tmp_path, api)
    cached = CachedBrokerClient(CachedBrokerClient(restarted))
    assert cached.get_open_execution(order_id, "AAPL", 300) is None
    preflight = cached.get_account_preflight()
    assert preflight.pending_open_orders == (order_id,)
    assert preflight.open_orders[order_id] == BrokerOpenOrder(
        order_id, order_id, "AAPL", 300, "partially_filled",
    )
    api.fill(order_id, "1.2", "canceled")
    result = cached.get_open_execution(order_id, "AAPL", 300)
    assert result.executed_units == 1.2
    assert result.executed_notional == 120
    assert result.order == BrokerOpenOrder(order_id, order_id, "AAPL", 300, "canceled")
    assert cached.get_open_execution(order_id, "AAPL", 300) == result
    assert len(api.submissions) == 1


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_only_terminal_zero_fill_releases_a_pending_buy(tmp_path, status):
    client, api = broker(tmp_path)
    order_id = client.prepare_open_order_id("action")
    api.next_status = "new"
    with pytest.raises(TimeoutError):
        client.open_position("AAPL", "BUY", 300, 50, 1000, client_order_id=order_id)
    assert client.get_open_execution(order_id, "AAPL", 300) is None
    api.fill(order_id, 0, status)
    with pytest.raises(OpenPositionRejectedError, match="terminal unfilled"):
        client.get_open_execution(order_id, "AAPL", 300)
    assert len(api.submissions) == 1


def test_missing_order_after_timeout_is_not_a_terminal_buy_rejection(tmp_path):
    client, api = broker(tmp_path)
    order_id = client.prepare_open_order_id("action")
    api.next_status, api.timeout_after_post = "new", True
    with pytest.raises(requests.Timeout):
        client.open_position("AAPL", "BUY", 300, 50, 1000, client_order_id=order_id)
    api.orders.pop(order_id)
    restarted, _ = broker(tmp_path, api)
    assert restarted.get_open_execution(order_id, "AAPL", 300) is None
    assert restarted.get_account_preflight().open_orders[order_id].status is None
    assert len(api.submissions) == 1


@pytest.mark.parametrize("symbol,amount", [("MSFT", 300), ("AAPL", 200), ("AAPL", 300.01)])
def test_lookup_requires_the_exact_buy_request(tmp_path, symbol, amount):
    client, api = broker(tmp_path)
    result = buy(client)
    with pytest.raises(ValueError, match="does not match"):
        client.get_open_execution(result.position_id, symbol, amount)
    assert len(api.submissions) == 1


def test_lookup_cannot_treat_a_sell_identity_as_a_buy(tmp_path):
    client, api = broker(tmp_path)
    result = buy(client)
    close = client.close_position(result.position_id, 1)
    with pytest.raises(ValueError, match="does not match"):
        client.get_open_execution(close.close_order_id, "AAPL", 300)
    assert len(api.submissions) == 2


def test_lookup_keeps_the_original_cent_rounded_request(tmp_path):
    client, api = broker(tmp_path)
    result = client.open_position("AAPL", "BUY", 300.129, 50, 1000)
    assert result.order.requested_notional == 300.12
    assert client.get_open_execution(result.position_id, "AAPL", 300.129) == result
    assert api.submissions[0]["notional"] == "300.12"
