import pytest

from app.brokers.etoro.order_response_parser import extract_executed_position_details


def test_open_execution_preserves_broker_units_and_account_currency_notional():
    payload = {
        "positionExecutions": [
            {
                "positionId": "p1",
                "investedAmountCurrency": 399.25,
                "openingData": {
                    "avgPrice": 200.0,
                    "units": 1.2345,
                },
            }
        ]
    }

    details = extract_executed_position_details(payload)

    assert details is not None
    assert details.position_id == "p1"
    assert details.executed_entry_price == pytest.approx(200.0)
    assert details.executed_units == pytest.approx(1.2345)
    assert details.executed_notional == pytest.approx(399.25)
    assert details.executed_notional != pytest.approx(
        details.executed_units * details.executed_entry_price
    )


def test_open_execution_allows_missing_account_notional_without_inventing_it():
    payload = {
        "positionExecutions": [
            {
                "positionId": "p1",
                "openingData": {
                    "avgPrice": 200.0,
                    "units": 1.2345,
                },
            }
        ]
    }

    details = extract_executed_position_details(payload)

    assert details is not None
    assert details.executed_notional is None
