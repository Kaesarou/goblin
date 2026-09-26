from app.market.models import MarketSnapshot, PriceSource


def to_market_snapshot(
    *,
    symbol: str,
    rates_payload: dict,
    symbol_by_instrument_id: dict[int, str],
) -> MarketSnapshot:
    return to_market_snapshots(
        rates_payload=rates_payload,
        symbol_by_instrument_id=symbol_by_instrument_id,
    )[symbol]


def to_market_snapshots(
    *,
    rates_payload: dict,
    symbol_by_instrument_id: dict[int, str],
) -> dict[str, MarketSnapshot]:
    result: dict[str, MarketSnapshot] = {}
    rates = rates_payload['rates']

    for rate in rates:
        instrument_id = _required_int(rate, 'instrumentID')
        symbol = symbol_by_instrument_id.get(instrument_id)
        if symbol is None:
            raise ValueError(f'Unable to find cached symbol by instrument_id={instrument_id}.')
        bid = _required_float(rate, 'Bid')
        ask = _required_float(rate, 'Ask')

        last = _optional_float(rate.get('Last'))
        price_source = PriceSource.BROKER_LAST
        if last is None:
            last = (bid + ask) / 2
            price_source = PriceSource.BID_ASK_MIDPOINT

        result[symbol] = MarketSnapshot.now(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            price_source=price_source,
        )

    return result


def _required_float(payload: dict, field: str) -> float:
    value = payload.get(field)
    if value is None:
        raise ValueError(
            f'Unable to extract required float field={field}. Payload={payload}'
        )
    return float(value)


def _required_int(payload: dict, field: str) -> int:
    value = payload.get(field)
    if value is None:
        raise ValueError(
            f'Unable to extract required int field={field}. Payload={payload}'
        )
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
