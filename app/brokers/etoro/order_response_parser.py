from dataclasses import dataclass

EXECUTED_STATUS_NAMES = ('executed', 'filled')
REJECTED_STATUS_NAMES = ('rejected', 'failed', 'cancelled', 'canceled', 'error')


@dataclass(frozen=True)
class ExecutedPositionDetails:
    position_id: str
    executed_entry_price: float
    executed_units: float
    executed_notional: float | None = None


def extract_order_id(payload: dict) -> str:
    # Open submission responses expose orderId; close submission responses
    # expose orderForClose.orderID. These are separate documented contracts,
    # not interchangeable case/shape guesses.
    order_id = payload.get('orderId')
    if order_id is not None:
        return str(order_id)
    order_for_close = payload.get('orderForClose')
    if isinstance(order_for_close, dict):
        order_id = order_for_close.get('orderID')
        if order_id is not None:
            return str(order_id)

    raise ValueError(f'Unable to extract order id from eToro response: {payload}')


def extract_reference_id(payload: dict) -> str | None:
    reference_id = payload.get('referenceId')
    return None if reference_id is None else str(reference_id)


def extract_position_id(payload: dict) -> str | None:
    position_id = payload.get('positionId')
    return None if position_id is None else str(position_id)


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
    for execution in position_executions:
        if not isinstance(execution, dict):
            continue
        position_id = extract_position_id(execution)
        opening_data = execution.get('openingData')
        if position_id is None or not isinstance(opening_data, dict):
            continue
        avg_price = _optional_float(opening_data.get('avgPrice'))
        units = _optional_float(opening_data.get('units'))
        invested_amount = _optional_float(execution.get('investedAmountCurrency'))
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
    error_code = _optional_int(payload.get('errorCode'))
    if error_code is not None:
        return error_code
    status = payload.get('status')
    if isinstance(status, dict):
        return _optional_int(status.get('errorCode'))
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

    # eToro close-order lookup uses a top-level statusID. Prospectively observed
    # terminal failures return statusID=4 with positions=[]; treating those as
    # merely "execution unavailable" leaves the broker leg mutation locked
    # forever. Status 4 is therefore terminal even when errorCode is absent/zero.
    top_level_status_id = _optional_int(payload.get('statusID'))
    if top_level_status_id == 4:
        return True

    status = payload.get('status')
    if not isinstance(status, dict):
        return False

    status_error_code = status.get('errorCode')
    if status_error_code not in (None, 0):
        return True

    status_name = str(status.get('name', '')).lower()
    if status_name in REJECTED_STATUS_NAMES:
        return True
    return status.get('id') == 4


def is_close_response_accepted(payload: dict, position_id: str) -> bool:
    order_for_close = payload.get('orderForClose')
    if not isinstance(order_for_close, dict):
        return False
    response_position_id = order_for_close.get('positionID')
    if str(response_position_id) != str(position_id):
        return False
    status_id = _optional_int(order_for_close.get('statusID'))
    return status_id == 1


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
