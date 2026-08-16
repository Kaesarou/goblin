from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.journal.serialization import serialize_value

RESEARCH_SUMMARY_SCHEMA_VERSION = 'research_summary_v1'
RESEARCH_EXPECTED_STATE_DEFINITION = (
    'At each unique five-minute boundary observed after run start, one state '
    'is expected for every configured research symbol whose session is '
    'research-tradable at that boundary, even when quote, candle, context, '
    'or microstructure inputs are unavailable.'
)
RESEARCH_EMITTED_STATE_DEFINITION = (
    'A state is emitted only after its research_state JSONL record is '
    'successfully persisted.'
)


def empty_research_summary(
    *,
    run_id: str,
    enabled: bool,
    updated_at: datetime,
) -> dict[str, Any]:
    return {
        'schema_version': RESEARCH_SUMMARY_SCHEMA_VERSION,
        'run_id': run_id,
        'updated_at': _as_utc(updated_at),
        'research_enabled': enabled,
        'expected_state_definition': RESEARCH_EXPECTED_STATE_DEFINITION,
        'emitted_state_definition': RESEARCH_EMITTED_STATE_DEFINITION,
        'expected_state_count': 0,
        'emitted_state_count': 0,
        'skipped_not_tradable_count': 0,
        'duplicate_prevented_count': 0,
        'state_calculation_failure_count': 0,
        'journal_write_failure_count': 0,
        'payload_schema_failure_count': 0,
        'states_missing_quote_count': 0,
        'states_missing_candle_count': 0,
        'states_missing_boundary_candle_count': 0,
        'states_with_micro_10s_count': 0,
        'states_with_micro_30s_count': 0,
        'states_with_micro_60s_count': 0,
        'first_state_at': None,
        'last_state_at': None,
        'boundary_count': 0,
        'last_boundary_at': None,
        'last_boundary_duration_ms': None,
        'last_boundary_calculation_ms': None,
        'last_boundary_persistence_ms': None,
        'maximum_boundary_duration_ms': None,
        'maximum_boundary_calculation_ms': None,
        'maximum_boundary_persistence_ms': None,
        'total_research_processing_ms': 0.0,
        'research_journal_open_count': 0,
        'summary_write_failure_count': 0,
    }


def research_summary_contract_metadata() -> dict[str, object]:
    return {
        'schema_version': RESEARCH_SUMMARY_SCHEMA_VERSION,
        'role': 'run_scoped_research_completeness_and_health',
        'expected_state_definition': RESEARCH_EXPECTED_STATE_DEFINITION,
        'emitted_state_definition': RESEARCH_EMITTED_STATE_DEFINITION,
        'missing_candle_definitions': {
            'states_missing_candle_count': (
                'No causal closed M1 candle exists at or before state_at.'
            ),
            'states_missing_boundary_candle_count': (
                'No causal closed M1 candle exists with closed_at == state_at.'
            ),
        },
        'atomic_write': True,
        'update_cadence': 'startup_each_observed_boundary_and_clean_shutdown',
        'bounded': True,
    }


def write_research_summary(path: str | Path, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.tmp')
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
    temporary.replace(destination)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
