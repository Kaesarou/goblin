import pytest

from app.brokers.etoro.instrument_search_parser import (
    candidate_summaries,
    extract_instrument_id,
    extract_items,
    resolve_exact_instrument_id,
)


def test_extract_items_from_documented_items_list_and_ignores_non_dict_values():
    payload = {
        'items': [
            {'internalSymbolFull': 'BTC'},
            'ignored',
            {'internalSymbolFull': 'ETH'},
        ]
    }

    assert extract_items(payload) == [
        {'internalSymbolFull': 'BTC'},
        {'internalSymbolFull': 'ETH'},
    ]


def test_extract_items_returns_empty_list_when_missing():
    assert extract_items({'unexpected': []}) == []


def test_extract_instrument_id_uses_documented_field():
    assert extract_instrument_id({'internalInstrumentId': '100000'}) == 100000


def test_extract_instrument_id_ignores_legacy_field_names():
    assert extract_instrument_id({'instrumentId': 100001}) is None


def test_extract_instrument_id_returns_none_when_missing():
    assert extract_instrument_id({'internalSymbolFull': 'BTC'}) is None


def test_resolve_exact_instrument_id_uses_exact_symbol_match():
    payload = {
        'items': [
            {
                'internalSymbolFull': 'BTCA',
                'internalInstrumentDisplayName': 'Bitcoin / VAULTA',
                'internalInstrumentId': 100134,
            },
            {
                'internalSymbolFull': 'BTC',
                'internalInstrumentDisplayName': 'Bitcoin',
                'internalInstrumentId': 100000,
            },
        ]
    }

    assert resolve_exact_instrument_id('BTC', payload) == 100000


def test_resolve_exact_instrument_id_is_case_insensitive():
    payload = {
        'items': [
            {
                'internalSymbolFull': 'btc',
                'internalInstrumentDisplayName': 'Bitcoin',
                'internalInstrumentId': 100000,
            },
        ]
    }

    assert resolve_exact_instrument_id('BTC', payload) == 100000


def test_resolve_exact_instrument_id_raises_when_no_exact_match():
    payload = {
        'items': [
            {
                'internalSymbolFull': 'BTCA',
                'internalInstrumentDisplayName': 'Bitcoin / VAULTA',
                'internalInstrumentId': 100134,
                'currentRate': 1.23,
            },
        ]
    }

    with pytest.raises(ValueError, match='No exact eToro instrument match found') as exc_info:
        resolve_exact_instrument_id('BTC', payload)

    assert 'BTCA' in str(exc_info.value)
    assert 'Bitcoin / VAULTA' in str(exc_info.value)


def test_resolve_exact_instrument_id_raises_when_match_has_no_instrument_id():
    payload = {
        'items': [
            {
                'internalSymbolFull': 'BTC',
                'internalInstrumentDisplayName': 'Bitcoin',
            },
        ]
    }

    with pytest.raises(ValueError, match='Unable to find instrument id'):
        resolve_exact_instrument_id('BTC', payload)


def test_candidate_summaries_keeps_first_ten_items():
    items = [
        {
            'internalSymbolFull': f'SYM{index}',
            'internalInstrumentDisplayName': f'Instrument {index}',
            'internalInstrumentId': index,
            'currentRate': index + 0.5,
        }
        for index in range(12)
    ]

    summaries = candidate_summaries(items)

    assert len(summaries) == 10
    assert summaries[0] == {
        'internalSymbolFull': 'SYM0',
        'displayName': 'Instrument 0',
        'instrumentId': 0,
        'currentRate': 0.5,
    }
    assert summaries[-1]['internalSymbolFull'] == 'SYM9'
