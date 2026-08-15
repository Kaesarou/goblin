import json
from datetime import datetime, timezone

from app.brokers.etoro.websocket_protocol import (
    parse_json_frame,
    parse_websocket_events,
)

NOW = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


def test_parse_json_frame_accepts_control_framing():
    payload = parse_json_frame('\x00{"id":"auth","success":true}\x1e')
    assert payload == {'id': 'auth', 'success': True}


def test_partial_messages_reconstruct_quote_and_do_not_use_rate_id_as_identity():
    state = {}
    first = {
        'messages': [
            {
                'topic': 'instrument:100',
                'type': 'Snapshot',
                'id': 'message-1',
                'content': json.dumps(
                    {
                        'Bid': '99',
                        'Ask': '101',
                        'LastExecution': '100',
                        'Date': '2026-07-20T00:00:00Z',
                        'PriceRateID': 'reused-rate',
                    }
                ),
            }
        ]
    }
    second = {
        'messages': [
            {
                'topic': 'instrument:100',
                'id': 'message-2',
                'content': json.dumps(
                    {
                        'LastExecution': '100.5',
                        'Date': '2026-07-20T00:00:01Z',
                        'PriceRateID': 'reused-rate',
                    }
                ),
            }
        ]
    }

    first_events = parse_websocket_events(
        json.dumps(first),
        symbol_by_instrument_id={100: 'BTC'},
        received_at=NOW,
        connection_id='connection',
        rate_state_by_instrument_id=state,
    )
    second_events = parse_websocket_events(
        json.dumps(second),
        symbol_by_instrument_id={100: 'BTC'},
        received_at=NOW,
        connection_id='connection',
        rate_state_by_instrument_id=state,
    )

    assert first_events[0].snapshot.last == 100.0
    assert second_events[0].snapshot.bid == 99.0
    assert second_events[0].snapshot.ask == 101.0
    assert second_events[0].snapshot.last == 100.5
    assert first_events[0].price_rate_id == second_events[0].price_rate_id
    assert first_events[0].message_id != second_events[0].message_id
    assert {
        field.field_name
        for field in second_events[0].payload_schema.patch_fields
    } == {'LastExecution', 'Date', 'PriceRateID'}
    assert {
        field.field_name
        for field in second_events[0].payload_schema.merged_fields
    } == {'Bid', 'Ask', 'LastExecution', 'Date', 'PriceRateID'}
