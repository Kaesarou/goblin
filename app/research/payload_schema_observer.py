from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.instruments.models import AssetClass
from app.journal.serialization import serialize_value

logger = logging.getLogger(__name__)

ETORO_PAYLOAD_SCHEMA_OBSERVER_VERSION = 'etoro_payload_schema_v1'
PAYLOAD_SCHEMA_MAX_FIELDS = 256
PAYLOAD_SCHEMA_MAX_EXAMPLES_PER_FIELD = 3
PAYLOAD_SCHEMA_EXAMPLE_MAX_CHARACTERS = 80
PAYLOAD_SCHEMA_FIELD_NAME_MAX_CHARACTERS = 128
PAYLOAD_SCHEMA_FLUSH_INTERVAL_SECONDS = 60

_SENSITIVE_FIELD_TOKENS = (
    'apikey',
    'authorization',
    'credential',
    'password',
    'secret',
    'token',
    'userkey',
)


@dataclass(frozen=True)
class WebSocketPayloadFieldSample:
    field_name: str
    observed_type: str
    numeric_value: float | None
    example: str | int | float | bool | None


@dataclass(frozen=True)
class WebSocketPayloadSchemaSample:
    observed_at: datetime
    patch_fields: tuple[WebSocketPayloadFieldSample, ...]
    merged_fields: tuple[WebSocketPayloadFieldSample, ...]


def build_payload_schema_sample(
    *,
    patch: dict[str, object],
    merged: dict[str, object],
    observed_at: datetime,
) -> WebSocketPayloadSchemaSample:
    return WebSocketPayloadSchemaSample(
        observed_at=_as_utc(observed_at),
        patch_fields=tuple(
            _field_sample(field_name, value)
            for field_name, value in sorted(patch.items())
        ),
        merged_fields=tuple(
            _field_sample(field_name, value)
            for field_name, value in sorted(merged.items())
        ),
    )


class EtoroPayloadSchemaObserver:
    """Persist a bounded aggregate of eToro instrument payload keys."""

    def __init__(
        self,
        *,
        run_id: str,
        paths: tuple[Path, ...],
        flush_interval_seconds: int = PAYLOAD_SCHEMA_FLUSH_INTERVAL_SECONDS,
        maximum_fields: int = PAYLOAD_SCHEMA_MAX_FIELDS,
        maximum_examples_per_field: int = (
            PAYLOAD_SCHEMA_MAX_EXAMPLES_PER_FIELD
        ),
    ) -> None:
        self.run_id = run_id
        self.paths = tuple(dict.fromkeys(Path(path) for path in paths))
        self.flush_interval = timedelta(
            seconds=max(1, flush_interval_seconds)
        )
        self.maximum_fields = max(1, maximum_fields)
        self.maximum_examples_per_field = max(
            0,
            maximum_examples_per_field,
        )
        self._fields: dict[str, dict[str, Any]] = {}
        self._message_count = 0
        self._dropped_field_observations = 0
        self._write_count = 0
        self._write_failure_count = 0
        self._dirty = False
        self._last_write_at: datetime | None = None

    def observe(
        self,
        sample: WebSocketPayloadSchemaSample,
        *,
        asset_class: AssetClass | None,
    ) -> None:
        observed_at = _as_utc(sample.observed_at)
        self._message_count += 1
        significant_change = False
        patch_names = {field.field_name for field in sample.patch_fields}
        merged_names = {field.field_name for field in sample.merged_fields}
        by_name: dict[str, list[WebSocketPayloadFieldSample]] = {}
        for field in (*sample.patch_fields, *sample.merged_fields):
            by_name.setdefault(field.field_name, []).append(field)

        for field_name, samples in by_name.items():
            state = self._fields.get(field_name)
            if state is None:
                if len(self._fields) >= self.maximum_fields:
                    self._dropped_field_observations += 1
                    continue
                state = {
                    'field_name': field_name,
                    'observed_types': set(),
                    'first_seen_at': observed_at,
                    'last_seen_at': observed_at,
                    'patch_presence_count': 0,
                    'merged_presence_count': 0,
                    'asset_classes': set(),
                    'numeric_min': None,
                    'numeric_max': None,
                    'examples': [],
                }
                self._fields[field_name] = state
                significant_change = True

            if field_name in patch_names:
                state['patch_presence_count'] += 1
            if field_name in merged_names:
                state['merged_presence_count'] += 1
            state['last_seen_at'] = observed_at
            if asset_class is not None:
                before = len(state['asset_classes'])
                state['asset_classes'].add(asset_class.value)
                significant_change |= len(state['asset_classes']) != before

            for field in samples:
                before = len(state['observed_types'])
                state['observed_types'].add(field.observed_type)
                significant_change |= len(state['observed_types']) != before
                if field.numeric_value is not None:
                    state['numeric_min'] = _minimum(
                        state['numeric_min'],
                        field.numeric_value,
                    )
                    state['numeric_max'] = _maximum(
                        state['numeric_max'],
                        field.numeric_value,
                    )
                if (
                    field.example is not None
                    and field.example not in state['examples']
                    and len(state['examples'])
                    < self.maximum_examples_per_field
                ):
                    state['examples'].append(field.example)

        self._dirty = True
        if significant_change:
            self.flush(force=True, observed_at=observed_at)
            return
        self.flush(force=False, observed_at=observed_at)

    def flush(
        self,
        *,
        force: bool,
        observed_at: datetime | None = None,
    ) -> bool:
        now = _as_utc(observed_at or datetime.now(UTC))
        if not self._dirty:
            return True
        if (
            not force
            and self._last_write_at is not None
            and now - self._last_write_at < self.flush_interval
        ):
            return True
        payload = self.snapshot(updated_at=now)
        all_written = True
        for path in self.paths:
            try:
                _write_json_atomically(path, payload)
            except Exception:
                all_written = False
                self._write_failure_count += 1
                logger.exception(
                    'eToro payload schema write failed | path=%s',
                    path,
                )
        if all_written:
            self._write_count += 1
            self._dirty = False
            self._last_write_at = now
        return all_written

    def snapshot(self, *, updated_at: datetime) -> dict[str, Any]:
        fields = []
        for field_name in sorted(self._fields):
            state = self._fields[field_name]
            fields.append(
                {
                    'field_name': field_name,
                    'observed_types': sorted(state['observed_types']),
                    'first_seen_at': state['first_seen_at'],
                    'last_seen_at': state['last_seen_at'],
                    'patch_presence_count': state[
                        'patch_presence_count'
                    ],
                    'merged_presence_count': state[
                        'merged_presence_count'
                    ],
                    'asset_classes': sorted(state['asset_classes']),
                    'numeric_min': state['numeric_min'],
                    'numeric_max': state['numeric_max'],
                    'examples': list(state['examples']),
                }
            )
        return {
            'schema_version': ETORO_PAYLOAD_SCHEMA_OBSERVER_VERSION,
            'run_id': self.run_id,
            'updated_at': updated_at,
            'message_count': self._message_count,
            'field_count': len(fields),
            'maximum_fields': self.maximum_fields,
            'maximum_examples_per_field': self.maximum_examples_per_field,
            'dropped_field_observations': (
                self._dropped_field_observations
            ),
            'write_count_before_this_snapshot': self._write_count,
            'write_failure_count': self._write_failure_count,
            'fields': fields,
        }


def payload_schema_contract_metadata() -> dict[str, object]:
    return {
        'version': ETORO_PAYLOAD_SCHEMA_OBSERVER_VERSION,
        'maximum_fields': PAYLOAD_SCHEMA_MAX_FIELDS,
        'maximum_examples_per_field': (
            PAYLOAD_SCHEMA_MAX_EXAMPLES_PER_FIELD
        ),
        'example_max_characters': (
            PAYLOAD_SCHEMA_EXAMPLE_MAX_CHARACTERS
        ),
        'field_name_max_characters': (
            PAYLOAD_SCHEMA_FIELD_NAME_MAX_CHARACTERS
        ),
        'flush_interval_seconds': (
            PAYLOAD_SCHEMA_FLUSH_INTERVAL_SECONDS
        ),
        'raw_payload_retained': False,
        'patch_and_merged_presence_distinguished': True,
    }


def _field_sample(
    field_name: str,
    value: object,
) -> WebSocketPayloadFieldSample:
    normalized_field_name = _bounded_field_name(str(field_name))
    observed_type = _observed_type(value)
    numeric_value = _finite_float(value, observed_type)
    return WebSocketPayloadFieldSample(
        field_name=normalized_field_name,
        observed_type=observed_type,
        numeric_value=numeric_value,
        example=_safe_example(
            normalized_field_name,
            value,
            observed_type,
        ),
    )


def _observed_type(value: object) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, (list, tuple)):
        return 'array'
    return type(value).__name__.lower()


def _safe_example(
    field_name: str,
    value: object,
    observed_type: str,
) -> str | int | float | bool | None:
    normalized_name = ''.join(
        character.lower() for character in field_name if character.isalnum()
    )
    if any(token in normalized_name for token in _SENSITIVE_FIELD_TOKENS):
        return None
    if observed_type in {'null', 'object', 'array'}:
        return None
    if observed_type == 'string':
        return str(value)[:PAYLOAD_SCHEMA_EXAMPLE_MAX_CHARACTERS]
    if observed_type == 'number' and not math.isfinite(float(value)):
        return None
    if observed_type == 'boolean':
        return value  # type: ignore[return-value]
    if observed_type in {'integer', 'number'}:
        rendered = str(value)
        if len(rendered) <= PAYLOAD_SCHEMA_EXAMPLE_MAX_CHARACTERS:
            return value  # type: ignore[return-value]
        return rendered[:PAYLOAD_SCHEMA_EXAMPLE_MAX_CHARACTERS]
    return str(value)[:PAYLOAD_SCHEMA_EXAMPLE_MAX_CHARACTERS]


def _bounded_field_name(field_name: str) -> str:
    if len(field_name) <= PAYLOAD_SCHEMA_FIELD_NAME_MAX_CHARACTERS:
        return field_name
    digest = hashlib.sha256(field_name.encode('utf-8')).hexdigest()[:8]
    prefix_length = PAYLOAD_SCHEMA_FIELD_NAME_MAX_CHARACTERS - len(digest) - 1
    return f'{field_name[:prefix_length]}#{digest}'


def _finite_float(value: object, observed_type: str) -> float | None:
    if observed_type not in {'integer', 'number'}:
        return None
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _minimum(current: float | None, value: float) -> float:
    return value if current is None else min(current, value)


def _maximum(current: float | None, value: float) -> float:
    return value if current is None else max(current, value)


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(
            serialize_value(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        + '\n',
        encoding='utf-8',
    )
    temporary.replace(path)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
