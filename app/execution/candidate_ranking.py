import hashlib
from typing import TYPE_CHECKING, Any

from app.execution.scoring.market_context_scorer import score_market_context
from app.execution.scoring.multi_timeframe_scorer import score_multi_timeframe
from app.execution.scoring.sell_signal_scorer import SellSignalScorer
from app.execution.scoring.signal_scorer import (
    directional_score_breakdown,
    float_metadata,
)
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal
from app.utils.commons import spread_percent


if TYPE_CHECKING:
    from app.instruments.models import EntryDecisionConfig
    from app.market.market_context import CandidateMarketContext
    from app.market.multi_timeframe import MultiTimeframeContext


_DEFAULT_SELL_SCORER = SellSignalScorer()


def build_trade_candidate(
    symbol: str,
    snapshot: MarketSnapshot,
    candle: Candle,
    signal: Signal,
    session_key: str = '',
    *,
    run_id: str = '',
    origin_candidate_id: str | None = None,
    pending_entry_id: str | None = None,
    market_context: 'CandidateMarketContext | None' = None,
    multi_timeframe_context: 'MultiTimeframeContext | None' = None,
    entry_decision_config: 'EntryDecisionConfig | None' = None,
) -> TradeCandidate:
    score_breakdown = _score_breakdown(
        snapshot=snapshot,
        candle=candle,
        signal=signal,
    )
    directional_score = score_breakdown.final_score
    context_score = score_market_context(
        context=market_context,
        side=signal.action,
    )
    multi_timeframe_score = score_multi_timeframe(
        context=multi_timeframe_context,
        side=signal.action,
    )
    exhaustion = score_breakdown.exhaustion
    entry_quality_metadata = _entry_quality_metadata(score_breakdown)
    sell_score_metadata = score_breakdown.score_metadata.get('sell_score', {})
    candidate_id = _candidate_id(
        run_id=run_id,
        symbol=symbol,
        signal=signal,
        session_key=session_key,
        candle=candle,
        pending_entry_id=pending_entry_id,
    )

    return TradeCandidate(
        symbol=symbol,
        snapshot=snapshot,
        candle=candle,
        signal=signal,
        rank_reason=_rank_reason(
            snapshot=snapshot,
            signal=signal,
            base_score=score_breakdown.base_score,
            directional_score=directional_score,
            entry_quality_metadata=entry_quality_metadata,
            sell_score_metadata=sell_score_metadata,
            market_context_score=context_score.score,
            market_context_components=context_score.components,
            multi_timeframe_score=multi_timeframe_score.score,
            multi_timeframe_components=multi_timeframe_score.components,
        ),
        session_key=session_key,
        base_score=round(score_breakdown.base_score, 4),
        directional_score=round(directional_score, 4),
        exhaustion_penalty=exhaustion.exhaustion_penalty,
        late_entry_risk=exhaustion.late_entry_risk,
        late_entry_severity=exhaustion.late_entry_severity,
        entry_quality_metadata=entry_quality_metadata,
        sell_score_metadata=sell_score_metadata,
        sell_specific_penalty=score_breakdown.sell_specific_penalty,
        market_context_score=context_score.score,
        market_context_components=context_score.components,
        market_context_score_metadata={
            **context_score.diagnostics,
            'model_version': context_score.model_version,
        },
        multi_timeframe_score=multi_timeframe_score.score,
        multi_timeframe_components=multi_timeframe_score.components,
        multi_timeframe_score_metadata={
            **multi_timeframe_score.diagnostics,
            'model_version': multi_timeframe_score.model_version,
        },
        candidate_id=candidate_id,
        origin_candidate_id=origin_candidate_id or candidate_id,
        pending_entry_id=pending_entry_id,
        market_context=market_context,
        multi_timeframe_context=multi_timeframe_context,
        entry_decision_config=entry_decision_config,
    )


def rank_trade_candidates(candidates: list[TradeCandidate]) -> list[TradeCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            -_ranking_value(candidate.probability_score),
            -candidate.directional_score,
            candidate.candidate_id,
        ),
    )


def _candidate_id(
    *,
    run_id: str,
    symbol: str,
    signal: Signal,
    session_key: str,
    candle: Candle,
    pending_entry_id: str | None,
) -> str:
    metadata = signal.metadata or {}
    level_key = 'range_high' if signal.action == 'BUY' else 'range_low'
    level = metadata.get(level_key, '')
    raw = '|'.join(
        (
            run_id,
            symbol.strip().upper(),
            signal.action.strip().upper(),
            session_key,
            candle.closed_at.isoformat(),
            str(level),
            pending_entry_id or '',
        )
    )
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _score_breakdown(
    *,
    snapshot: MarketSnapshot,
    candle: Candle,
    signal: Signal,
):
    metadata = signal.metadata or {}
    close_position_percent = float_metadata(metadata, 'close_position_percent')
    close_quality = (
        100 - close_position_percent
        if signal.action == 'SELL'
        else close_position_percent
    )
    if signal.action == 'SELL':
        return _DEFAULT_SELL_SCORER.score_breakdown(
            snapshot=snapshot,
            candle=candle,
            signal=signal,
        )
    return directional_score_breakdown(
        snapshot=snapshot,
        candle=candle,
        signal=signal,
        close_quality=close_quality,
    )


def _entry_quality_metadata(score_breakdown) -> dict[str, Any]:
    exhaustion = score_breakdown.exhaustion
    return {
        'setup_quality_bonus': score_breakdown.score_metadata.get(
            'setup_quality_bonus',
            0.0,
        ),
        'late_entry_risk': exhaustion.late_entry_risk,
        'exhaustion_penalty': exhaustion.exhaustion_penalty,
        'late_entry_severity': exhaustion.late_entry_severity,
        'move_extension_percent': exhaustion.move_extension_percent,
        'extension_atr_ratio': exhaustion.extension_atr_ratio,
        'distance_to_recent_high_percent': (
            exhaustion.distance_to_recent_high_percent
        ),
        'distance_to_recent_low_percent': (
            exhaustion.distance_to_recent_low_percent
        ),
        'momentum_acceleration_percent': (
            exhaustion.momentum_acceleration_percent
        ),
        'momentum_deceleration_detected': (
            exhaustion.momentum_deceleration_detected
        ),
        'remaining_move_quality': exhaustion.remaining_move_quality,
        'reason_exhaustion_components': (
            exhaustion.reason_exhaustion_components
        ),
    }


def _rank_reason(
    snapshot: MarketSnapshot,
    signal: Signal,
    base_score: float,
    directional_score: float,
    entry_quality_metadata: dict[str, Any],
    sell_score_metadata: dict[str, Any],
    market_context_score: float,
    market_context_components: dict[str, float],
    multi_timeframe_score: float,
    multi_timeframe_components: dict[str, float],
) -> str:
    metadata = signal.metadata or {}
    sell_reason = _sell_rank_reason(sell_score_metadata)
    return (
        f'base_score={round(base_score, 4)} | '
        f'directional_score={round(directional_score, 4)} | '
        f'market_context_score={round(market_context_score, 4)} | '
        f'market_context_components={market_context_components} | '
        f'multi_timeframe_score={round(multi_timeframe_score, 4)} | '
        f'multi_timeframe_components={multi_timeframe_components} | '
        f'setup_quality={signal.setup_quality} | '
        f'setup_quality_bonus={entry_quality_metadata["setup_quality_bonus"]} | '
        f'exhaustion_penalty={entry_quality_metadata["exhaustion_penalty"]} | '
        f'late_entry_risk={entry_quality_metadata["late_entry_risk"]} | '
        f'late_entry_severity={entry_quality_metadata["late_entry_severity"]} | '
        f'{sell_reason}'
        f'remaining_move_quality={entry_quality_metadata["remaining_move_quality"]} | '
        f'exhaustion_components={entry_quality_metadata["reason_exhaustion_components"]} | '
        f'action={signal.action} | '
        f'session_move={float_metadata(metadata, "session_move_percent")} | '
        f'trend_strength={float_metadata(metadata, "trend_strength_percent")} | '
        f'breakout={float_metadata(metadata, "breakout_percent")} | '
        f'breakdown={float_metadata(metadata, "breakdown_percent")} | '
        f'candle_range={float_metadata(metadata, "candle_range_percent")} | '
        f'close_position={float_metadata(metadata, "close_position_percent")} | '
        f'spread={round(spread_percent(snapshot), 4)}'
    )


def _ranking_value(value: float | None) -> float:
    return float('-inf') if value is None else float(value)


def _sell_rank_reason(sell_score_metadata: dict[str, Any]) -> str:
    if not sell_score_metadata:
        return ''
    return (
        f'sell_specific_penalty={sell_score_metadata["sell_specific_penalty"]} | '
        f'sell_components={sell_score_metadata["sell_score_components"]} | '
        f'breakdown_strength={sell_score_metadata["breakdown_strength"]} | '
        f'short_snapshot_momentum={sell_score_metadata["short_snapshot_momentum"]} | '
        f'sell_close_quality={sell_score_metadata["sell_close_quality"]} | '
    )
