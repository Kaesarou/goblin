import json
from datetime import UTC, datetime, timedelta

from app.instruments.models import AssetClass
from app.research.payload_schema_observer import (
    EtoroPayloadSchemaObserver,
    build_payload_schema_sample,
)

OBSERVED_AT = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def _sample(*, patch: dict, merged: dict, seconds: int = 0):
    return build_payload_schema_sample(
        patch=patch,
        merged=merged,
        observed_at=OBSERVED_AT + timedelta(seconds=seconds),
    )


def _fields(path):
    payload = json.loads(path.read_text(encoding='utf-8'))
    return payload, {
        field['field_name']: field for field in payload['fields']
    }


def test_patch_presence_is_distinct_from_reconstructed_merged_presence(tmp_path):
    path = tmp_path / 'data' / 'logs' / 'etoro_payload_schema.json'
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(path,),
    )
    observer.observe(
        _sample(
            patch={'Bid': 100.0, 'Mystery': 42},
            merged={'Bid': 100.0, 'Ask': 100.2, 'Mystery': 42},
        ),
        asset_class=AssetClass.EQUITY_US,
    )
    observer.observe(
        _sample(
            patch={'Bid': 100.1},
            merged={'Bid': 100.1, 'Ask': 100.2, 'Mystery': 42},
            seconds=1,
        ),
        asset_class=AssetClass.EQUITY_US,
    )
    observer.flush(force=True, observed_at=OBSERVED_AT + timedelta(seconds=2))

    payload, fields = _fields(path)

    assert payload['message_count'] == 2
    assert fields['Bid']['patch_presence_count'] == 2
    assert fields['Bid']['merged_presence_count'] == 2
    assert fields['Ask']['patch_presence_count'] == 0
    assert fields['Ask']['merged_presence_count'] == 2
    assert fields['Mystery']['patch_presence_count'] == 1
    assert fields['Mystery']['merged_presence_count'] == 2
    assert fields['Mystery']['numeric_min'] == 42.0
    assert fields['Mystery']['numeric_max'] == 42.0
    assert fields['Mystery']['asset_classes'] == ['EQUITY_US']


def test_new_field_and_type_are_persisted_immediately_and_atomically(tmp_path):
    path = tmp_path / 'data' / 'logs' / 'etoro_payload_schema.json'
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(path,),
        flush_interval_seconds=3600,
    )
    observer.observe(
        _sample(patch={'Unknown': 1}, merged={'Unknown': 1}),
        asset_class=AssetClass.EQUITY_EU,
    )

    assert path.exists()
    first_mtime = path.stat().st_mtime_ns
    assert not path.with_suffix('.json.tmp').exists()

    observer.observe(
        _sample(
            patch={'Unknown': 'one'},
            merged={'Unknown': 'one'},
            seconds=1,
        ),
        asset_class=AssetClass.EQUITY_EU,
    )
    _, fields = _fields(path)

    assert path.stat().st_mtime_ns >= first_mtime
    assert fields['Unknown']['observed_types'] == ['integer', 'string']
    assert fields['Unknown']['first_seen_at'] == OBSERVED_AT.isoformat()
    assert fields['Unknown']['last_seen_at'] == (
        OBSERVED_AT + timedelta(seconds=1)
    ).isoformat()
    assert not path.with_suffix('.json.tmp').exists()


def test_examples_are_bounded_truncated_and_sensitive_values_are_redacted(
    tmp_path,
):
    path = tmp_path / 'data' / 'logs' / 'etoro_payload_schema.json'
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(path,),
        maximum_examples_per_field=2,
    )
    for index, value in enumerate(('x' * 120, 'second', 'third')):
        observer.observe(
            _sample(
                patch={
                    'Description': value,
                    'AuthorizationToken': 'never-write-this-secret',
                },
                merged={
                    'Description': value,
                    'AuthorizationToken': 'never-write-this-secret',
                },
                seconds=index,
            ),
            asset_class=AssetClass.EQUITY_US,
        )
    observer.flush(force=True, observed_at=OBSERVED_AT + timedelta(seconds=4))

    raw = path.read_text(encoding='utf-8')
    _, fields = _fields(path)

    assert len(fields['Description']['examples']) == 2
    assert len(fields['Description']['examples'][0]) == 80
    assert fields['AuthorizationToken']['examples'] == []
    assert 'never-write-this-secret' not in raw
    assert 'raw_payload' not in raw


def test_field_count_and_examples_remain_bounded(tmp_path):
    path = tmp_path / 'data' / 'logs' / 'etoro_payload_schema.json'
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(path,),
        maximum_fields=2,
        maximum_examples_per_field=1,
    )
    observer.observe(
        _sample(
            patch={'A': 1, 'B': 2, 'C': 3},
            merged={'A': 1, 'B': 2, 'C': 3},
        ),
        asset_class=AssetClass.EQUITY_US,
    )

    payload, fields = _fields(path)

    assert payload['field_count'] == 2
    assert payload['dropped_field_observations'] == 1
    assert set(fields) == {'A', 'B'}
    assert all(len(field['examples']) <= 1 for field in fields.values())


def test_non_finite_numbers_never_create_invalid_json(tmp_path):
    path = tmp_path / 'data' / 'logs' / 'etoro_payload_schema.json'
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(path,),
    )
    observer.observe(
        _sample(patch={'Unknown': float('nan')}, merged={'Unknown': float('nan')}),
        asset_class=AssetClass.EQUITY_US,
    )

    payload, fields = _fields(path)

    assert payload['field_count'] == 1
    assert fields['Unknown']['numeric_min'] is None
    assert fields['Unknown']['numeric_max'] is None
    assert fields['Unknown']['examples'] == []


def test_field_names_and_large_numeric_examples_are_size_bounded(tmp_path):
    path = tmp_path / 'data' / 'logs' / 'etoro_payload_schema.json'
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(path,),
    )
    long_name = 'UnknownField' * 100
    observer.observe(
        _sample(
            patch={long_name: 10**200},
            merged={long_name: 10**200},
        ),
        asset_class=AssetClass.EQUITY_US,
    )

    _, fields = _fields(path)
    field = next(iter(fields.values()))

    assert len(field['field_name']) <= 128
    assert len(str(field['examples'][0])) <= 80


def test_schema_persistence_failure_is_contained_and_counted(tmp_path):
    blocked_parent = tmp_path / 'blocked'
    blocked_parent.write_text('not-a-directory', encoding='utf-8')
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(blocked_parent / 'schema.json',),
    )

    observer.observe(
        _sample(patch={'Unknown': 1}, merged={'Unknown': 1}),
        asset_class=AssetClass.EQUITY_US,
    )
    snapshot = observer.snapshot(updated_at=OBSERVED_AT)

    assert snapshot['write_failure_count'] == 1
    assert observer.flush(force=True, observed_at=OBSERVED_AT) is False


def test_schema_write_failure_is_rate_limited_instead_of_retrying_each_quote(
    tmp_path,
):
    blocked_parent = tmp_path / 'blocked'
    blocked_parent.write_text('not-a-directory', encoding='utf-8')
    observer = EtoroPayloadSchemaObserver(
        run_id='run-test',
        paths=(blocked_parent / 'schema.json',),
        flush_interval_seconds=60,
    )

    observer.observe(
        _sample(patch={'Unknown': 1}, merged={'Unknown': 1}),
        asset_class=AssetClass.EQUITY_US,
    )
    observer.observe(
        _sample(
            patch={'Unknown': 1},
            merged={'Unknown': 1},
            seconds=1,
        ),
        asset_class=AssetClass.EQUITY_US,
    )

    assert observer.snapshot(updated_at=OBSERVED_AT)[
        'write_failure_count'
    ] == 1

    observer.observe(
        _sample(
            patch={'Unknown': 1},
            merged={'Unknown': 1},
            seconds=61,
        ),
        asset_class=AssetClass.EQUITY_US,
    )

    assert observer.snapshot(updated_at=OBSERVED_AT)[
        'write_failure_count'
    ] == 2
