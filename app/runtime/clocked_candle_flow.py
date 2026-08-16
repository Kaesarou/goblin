import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.market.models import MarketSnapshot
from app.market_data.models import CandleBuildResult
from app.runtime.runtime_policy import (
    CANDLE_CLOCK_GRACE_SECONDS,
    CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS,
)
from app.runtime.symbol_flow import process_closed_candle

logger = logging.getLogger(__name__)


class ClockedCandleFlow:
    def _finalize_clocked_candles(self, now: datetime) -> None:
        candle_builders = getattr(self, 'candle_builders', {})
        session_decisions = getattr(self, 'session_decisions', {})
        latest_snapshots = getattr(self, 'latest_snapshots', {})
        for symbol in list(self.active_symbols):
            builder = candle_builders.get(symbol)
            session_decision = session_decisions.get(symbol)
            latest = latest_snapshots.get(symbol)
            if builder is None or session_decision is None or latest is None:
                continue
            for result in builder.finalize_until(
                now,
                grace_seconds=CANDLE_CLOCK_GRACE_SECONDS,
                max_carry_forward_age_seconds=(
                    CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS
                ),
            ):
                self._process_candle_result(
                    symbol=symbol,
                    result=result,
                    latest_snapshot=latest,
                    session_decision=session_decision,
                    now=now,
                    source='clock',
                )
        self._emit_due_research_states(now)

    def _process_candle_result(
        self,
        *,
        symbol: str,
        result: CandleBuildResult,
        latest_snapshot: MarketSnapshot,
        session_decision,
        now: datetime,
        source: str,
    ) -> None:
        enriched_candle = replace(
            result.candle,
            carried_forward=result.quality.carried_forward,
            source_price_age_seconds=result.quality.last_price_age_seconds,
            quality_degraded=result.quality.degraded,
        )
        entry_allowed = (
            self.coordinator.entry_allowed(symbol)
            and not result.quality.degraded
        )
        effective_session = (
            session_decision
            if entry_allowed
            else session_decision._replace(
                new_entries_allowed=False,
                reason=(
                    'candle_quality_degraded'
                    if result.quality.degraded
                    else 'market_data_degraded'
                ),
            )
        )
        decision_snapshot = replace(
            latest_snapshot,
            last=enriched_candle.close,
            timestamp=enriched_candle.closed_at,
            received_at=_as_utc(now),
        )
        candidate = process_closed_candle(
            symbol=symbol,
            snapshot=decision_snapshot,
            closed_candle=enriched_candle,
            strategy=self.strategies[symbol],
            risk_manager=self.risk_manager,
            trade_journal=self.trade_journal,
            candle_journal=self.candle_journal,
            session_decision=effective_session,
            loop_id=self.loop_id,
            pending_entry_manager=self.pending_entry_manager,
            cooldown_guard=self.cooldown_guard,
            market_context_service=self.market_context_service,
            multi_timeframe_service=self.multi_timeframe_service,
            run_id=self.run_id,
        )
        self.candle_journal.write(
            'candle_finalized',
            {
                'symbol': symbol,
                'candle': enriched_candle,
                'quality': result.quality,
                'entry_allowed': entry_allowed,
                'feed_state': self.coordinator.state_for(symbol).value,
                'finalization_source': source,
                'finalized_at': _as_utc(now),
                'loop_id': self.loop_id,
            },
        )
        recorded = self.decision_windows.record(
            closed_at=enriched_candle.closed_at,
            symbol=symbol,
            expected_symbols=self.active_symbols,
            candidate=candidate,
        )
        if not recorded:
            self.trade_journal.write(
                'decision_window_late_symbol',
                {
                    'symbol': symbol,
                    'closed_at': enriched_candle.closed_at,
                    'candidate': candidate,
                    'quality': result.quality,
                    'finalization_source': source,
                },
            )
        self._last_bucket_by_symbol[symbol] = enriched_candle.opened_at

    def _emit_due_research_states(self, now: datetime) -> None:
        research_pipeline = getattr(self, 'research_pipeline', None)
        if research_pipeline is None:
            return
        state_at = _latest_due_research_boundary(
            now,
            cadence_minutes=research_pipeline.sampling_cadence_minutes,
        )
        if state_at == getattr(self, '_last_research_boundary', None):
            return
        self._last_research_boundary = state_at
        session_decisions = getattr(self, 'session_decisions', {})
        try:
            research_pipeline.emit_boundary(
                symbols=list(getattr(self, 'symbols', ())),
                state_at=state_at,
                session_decisions=session_decisions,
            )
        except Exception:  # noqa: BLE001 - final research isolation boundary
            logger.exception(
                'Research boundary failed without affecting trading | state_at=%s',
                state_at,
            )


def _latest_due_research_boundary(
    value: datetime,
    *,
    cadence_minutes: int,
) -> datetime:
    eligible = _as_utc(value) - timedelta(
        seconds=CANDLE_CLOCK_GRACE_SECONDS
    )
    minute = eligible.minute - eligible.minute % cadence_minutes
    return eligible.replace(minute=minute, second=0, microsecond=0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
