import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any


class RuntimeHeartbeat:
    def __init__(self, interval_minutes: int):
        self.interval = timedelta(minutes=max(1, interval_minutes))
        self.last_emitted_at = datetime.now(UTC)

    def maybe_emit(
        self,
        *,
        journal,
        logger: logging.Logger,
        metrics: dict[str, Any],
        open_positions: int,
        active_symbols: int,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(UTC)
        if current_time - self.last_emitted_at < self.interval:
            return False

        payload = {
            **metrics,
            'open_positions': open_positions,
            'active_symbols': active_symbols,
        }
        journal.write('session_heartbeat', payload)
        logger.info(
            'Runtime heartbeat | active_symbols=%s | snapshots=%s | candles=%s | candidates=%s | '
            'orders=%s | open_positions=%s | errors=%s',
            active_symbols,
            metrics.get('market_snapshots', 0),
            metrics.get('candles_closed', 0),
            metrics.get('candidates', 0),
            metrics.get('orders_submitted', 0),
            open_positions,
            metrics.get('errors', 0),
        )
        self.last_emitted_at = current_time
        return True
