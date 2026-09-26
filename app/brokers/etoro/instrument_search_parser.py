from app.brokers.etoro.payload_collections import keep_dict_items

INSTRUMENT_DISPLAY_NAME_KEY = 'internalInstrumentDisplayName'
INSTRUMENT_CURRENT_RATE_KEY = 'currentRate'


def normalize_symbol(symbol: str) -> str:
    return symbol.upper()


def extract_items(payload: dict) -> list[dict]:
    items = payload.get('items')
    return keep_dict_items(items) if isinstance(items, list) else []


def extract_instrument_symbol(instrument: dict) -> str | None:
    symbol = instrument.get('internalSymbolFull')
    return None if symbol is None else str(symbol)


def resolve_exact_instrument_id(symbol: str, payload: dict) -> int:
    normalized_symbol = normalize_symbol(symbol)
    items = extract_items(payload)

    exact_matches = [
        item for item in items
        if normalize_symbol(extract_instrument_symbol(item) or '') == normalized_symbol
    ]

    if not exact_matches:
        candidates = candidate_summaries(items)

        raise ValueError(
            f'No exact eToro instrument match found for symbol={symbol}. '
            f'Candidates={candidates}'
        )

    instrument = exact_matches[0]
    instrument_id = extract_instrument_id(instrument)

    if instrument_id is None:
        raise ValueError(
            f'Unable to find instrument id for symbol={symbol}. Instrument={instrument}'
        )

    return int(instrument_id)


def extract_instrument_id(instrument: dict) -> int | None:
    instrument_id = instrument.get('internalInstrumentId')
    return None if instrument_id is None else int(instrument_id)


def candidate_summaries(items: list[dict]) -> list[dict]:
    return [
        {
            'internalSymbolFull': extract_instrument_symbol(item),
            'displayName': item.get(INSTRUMENT_DISPLAY_NAME_KEY),
            'instrumentId': extract_instrument_id(item),
            'currentRate': item.get(INSTRUMENT_CURRENT_RATE_KEY),
        }
        for item in items[:10]
    ]
