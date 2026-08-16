import gzip
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from app.journal.serialization import serialize_value

logger = logging.getLogger(__name__)


class JsonlJournal:
    def __init__(
        self,
        path: str,
        *,
        run_id: str | None = None,
        stream_name: str | None = None,
        compact: bool = False,
    ):
        self.path = Path(path)
        self.run_id = run_id
        self.stream_name = stream_name or self.path.name
        self.compact = compact
        self.sequence = 0
        self.written_count = 0
        self.failed_count = 0
        self.open_count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event_type: str, payload: dict[str, Any]) -> bool:
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
        """Append one all-or-nothing logical batch with a single file open."""
        batch = list(events)
        if not batch:
            return 0
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

    def _open_append(self) -> TextIO:
        self.open_count += 1
        if self.path.suffix == '.gz':
            return gzip.open(self.path, 'at', encoding='utf-8')
        return self.path.open('a', encoding='utf-8')

    def _serialize(self, value: Any) -> Any:
        return serialize_value(value)
