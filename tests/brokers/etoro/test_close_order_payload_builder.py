import pytest

from app.brokers.etoro.close_order_payload_builder import (
    ETORO_CLOSE_UNIT_DECIMAL_PLACES,
    build_close_order_payload,
    normalize_close_units,
)


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


def test_partial_close_units_are_normalized_to_broker_precision():
    assert ETORO_CLOSE_UNIT_DECIMAL_PLACES == 6
    assert normalize_close_units(0.06338807999999999) == pytest.approx(0.063388)
    assert build_close_order_payload(
        100000,
        units_to_deduct=0.06338807999999999,
    )['UnitsToDeduct'] == pytest.approx(0.063388)


@pytest.mark.parametrize('units', [0.0, -0.1, 0.0000001])
def test_build_partial_close_order_payload_rejects_non_positive_or_zero_after_rounding(units):
    with pytest.raises(ValueError, match='units_to_deduct'):
        build_close_order_payload(100000, units_to_deduct=units)
