import pytest

from app.brokers.etoro.order_response_parser import (
    extract_executed_position_details,
    extract_executed_position_details_list,
    extract_order_error_code,
    extract_order_error_message,
    extract_order_id,
    extract_position_id_from_order_details,
    extract_reference_id,
    has_executed_position_details,
    is_close_response_accepted,
    is_order_executed,
    is_order_rejected,
)


def executed_order_payload(
    avg_price: float = 238.0,
    units: float = 1.25,
) -> dict:
    return {
        'status': {
            'id': 1,
            'name': 'Executed',
            'errorCode': 0,
        },
        'positionExecutions': [
            {
                'positionId': 9001,
                'state': 'open',
                'openingData': {
                    'avgPrice': avg_price,
                    'units': units,
                },
            }
        ],
    }


def filled_order_payload(*, with_position_details: bool = True) -> dict:
    payload = {
        'status': {
            'id': 3,
            'name': 'Filled',
            'errorCode': 0,
        },
    }
    if with_position_details:
        payload['positionExecutions'] = [
            {
                'positionId': '123',
                'state': 'open',
                'openingData': {'avgPrice': 238.0, 'units': 0.75},
            }
        ]
    return payload


def test_extract_order_id_from_direct_order_response():
    assert extract_order_id({'orderId': 362406474, 'referenceId': 'ref-1'}) == '362406474'


def test_extract_order_id_from_close_order_response():
    assert extract_order_id({'orderForClose': {'positionID': 9001, 'instrumentID': 100000, 'orderID': 362447424}}) == '362447424'


def test_extract_order_id_raises_when_missing():
    with pytest.raises(ValueError, match='Unable to extract order id'):
        extract_order_id({'status': {'name': 'Executed'}})


def test_extract_reference_id_from_order_response():
    assert extract_reference_id({'orderId': 362406474, 'referenceId': 'ref-1'}) == 'ref-1'


def test_extract_reference_id_returns_none_when_missing():
    assert extract_reference_id({'orderId': 362406474}) is None


def test_extract_position_id_from_order_details():
    assert extract_position_id_from_order_details(executed_order_payload()) == '9001'


def test_extract_position_id_from_nested_order_details():
    assert extract_position_id_from_order_details({'data': {'order': {'PositionID': 123456}}}) == '123456'


def test_extract_position_id_returns_none_when_missing():
    assert extract_position_id_from_order_details({'status': {'id': 1, 'name': 'Executed'}, 'positionExecutions': []}) is None


def test_extract_executed_position_details_from_opening_data():
    details = extract_executed_position_details(
        executed_order_payload(avg_price=238.0, units=1.234567)
    )

    assert details is not None
    assert details.position_id == '9001'
    assert details.executed_entry_price == 238.0
    assert details.executed_units == pytest.approx(1.234567)


def test_extract_executed_position_details_returns_none_when_opening_data_is_missing():
    payload = {'positionExecutions': [{'positionId': 9001}]}

    assert extract_executed_position_details(payload) is None
    assert not has_executed_position_details(payload)


def test_extract_executed_position_details_returns_none_when_avg_price_is_missing():
    payload = {'positionExecutions': [{'positionId': 9001, 'openingData': {'units': 1.0}}]}

    assert extract_executed_position_details(payload) is None
    assert not has_executed_position_details(payload)


def test_extract_executed_position_details_returns_none_when_units_are_missing():
    payload = {'positionExecutions': [{'positionId': 9001, 'openingData': {'avgPrice': 238.0}}]}

    assert extract_executed_position_details(payload) is None
    assert not has_executed_position_details(payload)


def test_extract_executed_position_details_returns_none_when_avg_price_is_not_positive():
    payload = {'positionExecutions': [{'positionId': 9001, 'openingData': {'avgPrice': 0, 'units': 1.0}}]}

    assert extract_executed_position_details(payload) is None
    assert not has_executed_position_details(payload)


def test_extract_executed_position_details_returns_none_when_units_are_not_positive():
    payload = {'positionExecutions': [{'positionId': 9001, 'openingData': {'avgPrice': 238.0, 'units': 0}}]}

    assert extract_executed_position_details(payload) is None
    assert not has_executed_position_details(payload)


def test_extract_executed_position_details_does_not_parse_legacy_rate_fields():
    payload = {'positionExecutions': [{'positionId': 9001, 'openingData': {'rate': 238.0, 'units': 1.0}}]}

    assert extract_executed_position_details(payload) is None
    assert not has_executed_position_details(payload)


def test_extract_executed_position_details_list_returns_all_supported_position_executions():
    payload = {
        'positionExecutions': [
            {'positionId': 9001, 'openingData': {'avgPrice': 238.0, 'units': 1.0}},
            {'positionId': 9002, 'openingData': {'avgPrice': 239.0, 'units': 2.0}},
        ]
    }

    details = extract_executed_position_details_list(payload)

    assert [item.position_id for item in details] == ['9001', '9002']
    assert [item.executed_entry_price for item in details] == [238.0, 239.0]
    assert [item.executed_units for item in details] == [1.0, 2.0]
    assert extract_executed_position_details(payload) is None
    assert has_executed_position_details(payload)


def test_filled_order_with_position_execution_is_executed_not_rejected():
    payload = filled_order_payload(with_position_details=True)

    assert is_order_executed(payload) is True
    assert is_order_rejected(payload) is False
    assert has_executed_position_details(payload) is True
    assert extract_executed_position_details(payload).executed_entry_price == 238.0
    assert extract_executed_position_details(payload).executed_units == 0.75


def test_filled_order_without_position_details_is_executed_but_not_ready():
    payload = filled_order_payload(with_position_details=False)

    assert is_order_executed(payload) is True
    assert is_order_rejected(payload) is False
    assert has_executed_position_details(payload) is False


def test_order_is_executed_when_status_id_is_one():
    assert is_order_executed({'status': {'id': 1, 'name': 'Executed', 'errorCode': 0}})


def test_order_is_executed_when_status_name_is_executed_without_error():
    assert is_order_executed({'status': {'id': 1, 'name': 'Executed'}})


def test_order_is_not_executed_when_status_is_pending():
    assert not is_order_executed({'status': {'id': 0, 'name': 'Pending'}})


def test_order_is_not_executed_when_status_is_cancelled_or_rejected():
    assert not is_order_executed({'status': {'id': 2, 'name': 'Cancelled', 'errorCode': 0}})
    assert not is_order_executed({'status': {'id': 3, 'name': 'Rejected', 'errorCode': 0}})


def test_order_is_not_executed_when_error_code_is_non_zero():
    assert not is_order_executed({'status': {'id': 1, 'name': 'Executed', 'errorCode': 759}})
    assert not is_order_executed({'status': {'id': 3, 'name': 'Filled', 'errorCode': 759}})


def test_order_is_rejected_when_error_code_is_non_zero():
    assert is_order_rejected({'status': {'id': 1, 'errorCode': 759, 'errorMessage': 'Rejected by broker'}})


def test_order_is_rejected_when_status_name_is_rejected():
    assert is_order_rejected({'status': {'id': 3, 'name': 'Rejected', 'errorCode': 0}})


def test_order_is_rejected_when_status_is_cancelled():
    assert is_order_rejected({'status': {'id': 2, 'name': 'Cancelled', 'errorCode': 0}})


def test_order_is_rejected_when_status_is_failed():
    assert is_order_rejected({'status': {'id': 4, 'name': 'Failed', 'errorCode': 0}})


def test_extract_order_error_code_from_top_level_payload():
    assert extract_order_error_code({'errorCode': '759'}) == 759


def test_extract_order_error_code_from_status_payload():
    assert extract_order_error_code({'status': {'errorCode': '759'}}) == 759


def test_extract_order_error_message_from_top_level_payload():
    assert extract_order_error_message({'errorMessage': 'Rejected by broker'}) == 'Rejected by broker'


def test_extract_order_error_message_from_status_payload():
    assert extract_order_error_message({'status': {'errorMessage': 'Rejected by broker'}}) == 'Rejected by broker'


def test_is_close_response_accepted_when_position_matches_and_status_is_one():
    assert is_close_response_accepted({'orderForClose': {'positionID': 3549893989, 'orderID': 362453867, 'statusID': 1}}, '3549893989')


def test_is_close_response_accepted_with_camel_case_position_and_status_id():
    assert is_close_response_accepted({'orderForClose': {'PositionId': '3549893989', 'orderID': 362453867, 'statusId': 1}}, 3549893989)


def test_is_close_response_not_accepted_when_order_for_close_is_missing():
    assert not is_close_response_accepted({}, '3549893989')


def test_is_close_response_not_accepted_when_position_does_not_match():
    assert not is_close_response_accepted({'orderForClose': {'positionID': 3549893989, 'orderID': 362453867, 'statusID': 1}}, 'another-position')


def test_is_close_response_not_accepted_when_status_is_not_one():
    assert not is_close_response_accepted({'orderForClose': {'positionID': 3549893989, 'orderID': 362453867, 'statusID': 0}}, '3549893989')
