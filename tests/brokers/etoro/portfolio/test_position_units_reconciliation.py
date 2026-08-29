import pytest

from app.brokers.etoro.portfolio_position_parser import extract_open_position_units


def test_open_position_units_are_parsed_quantitatively():
    payload = {
        "clientPortfolio": {
            "positions": [
                {"positionID": "p1", "units": 1.25},
                {"positionID": "p2", "units": "0.75"},
            ]
        }
    }

    assert extract_open_position_units(payload, ("p1", "p2")) == {
        "p1": 1.25,
        "p2": 0.75,
    }


def test_absent_requested_position_is_zero_units():
    payload = {
        "clientPortfolio": {
            "positions": [{"positionID": "p1", "units": 1.25}]
        }
    }

    assert extract_open_position_units(payload, ("p1", "missing")) == {
        "p1": 1.25,
        "missing": 0.0,
    }


def test_open_etoro_position_without_units_fails_closed():
    payload = {
        "clientPortfolio": {
            "positions": [{"positionID": "p1", "isOpen": True}]
        }
    }

    with pytest.raises(
        ValueError,
        match="Unable to extract units for open eToro position p1",
    ):
        extract_open_position_units(payload, ("p1",))
