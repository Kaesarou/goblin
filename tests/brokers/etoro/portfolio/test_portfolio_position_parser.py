from app.brokers.etoro.portfolio_position_parser import (
    contains_open_position,
    extract_open_positions,
)


def test_extract_open_positions_from_client_portfolio():
    payload = {
        'clientPortfolio': {
            'positions': [
                {
                    'positionID': 3549893989,
                    'instrumentID': 1001,
                },
                'ignored-non-dict',
            ]
        }
    }

    assert extract_open_positions(payload) == [
        {
            'positionID': 3549893989,
            'instrumentID': 1001,
        }
    ]


def test_extract_open_positions_ignores_non_dict_positions():
    payload = {
        'clientPortfolio': {'positions': [
            'ignored',
            123,
            {
                'positionID': 3549893989,
                'instrumentID': 1001,
            },
        ]}
    }

    assert extract_open_positions(payload) == [
        {
            'positionID': 3549893989,
            'instrumentID': 1001,
        }
    ]


def test_extract_open_positions_returns_empty_list_when_positions_is_not_a_list():
    assert extract_open_positions(
        {'clientPortfolio': {'positions': {'positionID': 3549893989}}}
    ) == []


def test_extract_open_positions_returns_empty_list_when_missing():
    assert extract_open_positions({'clientPortfolio': {'orders': []}}) == []


def test_contains_open_position_when_position_exists_in_client_portfolio():
    assert contains_open_position(
        {
            'clientPortfolio': {
                'positions': [
                    {
                        'positionID': 3549893989,
                        'instrumentID': 1001,
                    }
                ]
            }
        },
        '3549893989',
    )


def test_contains_open_position_rejects_noncanonical_position_id_field():
    assert not contains_open_position(
        {
            'clientPortfolio': {
                'positions': [
                    {
                        'PositionId': 3549893989,
                        'instrumentID': 1001,
                    }
                ]
            }
        },
        '3549893989',
    )


def test_contains_open_position_accepts_int_position_id_argument():
    assert contains_open_position(
        {
            'clientPortfolio': {
                'positions': [
                    {
                        'positionID': '3549893989',
                        'instrumentID': 1001,
                    }
                ]
            }
        },
        3549893989,
    )


def test_contains_open_position_returns_false_when_position_is_missing():
    assert not contains_open_position(
        {
            'clientPortfolio': {
                'positions': [
                    {
                        'positionID': 111,
                        'instrumentID': 1001,
                    }
                ]
            }
        },
        '3549893989',
    )


def test_contains_open_position_returns_false_when_position_is_explicitly_closed():
    assert not contains_open_position(
        {
            'clientPortfolio': {
                'positions': [
                    {
                        'positionID': 3549893989,
                        'instrumentID': 1001,
                        'isOpen': False,
                    }
                ]
            }
        },
        '3549893989',
    )


def test_contains_open_position_treats_missing_is_open_as_open():
    assert contains_open_position(
        {
            'clientPortfolio': {
                'positions': [
                    {
                        'positionID': 3549893989,
                        'instrumentID': 1001,
                    }
                ]
            }
        },
        '3549893989',
    )
