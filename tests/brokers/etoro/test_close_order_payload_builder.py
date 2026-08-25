import pytest

from app.brokers.etoro.close_order_payload_builder import build_close_order_payload


def test_build_close_order_payload():
    assert build_close_order_payload(100000) == {
        'InstrumentId': 100000,
        'UnitsToDeduct': None,
    }


def test_build_partial_close_order_payload_uses_units_to_deduct():
    assert build_close_order_payload(100000, units_to_deduct=0.84) == {
        'InstrumentId': 100000,
        'UnitsToDeduct': 0.84,
    }


@pytest.mark.parametrize('units', [0.0, -0.1])
def test_build_partial_close_order_payload_rejects_non_positive_units(units):
    with pytest.raises(ValueError, match='units_to_deduct must be positive'):
        build_close_order_payload(100000, units_to_deduct=units)
