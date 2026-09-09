ETORO_CLOSE_UNIT_DECIMAL_PLACES = 6
ETORO_CLOSE_UNIT_QUANTUM = 10 ** (-ETORO_CLOSE_UNIT_DECIMAL_PLACES)


def normalize_close_units(units_to_deduct: float) -> float:
    units = float(units_to_deduct)
    if units <= 0:
        raise ValueError("units_to_deduct must be positive when supplied")
    normalized = round(units, ETORO_CLOSE_UNIT_DECIMAL_PLACES)
    if normalized <= 0:
        raise ValueError("units_to_deduct rounds to zero at eToro unit precision")
    return normalized


def build_close_order_payload(
    instrument_id: int,
    units_to_deduct: float | None = None,
) -> dict:
    return {
        'InstrumentId': instrument_id,
        'UnitsToDeduct': (
            None
            if units_to_deduct is None
            else normalize_close_units(units_to_deduct)
        ),
    }
