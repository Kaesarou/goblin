from datetime import UTC, datetime

from app.brokers.etoro.close_order_details_parser import (
    extract_close_execution,
)


def test_extract_close_execution_uses_matching_broker_position_fill():
    payload = {
        "positions": [
            {"positionID": "other", "rate": 900.0},
            {
                "positionID": "position-1",
                "occurred": "2026-07-31T19:12:34.123456+02:00",
                "rate": "99.42",
                "units": "10.5",
                "conversionRate": "0.92",
                "amount": "1043.91",
            },
        ]
    }

    execution = extract_close_execution(
        payload,
        close_order_id="close-1",
        position_id="position-1",
    )

    assert execution is not None
    assert execution.position_id == "position-1"
    assert execution.close_order_id == "close-1"
    assert execution.executed_exit_price == 99.42
    assert execution.executed_at == datetime(
        2026,
        7,
        31,
        17,
        12,
        34,
        123456,
        tzinfo=UTC,
    )
    assert execution.units == 10.5
    assert execution.conversion_rate == 0.92
    assert execution.amount == 1043.91
    assert execution.broker_response is payload


def test_extract_close_execution_does_not_fabricate_missing_or_invalid_fill():
    assert (
        extract_close_execution(
            {"status": "accepted"},
            close_order_id="close-1",
            position_id="position-1",
        )
        is None
    )
    assert (
        extract_close_execution(
            {"positions": [{"positionID": "position-1", "rate": None}]},
            close_order_id="close-1",
            position_id="position-1",
        )
        is None
    )
    assert (
        extract_close_execution(
            {"positions": [{"positionID": "other", "rate": 99.42}]},
            close_order_id="close-1",
            position_id="position-1",
        )
        is None
    )
