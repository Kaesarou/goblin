from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.persistence.closed_trade_memory_store import ClosedTradeMemoryStore
from app.risk.trade_cooldown import ClosedTradeMemoryEntry
from app.strategies.guards.post_tp_reset_detector import PostTpResetDecision, PostTpResetDetector

if TYPE_CHECKING:
    from app.execution.trade_candidate import TradeCandidate

CONSUMED_MOVE_AFTER_TP_REASON = 'consumed_move_after_take_profit'
POST_TP_RESET_CONFIRMED_REASON = 'post_tp_reset_confirmed'


@dataclass(frozen=True)
class PostTpReentryConfig:
    enabled: bool = True
    smart_watch_minutes: int = 60


@dataclass(frozen=True)
class PostTpReentryDecision:
    allowed: bool
    reason: str | None = None
    previous_trade: ClosedTradeMemoryEntry | None = None
    reset_decision: PostTpResetDecision | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class RejectedPostTpReentryCandidate:
    candidate: 'TradeCandidate'
    decision: PostTpReentryDecision


@dataclass(frozen=True)
class PostTpReentryFilterResult:
    selected_candidates: list['TradeCandidate']
    rejected_candidates: list[RejectedPostTpReentryCandidate]


class PostTpReentryGuard:
    def __init__(self, store: ClosedTradeMemoryStore, reset_detector: PostTpResetDetector | None = None):
        self.store = store
        self.reset_detector = reset_detector or PostTpResetDetector()

    def check(self, *, candidate: 'TradeCandidate', config: PostTpReentryConfig, now: datetime) -> PostTpReentryDecision:
        if not config.enabled:
            return PostTpReentryDecision(allowed=True)
        side = candidate.signal.action.strip().upper()
        if side not in ('BUY', 'SELL'):
            return PostTpReentryDecision(allowed=True)
        previous = self.store.find_recent_take_profit(
            symbol=candidate.symbol,
            side=side,
            now=now,
            lookback_minutes=config.smart_watch_minutes,
        )
        if previous is None:
            return PostTpReentryDecision(allowed=True)
        reset = self.reset_detector.detect_reset(previous_trade=previous, candidate=candidate)
        details = {
            'previous_trade_id': previous.position_id,
            'previous_closed_at': previous.closed_at.isoformat(),
            'minutes_since_take_profit': round((now - previous.closed_at).total_seconds() / 60, 4),
            'smart_watch_minutes': config.smart_watch_minutes,
            'reset_valid': reset.valid,
            'reset_type': reset.reset_type,
            'reset_details': reset.details,
        }
        if reset.valid:
            return PostTpReentryDecision(True, POST_TP_RESET_CONFIRMED_REASON, previous, reset, details)
        return PostTpReentryDecision(False, CONSUMED_MOVE_AFTER_TP_REASON, previous, reset, details)

    def filter_candidates(
        self,
        *,
        candidates: list['TradeCandidate'],
        config_for_candidate: Callable[['TradeCandidate'], PostTpReentryConfig],
        now: datetime,
    ) -> PostTpReentryFilterResult:
        selected: list[TradeCandidate] = []
        rejected: list[RejectedPostTpReentryCandidate] = []
        for candidate in candidates:
            decision = self.check(candidate=candidate, config=config_for_candidate(candidate), now=now)
            if decision.allowed:
                selected.append(candidate)
            else:
                rejected.append(RejectedPostTpReentryCandidate(candidate, decision))
        return PostTpReentryFilterResult(selected, rejected)
