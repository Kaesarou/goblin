import logging

from app.execution.candidate_ranking import build_trade_candidate
from app.execution.trade_candidate import TradeCandidate
from app.journal.jsonl_journal import JsonlJournal
from app.market.market_context import CandidateMarketContext, MarketContextService
from app.market.models import Candle, MarketSnapshot
from app.market.multi_timeframe import (
    MultiTimeframeContext,
    MultiTimeframeService,
    MultiTimeframeUpdate,
)
from app.market.session_rules import TradingSessionDecision
from app.market.timeframes import BASE_TIMEFRAME, BarCompleteness
from app.risk.models import TradePlan
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.runtime.entry_horizon import evaluate_entry_horizon
from app.runtime.pending_entry import PendingEntryManager
from app.runtime.pending_entry_flow import advance_pending_entry
from app.strategies.strategy import TrendStrategy

logger = logging.getLogger(__name__)


def process_closed_candle(
    *,
    symbol: str,
    snapshot: MarketSnapshot,
    closed_candle: Candle,
    strategy: TrendStrategy,
    risk_manager: RiskManager,
    trade_journal: JsonlJournal,
    candle_journal: JsonlJournal,
    session_decision: TradingSessionDecision | None = None,
    loop_id: int | None = None,
    pending_entry_manager: PendingEntryManager | None = None,
    cooldown_guard: TradeCooldownGuard | None = None,
    market_context_service: MarketContextService | None = None,
    multi_timeframe_service: MultiTimeframeService | None = None,
    run_id: str = '',
) -> TradeCandidate | None:
    if multi_timeframe_service is not None and session_decision is not None:
        update = multi_timeframe_service.on_base_candle(
            symbol=symbol,
            candle=closed_candle,
            session_decision=session_decision,
        )
        _write_multi_timeframe_update(
            candle_journal=candle_journal,
            symbol=symbol,
            update=update,
            loop_id=loop_id,
        )

    signal = strategy.on_candle(closed_candle)
    side = _side_for_signal(
        symbol=symbol,
        signal_action=signal.action,
        pending_entry_manager=pending_entry_manager,
    )
    market_context = _market_context_for_side(
        symbol=symbol,
        side=side,
        snapshot=snapshot,
        market_context_service=market_context_service,
    )
    multi_timeframe_context = _multi_timeframe_context_for_side(
        symbol=symbol,
        side=side,
        closed_candle=closed_candle,
        session_decision=session_decision,
        multi_timeframe_service=multi_timeframe_service,
    )
    if multi_timeframe_context is not None:
        trade_journal.write(
            'multi_timeframe_context_built',
            {
                'symbol': symbol,
                'side': side,
                'multi_timeframe_context': multi_timeframe_context,
                'loop_id': loop_id,
            },
        )
    entry_decision_config = _entry_decision_config(risk_manager, symbol)

    if pending_entry_manager is not None:
        confirmed_candidate = advance_pending_entry(
            symbol=symbol,
            snapshot=snapshot,
            candle=closed_candle,
            signal=signal,
            session_decision=session_decision,
            risk_manager=risk_manager,
            pending_manager=pending_entry_manager,
            cooldown_guard=cooldown_guard,
            trade_journal=trade_journal,
            market_context=market_context,
            multi_timeframe_context=multi_timeframe_context,
            entry_decision_config=entry_decision_config,
            run_id=run_id,
        )
        if confirmed_candidate is not None:
            return confirmed_candidate

    if signal.action == 'HOLD':
        logger.debug(
            'Strategy hold | symbol=%s | reason=%s | candle_close=%s',
            symbol,
            signal.reason,
            closed_candle.close,
        )
        return _write_rejected_decision(
            symbol=symbol,
            snapshot=snapshot,
            closed_candle=closed_candle,
            signal=signal,
            reason=signal.reason,
            risk_manager=risk_manager,
            trade_journal=trade_journal,
            session_decision=session_decision,
            loop_id=loop_id,
        )

    logger.info(
        'Strategy candidate signal | symbol=%s | action=%s | '
        'setup_quality=%s | reason=%s | candle_close=%s',
        symbol,
        signal.action,
        signal.setup_quality,
        signal.reason,
        closed_candle.close,
    )
    if session_decision is None or session_decision.session_key is None:
        return _write_rejected_decision(
            symbol=symbol,
            snapshot=snapshot,
            closed_candle=closed_candle,
            signal=signal,
            reason='missing_trading_session',
            risk_manager=risk_manager,
            trade_journal=trade_journal,
            session_decision=session_decision,
            loop_id=loop_id,
        )
    if not session_decision.new_entries_allowed:
        return _write_rejected_decision(
            symbol=symbol,
            snapshot=snapshot,
            closed_candle=closed_candle,
            signal=signal,
            reason=session_decision.reason,
            risk_manager=risk_manager,
            trade_journal=trade_journal,
            session_decision=session_decision,
            loop_id=loop_id,
        )

    risk_profile = risk_manager.risk_profile_for(symbol)
    horizon = evaluate_entry_horizon(
        risk_profile=risk_profile,
        side=signal.action,
        session_decision=session_decision,
    )
    if not horizon.allowed:
        trade_journal.write(
            'entry_horizon_rejected',
            {
                'symbol': symbol,
                'side': signal.action,
                'reason': horizon.reason,
                'required_minutes': horizon.required_minutes,
                'available_minutes': horizon.available_minutes,
                'profile_key': horizon.profile_key,
                'source': 'new_signal',
                'signal': signal,
                'snapshot': snapshot,
                'candle': closed_candle,
            },
        )
        return _write_rejected_decision(
            symbol=symbol,
            snapshot=snapshot,
            closed_candle=closed_candle,
            signal=signal,
            reason=horizon.reason,
            risk_manager=risk_manager,
            trade_journal=trade_journal,
            session_decision=session_decision,
            loop_id=loop_id,
        )

    candidate = build_trade_candidate(
        symbol=symbol,
        snapshot=snapshot,
        candle=closed_candle,
        signal=signal,
        session_key=session_decision.session_key,
        run_id=run_id,
        market_context=market_context,
        multi_timeframe_context=multi_timeframe_context,
        entry_decision_config=entry_decision_config,
    )
    trade_journal.write(
        'candidate_detected',
        {
            'candidate_id': candidate.candidate_id,
            'symbol': symbol,
            'snapshot': snapshot,
            'candle': closed_candle,
            'signal': signal,
            'candidate': candidate,
            'market_context': market_context,
            'multi_timeframe_context': multi_timeframe_context,
            'session_decision': session_decision,
            'instrument_profile': risk_manager.instrument_profile_for(symbol),
            'risk_profile': risk_profile,
            'loop_id': loop_id,
        },
    )
    logger.info(
        'Trade candidate detected | candidate_id=%s | symbol=%s | '
        'action=%s | directional_score=%s | reason=%s',
        candidate.candidate_id,
        symbol,
        signal.action,
        candidate.directional_score,
        candidate.rank_reason,
    )
    return candidate


def _write_multi_timeframe_update(
    *,
    candle_journal: JsonlJournal,
    symbol: str,
    update: MultiTimeframeUpdate,
    loop_id: int | None,
) -> None:
    for gap in update.gaps:
        candle_journal.write(
            'candle_gap_detected',
            {'symbol': symbol, 'gap': gap, 'loop_id': loop_id},
        )
    for bar in update.closed_bars:
        if bar.timeframe == BASE_TIMEFRAME:
            continue
        event_type = 'timeframe_bar_closed'
        if bar.completeness == BarCompleteness.INCOMPLETE:
            event_type = 'timeframe_bar_incomplete'
        elif bar.completeness == BarCompleteness.PARTIAL:
            event_type = 'timeframe_bar_partial'
        candle_journal.write(
            event_type,
            {
                'symbol': symbol,
                'timeframe': bar.timeframe.name.lower(),
                'timeframe_bar': bar,
                'loop_id': loop_id,
            },
        )


def _entry_decision_config(risk_manager: RiskManager, symbol: str):
    registry = getattr(risk_manager, 'instrument_registry', None)
    if registry is None:
        return None
    return registry.config_for(symbol).entry_decision


def _side_for_signal(
    *,
    symbol: str,
    signal_action: str,
    pending_entry_manager: PendingEntryManager | None,
) -> str | None:
    if signal_action in {'BUY', 'SELL'}:
        return signal_action
    if pending_entry_manager is None:
        return None
    pending = next(
        (
            item
            for item in pending_entry_manager.snapshot()
            if item.symbol == symbol
        ),
        None,
    )
    return pending.side if pending is not None else None


def _market_context_for_side(
    *,
    symbol: str,
    side: str | None,
    snapshot: MarketSnapshot,
    market_context_service: MarketContextService | None,
) -> CandidateMarketContext | None:
    if market_context_service is None or side is None:
        return None
    return market_context_service.build_candidate_context(
        symbol=symbol,
        side=side,
        as_of=snapshot.timestamp,
    )


def _multi_timeframe_context_for_side(
    *,
    symbol: str,
    side: str | None,
    closed_candle: Candle,
    session_decision: TradingSessionDecision | None,
    multi_timeframe_service: MultiTimeframeService | None,
) -> MultiTimeframeContext | None:
    if (
        multi_timeframe_service is None
        or session_decision is None
        or side is None
    ):
        return None
    return multi_timeframe_service.build_context(
        symbol=symbol,
        side=side,
        as_of=closed_candle.closed_at,
        session_decision=session_decision,
    )


def _write_rejected_decision(
    *,
    symbol: str,
    snapshot: MarketSnapshot,
    closed_candle: Candle,
    signal,
    reason: str,
    risk_manager: RiskManager,
    session_decision: TradingSessionDecision | None,
    trade_journal: JsonlJournal,
    loop_id: int | None = None,
) -> None:
    plan = TradePlan(
        approved=False,
        reason=reason,
        symbol=symbol,
        side=signal.action,
    )
    trade_journal.write(
        'decision',
        {
            'symbol': symbol,
            'snapshot': snapshot,
            'candle': closed_candle,
            'signal': signal,
            'equity': None,
            'trade_plan': plan,
            'session_decision': session_decision,
            'instrument_profile': risk_manager.instrument_profile_for(symbol),
            'risk_profile': risk_manager.risk_profile_for(symbol),
            'loop_id': loop_id,
        },
    )
    logger.debug('Trade rejected | symbol=%s | reason=%s', symbol, plan.reason)
    return None
