"""A broker terminal status, never an exception string, releases an open."""

import pytest
import requests

from app.brokers.etoro.order_confirmation_error import (
    EtoroOrderConfirmationUnknownError,
    EtoroOrderRejectedError,
)
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings


def _client():
    return ResilientEtoroClient(settings=Settings.model_construct(
        broker="etoro_demo", base_currency="USD",
        etoro_api_key="api", etoro_user_key="user",
    ))


def test_broker_terminal_status_produces_typed_rejection(monkeypatch):
    client = _client()
    details = {"status": {"name": "Rejected", "errorCode": 42,
                          "errorMessage": "insufficient funds"}}
    monkeypatch.setattr(client, "get_order_details", lambda order_id: details)

    with pytest.raises(EtoroOrderRejectedError) as caught:
        client._wait_for_executed_order("order-1", attempts=1, delay_seconds=0)

    assert caught.value.order_id == "order-1"
    assert caught.value.error_code == 42
    assert caught.value.details == details


def test_failed_lookup_never_proves_rejection(monkeypatch):
    client = _client()

    def timeout(_order_id):
        raise requests.Timeout("eToro order rejected: misleading upstream text")

    monkeypatch.setattr(client, "get_order_details", timeout)
    with pytest.raises(RuntimeError, match="not executed with required details") as caught:
        client._wait_for_executed_order("order-2", attempts=1, delay_seconds=0)
    assert not isinstance(caught.value, EtoroOrderRejectedError)


def test_resilient_open_ignores_untyped_rejection_message(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_find_instrument_id", lambda symbol: 100)
    monkeypatch.setattr(client, "_build_open_order_payload", lambda **kwargs: {})
    monkeypatch.setattr(client, "_post", lambda path, payload: {
        "orderId": "order-3", "referenceId": "reference-3",
    })

    def deceptive_error(*args, **kwargs):
        raise RuntimeError("eToro order rejected: this is not a broker status")

    monkeypatch.setattr(client, "_wait_for_executed_order", deceptive_error)
    with pytest.raises(EtoroOrderConfirmationUnknownError) as caught:
        client.open_position("INTC", "BUY", 300.0, 99.0, 300.0)
    assert caught.value.order_id == "order-3"
    assert caught.value.reference_id == "reference-3"
