def build_close_order_payload(
    instrument_id: int,
    units_to_deduct: float | None = None,
) -> dict:
    if units_to_deduct is not None and units_to_deduct <= 0:
        raise ValueError("units_to_deduct must be positive when supplied")
    return {
        'InstrumentId': instrument_id,
        'UnitsToDeduct': (
            None if units_to_deduct is None else float(units_to_deduct)
        ),
    }
