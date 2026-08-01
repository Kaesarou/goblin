from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.risk.trade_cooldown import ClosedTradeMemoryEntry

if TYPE_CHECKING:
    from app.execution.trade_candidate import TradeCandidate


@dataclass(frozen=True)
class PostTpResetConfig:
    pullback_ratio_of_move: float = 0.35
    min_pullback_percent: float = 0.15
    range_min_minutes: float = 6.0
    range_max_width_ratio_of_move: float = 0.35
    breakout_buffer_ratio_of_move: float = 0.10
    min_breakout_buffer_percent: float = 0.03


@dataclass(frozen=True)
class PostTpResetDecision:
    valid: bool
    reset_type: str = 'none'
    details: dict[str, Any] | None = None


class PostTpResetDetector:
    def __init__(self, config: PostTpResetConfig | None = None):
        self.config = config or PostTpResetConfig()

    def detect_reset(self, *, previous_trade: ClosedTradeMemoryEntry, candidate: 'TradeCandidate') -> PostTpResetDecision:
        side = candidate.signal.action.strip().upper()
        if side == 'BUY':
            return self._detect_buy_reset(previous_trade=previous_trade, candidate=candidate)
        if side == 'SELL':
            return self._detect_sell_reset(previous_trade=previous_trade, candidate=candidate)
        return PostTpResetDecision(valid=True, reset_type='unsupported_side')

    def _detect_buy_reset(self, *, previous_trade: ClosedTradeMemoryEntry, candidate: 'TradeCandidate') -> PostTpResetDecision:
        move_unit_percent = self._move_unit_percent(previous_trade)
        required_pullback_percent = self._required_pullback_percent(move_unit_percent)
        reference_high = max(
            self._positive_or_zero(
                previous_trade.highest_executable_price
            ),
            self._positive_or_zero(previous_trade.pnl_exit_price),
        )
        current_price = candidate.snapshot.last
        pullback_percent = self._percent_drop(reference_price=reference_high, current_price=current_price)
        pullback_valid = pullback_percent >= required_pullback_percent
        range_decision = self._range_reset_decision(candidate=candidate, side='BUY', move_unit_percent=move_unit_percent)
        details = {
            'move_unit_percent': round(move_unit_percent, 4),
            'reference_high': reference_high,
            'current_price': current_price,
            'pullback_percent': round(pullback_percent, 4),
            'required_pullback_percent': round(required_pullback_percent, 4),
            'pullback_valid': pullback_valid,
            'range_reset_valid': range_decision.valid,
            'range_reset_details': range_decision.details,
        }
        if pullback_valid:
            return PostTpResetDecision(valid=True, reset_type='pullback', details=details)
        if range_decision.valid:
            return PostTpResetDecision(valid=True, reset_type=range_decision.reset_type, details=details)
        return PostTpResetDecision(valid=False, details=details)

    def _detect_sell_reset(self, *, previous_trade: ClosedTradeMemoryEntry, candidate: 'TradeCandidate') -> PostTpResetDecision:
        move_unit_percent = self._move_unit_percent(previous_trade)
        required_pullback_percent = self._required_pullback_percent(move_unit_percent)
        reference_low = self._first_positive(
            previous_trade.lowest_executable_price,
            previous_trade.pnl_exit_price,
        )
        current_price = candidate.snapshot.last
        bounce_percent = self._percent_rise(reference_price=reference_low, current_price=current_price)
        pullback_valid = bounce_percent >= required_pullback_percent
        range_decision = self._range_reset_decision(candidate=candidate, side='SELL', move_unit_percent=move_unit_percent)
        details = {
            'move_unit_percent': round(move_unit_percent, 4),
            'reference_low': reference_low,
            'current_price': current_price,
            'pullback_percent': round(bounce_percent, 4),
            'required_pullback_percent': round(required_pullback_percent, 4),
            'pullback_valid': pullback_valid,
            'range_reset_valid': range_decision.valid,
            'range_reset_details': range_decision.details,
        }
        if pullback_valid:
            return PostTpResetDecision(valid=True, reset_type='pullback', details=details)
        if range_decision.valid:
            return PostTpResetDecision(valid=True, reset_type=range_decision.reset_type, details=details)
        return PostTpResetDecision(valid=False, details=details)

    def _range_reset_decision(self, *, candidate: 'TradeCandidate', side: str, move_unit_percent: float) -> PostTpResetDecision:
        metadata = candidate.signal.metadata or {}
        range_minutes = self._optional_float(metadata.get('post_tp_range_minutes'))
        range_width_percent = self._optional_float(metadata.get('post_tp_range_width_percent'))
        breakout_percent = self._optional_float(metadata.get('post_tp_range_breakout_percent'))
        required_breakout_percent = max(self.config.min_breakout_buffer_percent, move_unit_percent * self.config.breakout_buffer_ratio_of_move)
        max_range_width_percent = move_unit_percent * self.config.range_max_width_ratio_of_move
        details = {
            'range_minutes': range_minutes,
            'range_width_percent': range_width_percent,
            'breakout_percent': breakout_percent,
            'required_range_minutes': self.config.range_min_minutes,
            'max_range_width_percent': round(max_range_width_percent, 4),
            'required_breakout_percent': round(required_breakout_percent, 4),
            'side': side,
        }
        if range_minutes is None or range_width_percent is None or breakout_percent is None:
            return PostTpResetDecision(valid=False, details=details)
        if range_minutes < self.config.range_min_minutes:
            return PostTpResetDecision(valid=False, details=details)
        if range_width_percent > max_range_width_percent:
            return PostTpResetDecision(valid=False, details=details)
        if breakout_percent < required_breakout_percent:
            return PostTpResetDecision(valid=False, details=details)
        return PostTpResetDecision(valid=True, reset_type='range_breakout', details=details)

    def _move_unit_percent(self, previous_trade: ClosedTradeMemoryEntry) -> float:
        entry_price = self._positive_or_zero(previous_trade.pnl_entry_price)
        exit_price = self._positive_or_zero(previous_trade.pnl_exit_price)
        take_profit = self._positive_or_zero(previous_trade.take_profit)
        if entry_price <= 0:
            return 0.10
        captured_move_percent = abs(exit_price - entry_price) / entry_price * 100 if exit_price > 0 else 0.0
        planned_tp_percent = abs(take_profit - entry_price) / entry_price * 100 if take_profit > 0 else 0.0
        return max(captured_move_percent, planned_tp_percent, 0.10)

    def _required_pullback_percent(self, move_unit_percent: float) -> float:
        return max(self.config.min_pullback_percent, move_unit_percent * self.config.pullback_ratio_of_move)

    def _percent_drop(self, *, reference_price: float, current_price: float) -> float:
        if reference_price <= 0:
            return 0.0
        return max(((reference_price - current_price) / reference_price) * 100, 0.0)

    def _percent_rise(self, *, reference_price: float, current_price: float) -> float:
        if reference_price <= 0:
            return 0.0
        return max(((current_price - reference_price) / reference_price) * 100, 0.0)

    def _positive_or_zero(self, value: float | None) -> float:
        if value is None or value <= 0:
            return 0.0
        return value

    def _first_positive(self, *values: float | None) -> float:
        for value in values:
            if value is not None and value > 0:
                return value
        return 0.0

    def _optional_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
