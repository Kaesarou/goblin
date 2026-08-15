import json
from datetime import datetime, timezone
from uuid import uuid4

from app.market.models import MarketSnapshot, PriceSource, TimestampSource
from app.market_data.models import MarketDataEvent, MarketDataSource
from app.research.payload_schema_observer import build_payload_schema_sample


def build_authentication_request(
    *, api_key: str, user_key: str, request_id: str | None = None
) -> dict:
    return {
        'id': request_id or str(uuid4()),
        'operation': 'Authenticate',
        'data': {'userKey': user_key, 'apiKey': api_key},
    }


def build_subscription_request(
    instrument_ids: list[int],
    *,
    request_id: str | None = None,
) -> dict:
    return {
        'id': request_id or str(uuid4()),
        'operation': 'Subscribe',
        'data': {
            'topics': [
                f'instrument:{instrument_id}'
                for instrument_id in instrument_ids
            ],
            'snapshot': True,
        },
    }


def validate_authentication_response(
    raw_message: str,
    *,
    request_id: str,
) -> None:
    payload = parse_json_frame(raw_message)
    if not isinstance(payload, dict) or payload.get('id') != request_id:
        raise ValueError('Unexpected WebSocket authentication response.')
    if (
        payload.get('operation') != 'Authenticate'
        or payload.get('success') is not True
    ):
        raise RuntimeError(
            'eToro WebSocket authentication failed: '
            f"code={payload.get('errorCode')}, "
            f"message={payload.get('errorMessage')}"
        )


def parse_websocket_events(
    raw_message: str,
    *,
    symbol_by_instrument_id: dict[int, str],
    received_at: datetime,
    connection_id: str,
    rate_state_by_instrument_id: dict[int, dict],
) -> list[MarketDataEvent]:
    payload = parse_json_frame(raw_message)
    if not isinstance(payload, dict):
        return []
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return []

    result: list[MarketDataEvent] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        instrument_id = _instrument_id(message.get('topic'))
        if instrument_id is None:
            continue
        symbol = symbol_by_instrument_id.get(instrument_id)
        if symbol is None:
            continue
        patch = _content_payload(message.get('content'))
        is_snapshot = str(message.get('type') or '').lower() == 'snapshot'
        previous = (
            {}
            if is_snapshot
            else rate_state_by_instrument_id.get(instrument_id, {})
        )
        merged = {**previous, **patch}
        rate_state_by_instrument_id[instrument_id] = merged

        previous_prices = _prices(previous)
        current_prices = _prices(merged)
        source_timestamp = _first_datetime(
            merged,
            'Date',
            'date',
            'lastUpdate',
            'LastUpdate',
        )
        snapshot = _snapshot(
            symbol=symbol,
            payload=merged,
            source_timestamp=source_timestamp,
            received_at=received_at,
        )
        result.append(
            MarketDataEvent(
                symbol=symbol,
                source=MarketDataSource.WEBSOCKET,
                received_at=received_at,
                snapshot=snapshot,
                instrument_id=instrument_id,
                message_id=_optional_string(message.get('id')),
                price_rate_id=_first_string(
                    merged,
                    'PriceRateID',
                    'priceRateID',
                    'priceRateId',
                ),
                connection_id=connection_id,
                price_changed=(
                    is_snapshot
                    or previous_prices is None
                    or current_prices != previous_prices
                ),
                state_reconstructed=bool(previous),
                payload_schema=build_payload_schema_sample(
                    patch=patch,
                    merged=merged,
                    observed_at=received_at,
                ),
            )
        )
    return result


def parse_json_frame(raw_message: str) -> object:
    try:
        return json.loads(raw_message)
    except json.JSONDecodeError as original_error:
        decoder = json.JSONDecoder()
        starts = sorted(
            index
            for index in (
                raw_message.find('{'),
                raw_message.find('['),
            )
            if 0 <= index <= 32
        )
        for start in starts:
            try:
                payload, end = decoder.raw_decode(raw_message[start:])
            except json.JSONDecodeError:
                continue
            trailing = raw_message[start + end :]
            if trailing.strip(' \t\r\n\x00\x1e'):
                continue
            return payload
        raise original_error


def _snapshot(
    *,
    symbol: str,
    payload: dict,
    source_timestamp: datetime | None,
    received_at: datetime,
) -> MarketSnapshot | None:
    bid = _first_float(payload, 'Bid', 'bid', 'bidPrice')
    ask = _first_float(payload, 'Ask', 'ask', 'askPrice')
    if bid is None or ask is None:
        return None
    last = _first_float(
        payload,
        'LastExecution',
        'lastExecution',
        'Last',
        'last',
        'lastPrice',
        'Price',
        'price',
    )
    price_source = PriceSource.BROKER_LAST
    if last is None:
        last = (bid + ask) / 2
        price_source = PriceSource.BID_ASK_MIDPOINT
    return MarketSnapshot(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=last,
        timestamp=source_timestamp or received_at,
        received_at=received_at,
        price_source=price_source,
        timestamp_source=(
            TimestampSource.BROKER
            if source_timestamp is not None
            else TimestampSource.LOCAL_RECEIVE_TIME
        ),
    )


def _prices(payload: dict) -> tuple[float, float, float] | None:
    snapshot = _snapshot(
        symbol='TMP',
        payload=payload,
        source_timestamp=None,
        received_at=datetime.now(timezone.utc),
    )
    if snapshot is None:
        return None
    return snapshot.bid, snapshot.ask, snapshot.last


def _content_payload(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = parse_json_frame(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _instrument_id(topic: object) -> int | None:
    if not isinstance(topic, str) or not topic.startswith('instrument:'):
        return None
    try:
        return int(topic.split(':', 1)[1])
    except (IndexError, ValueError):
        return None


def _first_float(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_string(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = _optional_string(payload.get(key))
        if value is not None:
            return value
    return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _first_datetime(payload: dict, *keys: str) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip()
        if normalized.endswith('Z'):
            normalized = f'{normalized[:-1]}+00:00'
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None
