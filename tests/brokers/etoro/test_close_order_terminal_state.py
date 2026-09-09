import pytest

from app.brokers.base import ClosePositionRejectedError
from app.brokers.etoro.etoro_client import EtoroClient
from app.brokers.etoro.order_response_parser import is_order_rejected


REJECTED_776 = {
    "orderID": 380196349,
    "statusID": 4,
    "errorCode": 776,
    "errorMessage": (
        "Calculated post deduction remaining equity: 6.18 USD, is under the "
        "allowed MinPoisitionAmount :9.90 USD"
    ),
    "positions": [],
}


def test_top_level_status_four_is_terminal_close_rejection():
    assert is_order_rejected(REJECTED_776)
    assert is_order_rejected({"statusID": 4, "errorCode": 0, "positions": []})


def test_get_close_execution_raises_terminal_rejection_instead_of_returning_none():
    client = object.__new__(EtoroClient)
    client.env = "demo"
    client._order_lookup_governor = lambda: object()
    client._get_once = lambda path, governor=None: dict(REJECTED_776)

    with pytest.raises(ClosePositionRejectedError) as exc_info:
        client.get_close_execution("380196349", "3587993893")

    error = exc_info.value
    assert error.position_id == "3587993893"
    assert error.broker_response["statusID"] == 4
    assert error.broker_response["errorCode"] == 776
