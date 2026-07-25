from datetime import datetime

from app.runtime.runtime_policy import (
    POSITION_FALLBACK_INTERVAL_SECONDS,
    REST_CONTROL_INTERVAL_SECONDS,
)
from app.runtime.session_runtime import session_timestamp_rejection_reason


class MarketDataMaintenance:
    def _all_monitored_symbols(self) -> list[str]:
        result = list(self.symbols)
        active_assets = {
            self.instrument_registry.resolve(symbol).asset_class
            for symbol in self.symbols
        }
        for asset_class, configured in (
            self.settings.benchmark_symbols_by_asset_class().items()
        ):
            if asset_class in active_assets:
                result.extend(configured)
        return list(
            dict.fromkeys(symbol.strip().upper() for symbol in result)
        )

    def _update_context_if_due(self, monotonic_now: float) -> None:
        if monotonic_now - self._last_context_update < 1.0:
            return
        self._last_context_update = monotonic_now
        relevant = {}
        for symbol in [*self.active_symbols, *self.context_asset_classes]:
            snapshot = self.latest_snapshots.get(symbol)
            decision = self._session_decision_for_market_data_symbol(symbol)
            if snapshot is None or decision is None:
                continue
            if (
                session_timestamp_rejection_reason(
                    decision=decision,
                    timestamp=snapshot.timestamp,
                )
                is not None
            ):
                continue
            relevant[symbol] = snapshot
        if not relevant:
            return
        self.market_context_service.update(
            snapshots=relevant,
            session_decisions=self.session_decisions,
            context_asset_classes=self.context_asset_classes,
        )

    def _applied_monitored_symbols(self) -> list[str]:
        applied = set(self._applied_feed_symbols)
        return [
            symbol
            for symbol in self._desired_market_data_symbols()
            if symbol in applied
        ]

    def _run_position_fallback_if_due(
        self,
        now: datetime,
        monotonic_now: float,
    ) -> None:
        if (
            monotonic_now - self._last_position_fallback
            < POSITION_FALLBACK_INTERVAL_SECONDS
        ):
            return
        applied = set(self._applied_feed_symbols)
        open_symbols = list(
            dict.fromkeys(
                position.symbol.strip().upper()
                for position in self.position_tracker.open_positions_snapshot()
                if position.symbol.strip().upper() in applied
            )
        )
        if not open_symbols:
            return
        fallback_symbols = self.coordinator.position_fallback_symbols(
            symbols=open_symbols,
            now=now,
            force=not self.live_market_data.connection_healthy(),
        )
        if not fallback_symbols:
            return
        scheduled = self.broker_operations.schedule_position_fallback(
            symbols=fallback_symbols,
            now=now,
        )
        if scheduled:
            self._last_position_fallback = monotonic_now

    def _run_rest_control_if_due(
        self,
        now: datetime,
        monotonic_now: float,
    ) -> None:
        if (
            monotonic_now - self._last_rest_control
            < REST_CONTROL_INTERVAL_SECONDS
        ):
            return
        monitored = self._applied_monitored_symbols()
        if not monitored:
            self._last_rest_control = monotonic_now
            return
        scheduled = self.broker_operations.schedule_rest_control(
            symbols=monitored,
            now=now,
        )
        if scheduled:
            self._last_rest_control = monotonic_now

    def _flush_decision_windows(self, now: datetime) -> None:
        for batch in self.decision_windows.pop_ready_batches(now=now):
            self.trade_journal.write(
                'decision_window_finalized',
                {
                    'closed_at': batch.closed_at,
                    'finalization_reason': batch.finalization_reason,
                    'expected_symbol_count': len(batch.expected_symbols),
                    'completed_symbol_count': len(batch.completed_symbols),
                    'missing_symbol_count': len(batch.missing_symbols),
                    'missing_symbols': list(batch.missing_symbols),
                    'candidate_count': len(batch.candidates),
                },
            )
            self.candidate_execution.submit_candidates(
                list(batch.candidates),
                now=now,
            )
