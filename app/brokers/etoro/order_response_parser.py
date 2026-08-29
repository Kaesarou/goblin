from dataclasses import dataclass

from app.brokers.etoro.payload_collections import keep_dict_items
from app.brokers.etoro.scalar_extractors import extract_optional_float, extract_optional_int
from app.brokers.etoro.string_extractors import extract_optional_string


ORDER_ID_KEYS = ('orderId', 'OrderId', 'orderID', 'OrderID')
ORDER_ID_NESTED_KEYS = ('orderForClose', 'OrderForClose', 'data', 'Data', 'order', 'Order')
REFERENCE_ID_KEYS = ('referenceId', 'ReferenceId', 'referenceID', 'ReferenceID')
ORDER_ERROR_CODE_KEYS = ('errorCode',)
POSITION_ID_KEYS = ('positionId', 'PositionId', 'positionID', 'PositionID')
POSITION_EXECUTION_KEYS = ('positionExecutions', 'positions')
POSITION_NESTED_KEYS = ('position', 'Position', 'data', 'Data', 'order', 'Order')
CLOSE_STATUS_ID_KEYS = ('statusID', 'statusId')
AVG_PRICE_KEYS = ('avgPrice',)
OPENING_UNITS_KEYS = ('units', 'Units')
INVESTED_AMOUNT_ACCOUNT_KEYS = (
    'investedAmountCurrency',
    'InvestedAmountCurrency',
)
EXECUTED_STATUS_NAMES = ('executed', 'filled')
REJECTED_STATUS_NAMES = ('rejected', 'failed', 'cancelled', 'canceled', 'error')


@dataclass(frozen=True)
class ExecutedPositionDetails:
    position_id: str
    executed_entry_price: float
    executed_units: float
    executed_notional: float | None = None


def extract_order_id(payload: dict) -> str:
    order_id = extract_optional_string(payload, ORDER_ID_KEYS)
    if order_id is not None:
        return order_id

    for key in ORDER_ID_NESTED_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            try:
                return extract_order_id(value)
            except ValueError:
                pass

    raise ValueError(f'Unable to extract order id from eToro response: {payload}')


def extract_reference_id(payload: dict) -> str | None:
    return extract_optional_string(payload, REFERENCE_ID_KEYS)


def extract_position_id(payload: dict) -> str | None:
    return extract_optional_string(payload, POSITION_ID_KEYS)


def extract_position_id_from_order_details(payload: dict) -> str | None:
    direct_position_id = extract_position_id(payload)
    if direct_position_id is not None:
        return direct_position_id

    for key in POSITION_EXECUTION_KEYS:
        positions = payload.get(key)
        if not isinstance(positions, list):
            continue
        for position in keep_dict_items(positions):
            position_id = extract_position_id(position)
            if position_id is not None:
                return position_id

    for key in POSITION_NESTED_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            position_id = extract_position_id_from_order_details(value)
            if position_id is not None:
                return position_id

    return None


def extract_executed_position_details(payload: dict) -> ExecutedPositionDetails | None:
    executions = extract_executed_position_details_list(payload)
    if len(executions) != 1:
        return None
    return executions[0]


def extract_executed_position_details_list(payload: dict) -> list[ExecutedPositionDetails]:
    position_executions = payload.get('positionExecutions')
    if not isinstance(position_executions, list):
        return []

    executed_positions: list[ExecutedPositionDetails] = []
    for execution in keep_dict_items(position_executions):
        position_id = extract_position_id(execution)
        opening_data = execution.get('openingData')
        if position_id is None or not isinstance(opening_data, dict):
            continue
        avg_price = extract_optional_float(opening_data, AVG_PRICE_KEYS)
        units = extract_optional_float(opening_data, OPENING_UNITS_KEYS)
        invested_amount = extract_optional_float(
            execution,
            INVESTED_AMOUNT_ACCOUNT_KEYS,
        )
        # V3 opens by cash amount. eToro is authoritative for the resulting units;
        # deriving units locally from amount / avgPrice is unsafe for FX-converted
        # equities and broker rounding. The account-currency invested amount is
        # retained separately when eToro provides it so risk exposure never becomes
        # an asset-currency value merely because quantity accounting is corrected.
        if (
            avg_price is None
            or avg_price <= 0
            or units is None
            or units <= 0
        ):
            continue
        executed_positions.append(
            ExecutedPositionDetails(
                position_id=position_id,
                executed_entry_price=avg_price,
                executed_units=units,
                executed_notional=(
                    invested_amount
                    if invested_amount is not None and invested_amount > 0
                    else None
                ),
            )
        )
    return executed_positions


def has_executed_position_details(payload: dict) -> bool:
    return bool(extract_executed_position_details_list(payload))


def extract_order_error_code(payload: dict) -> int | None:
    error_code = extract_optional_int(payload, ORDER_ERROR_CODE_KEYS)
    if error_code is not None:
        return error_code
    status = payload.get('status')
    if isinstance(status, dict):
        return extract_optional_int(status, ORDER_ERROR_CODE_KEYS)
    return None


def extract_order_error_message(payload: dict) -> str | None:
    error_message = payload.get('errorMessage')
    if error_message:
        return str(error_message)
    status = payload.get('status')
    if isinstance(status, dict):
        status_error_message = status.get('errorMessage')
        if status_error_message:
            return str(status_error_message)
    return None


def is_order_executed(payload: dict) -> bool:
    status = payload.get('status')
    if not isinstance(status, dict):
        return False

    status_error_code = status.get('errorCode')
    if status_error_code not in (None, 0):
        return False

    status_name = str(status.get('name', '')).lower()
    if status_name in EXECUTED_STATUS_NAMES:
        return True

    status_id = status.get('id')
    if status_id == 1:
        return True

    return status_id == 3 and has_executed_position_details(payload)


def is_order_rejected(payload: dict) -> bool:
    error_code = extract_order_error_code(payload)
    if error_code not in (None, 0):
        return True

    status = payload.get('status')
    if not isinstance(status, dict):
        return False

    status_error_code = status.get('errorCode')
    if status_error_code not in (None, 0):
        return True

    status_name = str(status.get('name', '')).lower()
    return status_name in REJECTED_STATUS_NAMES


def is_close_response_accepted(payload: dict, position_id: str) -> bool:
    order_for_close = payload.get('orderForClose')
    if not isinstance(order_for_close, dict):
        return False
    response_position_id = extract_position_id(order_for_close)
    if str(response_position_id) != str(position_id):
        return False
    status_id = extract_optional_int(order_for_close, CLOSE_STATUS_ID_KEYS)
    return status_id == 1
