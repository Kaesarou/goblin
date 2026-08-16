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
    observed_payloads = []

    def observe_payload(symbol, patch, merged, observed_at):
        observed_payloads.append((symbol, patch, merged, observed_at))

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
        payload_observer=observe_payload,
    )
    second_events = parse_websocket_events(
        json.dumps(second),
        symbol_by_instrument_id={100: 'BTC'},
        received_at=NOW,
        connection_id='connection',
        rate_state_by_instrument_id=state,
        payload_observer=observe_payload,
    )

    assert first_events[0].snapshot.last == 100.0
    assert second_events[0].snapshot.bid == 99.0
    assert second_events[0].snapshot.ask == 101.0
    assert second_events[0].snapshot.last == 100.5
    assert first_events[0].price_rate_id == second_events[0].price_rate_id
    assert first_events[0].message_id != second_events[0].message_id
    assert set(observed_payloads[1][1]) == {
        'LastExecution',
        'Date',
        'PriceRateID',
    }
    assert set(observed_payloads[1][2]) == {
        'Bid',
        'Ask',
        'LastExecution',
        'Date',
        'PriceRateID',
    }
    assert not hasattr(second_events[0], 'payload_schema')


def test_optional_payload_observer_failure_does_not_change_economic_parsing():
    payload = {
        'messages': [
            {
                'topic': 'instrument:100',
                'type': 'Snapshot',
                'content': json.dumps(
                    {'Bid': 99.0, 'Ask': 101.0, 'LastExecution': 100.0}
                ),
            }
        ]
    }

    def fail_observation(*_args):
        raise RuntimeError('research sidecar unavailable')

    events = parse_websocket_events(
        json.dumps(payload),
        symbol_by_instrument_id={100: 'AAPL'},
        received_at=NOW,
        connection_id='connection',
        rate_state_by_instrument_id={},
        payload_observer=fail_observation,
    )

    assert len(events) == 1
    assert events[0].snapshot.bid == 99.0
    assert events[0].snapshot.ask == 101.0
    assert events[0].snapshot.last == 100.0


def test_optional_payload_observer_receives_read_only_transport_views():
    payload = {
        'messages': [
            {
                'topic': 'instrument:100',
                'type': 'Snapshot',
                'content': json.dumps(
                    {'Bid': 99.0, 'Ask': 101.0, 'LastExecution': 100.0}
                ),
            }
        ]
    }
    mutation_blocked = []

    def try_to_mutate(_symbol, patch, merged, _observed_at):
        for view in (patch, merged):
            try:
                view['Bid'] = 0.0
            except TypeError:
                mutation_blocked.append(True)

    state = {}
    events = parse_websocket_events(
        json.dumps(payload),
        symbol_by_instrument_id={100: 'AAPL'},
        received_at=NOW,
        connection_id='connection',
        rate_state_by_instrument_id=state,
        payload_observer=try_to_mutate,
    )

    assert mutation_blocked == [True, True]
    assert events[0].snapshot.bid == 99.0
    assert events[0].snapshot.ask == 101.0
    assert events[0].snapshot.last == 100.0
    assert state[100]['Bid'] == 99.0
