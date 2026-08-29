import gzip
import json
import logging
import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from app.journal.serialization import serialize_value

logger = logging.getLogger(__name__)

TRADE_JOURNAL_SOFT_MAX_BYTES = 256 * 1024 * 1024
TRADE_JOURNAL_HARD_MAX_BYTES = 384 * 1024 * 1024
TRADE_JOURNAL_MIN_FREE_BYTES = 1024 * 1024 * 1024
# Only events reachable from the V3 production entry point receive soft-budget
# priority. V1 lifecycle names were deliberately removed so dead contracts cannot
# silently retain authority through the journal layer.
TRADE_JOURNAL_CRITICAL_EVENT_TYPES = frozenset(
    {
        'runtime_started',
        'runtime_stopped',
        'error',
        'v3_runtime_started',
        'v3_runtime_stopped',
        'v3_runtime_interrupted',
        'v3_startup_rejected',
        'v3_inventory_event',
        'v3_intent_triggered',
        'raw_journal_budget_exhausted',
    }
)


class JsonlJournal:
    def __init__(
        self,
        path: str,
        *,
        run_id: str | None = None,
        stream_name: str | None = None,
        compact: bool = False,
        soft_max_bytes: int | None = None,
        hard_max_bytes: int | None = None,
        min_free_bytes: int | None = None,
        critical_event_types: Iterable[str] | None = None,
    ):
        self.path = Path(path)
        self.run_id = run_id
        self.stream_name = stream_name or self.path.name
        self.compact = compact
        self.sequence = 0
        self.written_count = 0
        self.failed_count = 0
        self.open_count = 0
        self.suppressed_count = 0
        self.budget_reason: str | None = None
        self._budget_warnings: set[str] = set()

        trade_stream = self.stream_name == 'trades'
        if trade_stream and soft_max_bytes is None:
            soft_max_bytes = TRADE_JOURNAL_SOFT_MAX_BYTES
        if trade_stream and hard_max_bytes is None:
            hard_max_bytes = TRADE_JOURNAL_HARD_MAX_BYTES
        if trade_stream and min_free_bytes is None:
            min_free_bytes = TRADE_JOURNAL_MIN_FREE_BYTES
        if hard_max_bytes is not None and soft_max_bytes is not None:
            if hard_max_bytes < soft_max_bytes:
                raise ValueError('hard_max_bytes must be >= soft_max_bytes')

        self.soft_max_bytes = soft_max_bytes
        self.hard_max_bytes = hard_max_bytes
        self.min_free_bytes = min_free_bytes
        self.critical_event_types = (
            TRADE_JOURNAL_CRITICAL_EVENT_TYPES
            if trade_stream and critical_event_types is None
            else frozenset(critical_event_types or ())
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event_type: str, payload: dict[str, Any]) -> bool:
        if self._budget_blocks_write(event_type):
            self.suppressed_count += 1
            return True

        next_sequence = self.sequence + 1
        try:
            record = {
                'schema_version': 1,
                'run_id': self.run_id,
                'stream': self.stream_name,
                'sequence': next_sequence,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'event_type': event_type,
                'payload': serialize_value(payload),
            }
            with self._open_append() as file:
                file.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(',', ':') if self.compact else None,
                    )
                    + '\n'
                )

            self.sequence = next_sequence
            self.written_count += 1
            return True

        except Exception as exc:
            self.failed_count += 1
            logger.exception(
                'Journal write failed | path=%s | event_type=%s | error=%s',
                self.path,
                event_type,
                exc,
            )
            return False

    def write_many(
        self,
        events: Iterable[tuple[str, dict[str, Any]]],
    ) -> int:
        """Append one all-or-nothing logical batch with a single file open.

        Budgeted streams are intentionally written through ``write`` so every
        record is subject to the same disk guardrail. Unbudgeted high-volume
        streams keep the original single-open batch behavior.
        """
        batch = list(events)
        if not batch:
            return 0
        if self._budget_enabled():
            physical_writes = 0
            for event_type, payload in batch:
                before = self.written_count
                if not self.write(event_type, payload):
                    break
                if self.written_count > before:
                    physical_writes += 1
            return physical_writes

        first_sequence = self.sequence + 1
        try:
            rendered_records = []
            for offset, (event_type, payload) in enumerate(batch):
                record = {
                    'schema_version': 1,
                    'run_id': self.run_id,
                    'stream': self.stream_name,
                    'sequence': first_sequence + offset,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'event_type': event_type,
                    'payload': serialize_value(payload),
                }
                rendered_records.append(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(
                            (',', ':') if self.compact else None
                        ),
                    )
                    + '\n'
                )
            with self._open_append() as file:
                file.write(''.join(rendered_records))

            written = len(batch)
            self.sequence += written
            self.written_count += written
            return written
        except Exception as exc:
            self.failed_count += len(batch)
            logger.exception(
                'Journal batch write failed | path=%s | events=%d | error=%s',
                self.path,
                len(batch),
                exc,
            )
            return 0

    def _budget_enabled(self) -> bool:
        return any(
            value is not None
            for value in (
                self.soft_max_bytes,
                self.hard_max_bytes,
                self.min_free_bytes,
            )
        )

    def _budget_blocks_write(self, event_type: str) -> bool:
        if not self._budget_enabled():
            return False

        size = self.path.stat().st_size if self.path.exists() else 0
        if self.hard_max_bytes is not None and size >= self.hard_max_bytes:
            self._activate_budget_reason('hard_max_bytes', size=size)
            return True

        if self.min_free_bytes is not None:
            try:
                free = shutil.disk_usage(self.path.parent).free
            except OSError as exc:
                logger.warning(
                    'Journal disk budget check failed | path=%s | error=%s',
                    self.path,
                    exc,
                )
            else:
                if free <= self.min_free_bytes:
                    self._activate_budget_reason(
                        'min_free_bytes',
                        size=size,
                        free=free,
                    )
                    return True

        if (
            self.soft_max_bytes is not None
            and size >= self.soft_max_bytes
            and event_type not in self.critical_event_types
        ):
            self._activate_budget_reason('soft_max_bytes', size=size)
            return True

        return False

    def _activate_budget_reason(
        self,
        reason: str,
        *,
        size: int,
        free: int | None = None,
    ) -> None:
        self.budget_reason = reason
        if reason in self._budget_warnings:
            return
        self._budget_warnings.add(reason)
        logger.warning(
            'Journal budget active | stream=%s | reason=%s | path=%s | '
            'size=%s | free=%s | soft_max=%s | hard_max=%s | min_free=%s',
            self.stream_name,
            reason,
            self.path,
            size,
            free,
            self.soft_max_bytes,
            self.hard_max_bytes,
            self.min_free_bytes,
        )

    def _open_append(self) -> TextIO:
        self.open_count += 1
        if self.path.suffix == '.gz':
            return gzip.open(self.path, 'at', encoding='utf-8')
        return self.path.open('a', encoding='utf-8')

    def _serialize(self, value: Any) -> Any:
        return serialize_value(value)
