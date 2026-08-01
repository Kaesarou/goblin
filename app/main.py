import logging
from datetime import datetime, timezone

from app.brokers.etoro.order_payload_builder import (
    SELLSHORT_SAFETY_SL_BUFFER_PERCENT,
)
from app.config.settings import Settings, get_settings
from app.execution.candidate_economics import CandidateEconomicsEstimator
from app.execution.position_tracker import PositionTracker
from app.execution.trade_executor import TradeExecutor
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.analysis_journal import AnalysisJournal
from app.journal.jsonl_journal import JsonlJournal
from app.journal.raw_data_journal import RawDataJournal
from app.journal.run_manifest import (
    build_run_id,
    build_run_manifest,
    finalize_run_manifest,
    write_run_manifest,
)
from app.journal.run_paths import build_run_journal_paths, rotate_run_journals
from app.market.data_quality import MarketDataValidator
from app.market.market_context import MarketContextService
from app.market.session_timeframe_service import FullSessionMultiTimeframeService
from app.market_data.candle_stream import QualityAwareCandleBuilder
from app.market_data.models import MARKET_DATA_MODEL_VERSION
from app.persistence.pending_close_store import PendingCloseStore
from app.persistence.position_store import PositionStore
from app.persistence.trade_cooldown_store import TradeCooldownStore
from app.risk.position_sizing import FixedPercentPositionSizing
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.runtime.factories import build_runtime_clients
from app.runtime.market_data_runtime import EventDrivenMarketRuntime
from app.runtime.pending_entry import PendingEntryManager
from app.runtime.runtime_heartbeat import RuntimeHeartbeat
from app.runtime.runtime_policy import (
    CANDLE_CLOCK_GRACE_SECONDS,
    CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS,
    CANDLE_ORDERING_DROP_DEGRADE_COUNT,
    CANDLE_ORDERING_DROP_DEGRADE_RATIO,
    DECISION_WINDOW_GRACE_SECONDS,
    ETORO_INSTRUMENT_RESOLUTION_MIN_INTERVAL_SECONDS,
    JOURNAL_PARTIAL_SUMMARY_INTERVAL_MINUTES,
    JOURNAL_WRITE_PARTIAL_SUMMARY,
    MARKET_DATA_QUEUE_CAPACITY,
    POSITION_FALLBACK_INTERVAL_SECONDS,
    POSITION_RECONCILIATION_GRACE_SECONDS,
    POSITION_RECONCILIATION_MISS_INTERVAL_SECONDS,
    POSITION_RECONCILIATION_REQUIRED_MISSES,
    REST_CONTROL_ANOMALY_PERCENT,
    REST_CONTROL_INTERVAL_SECONDS,
    UNKNOWN_ORDER_LOOKUP_INTERVAL_SECONDS,
    UNKNOWN_ORDER_MAX_AGE_MINUTES,
    WS_GLOBAL_SILENCE_SECONDS,
    WS_POSITION_SILENCE_SECONDS,
)
from app.runtime.startup_position_restore import restore_persisted_positions_batched
from app.runtime.trading_session_window import (
    TradingSessionState,
    trading_session_service_from_settings,
)
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from app.strategies.strategy import TrendStrategy
from app.utils.logging import configure_logging

logger = logging.getLogger(__name__)


def is_broker_authorization_error(exc: Exception) -> bool:
    response = getattr(exc, 'response', None)
    return getattr(response, 'status_code', None) in (401, 403)


def build_risk_manager(
    settings: Settings,
    instrument_registry: InstrumentRegistry,
) -> RiskManager:
    return RiskManager(
        settings=settings,
        position_sizing_strategy=FixedPercentPositionSizing(),
        instrument_registry=instrument_registry,
    )


def build_candidate_economics_estimator(
    instrument_registry: InstrumentRegistry,
) -> CandidateEconomicsEstimator:
    return CandidateEconomicsEstimator(
        position_sizing_strategy=FixedPercentPositionSizing(),
        instrument_registry=instrument_registry,
    )


def build_candle_builders(
    symbols: list[str],
) -> dict[str, QualityAwareCandleBuilder]:
    return {symbol: QualityAwareCandleBuilder() for symbol in symbols}


def build_strategies(
    symbols: list[str],
    instrument_registry: InstrumentRegistry,
) -> dict[str, TrendStrategy]:
    return {
        symbol: TrendStrategy(instrument_registry.config_for(symbol).trend)
        for symbol in symbols
    }


def build_market_data_manifest() -> dict[str, object]:
    return {
        'mode': 'websocket',
        'queue_capacity': MARKET_DATA_QUEUE_CAPACITY,
        'position_silence_seconds': WS_POSITION_SILENCE_SECONDS,
        'global_silence_seconds': WS_GLOBAL_SILENCE_SECONDS,
        'rest_control_interval_seconds': REST_CONTROL_INTERVAL_SECONDS,
        'rest_control_anomaly_percent': REST_CONTROL_ANOMALY_PERCENT,
        'position_fallback_interval_seconds': (
            POSITION_FALLBACK_INTERVAL_SECONDS
        ),
        'decision_window_grace_seconds': DECISION_WINDOW_GRACE_SECONDS,
        'candle_clock_grace_seconds': CANDLE_CLOCK_GRACE_SECONDS,
        'candle_max_carry_forward_age_seconds': (
            CANDLE_MAX_CARRY_FORWARD_AGE_SECONDS
        ),
        'candle_ordering_drop_degrade_count': (
            CANDLE_ORDERING_DROP_DEGRADE_COUNT
        ),
        'candle_ordering_drop_degrade_ratio': (
            CANDLE_ORDERING_DROP_DEGRADE_RATIO
        ),
        'position_reconciliation_grace_seconds': (
            POSITION_RECONCILIATION_GRACE_SECONDS
        ),
        'position_reconciliation_required_misses': (
            POSITION_RECONCILIATION_REQUIRED_MISSES
        ),
        'position_reconciliation_miss_interval_seconds': (
            POSITION_RECONCILIATION_MISS_INTERVAL_SECONDS
        ),
        'unknown_order_lookup_interval_seconds': (
            UNKNOWN_ORDER_LOOKUP_INTERVAL_SECONDS
        ),
        'unknown_order_max_age_minutes': UNKNOWN_ORDER_MAX_AGE_MINUTES,
        'instrument_resolution_min_interval_seconds': (
            ETORO_INSTRUMENT_RESOLUTION_MIN_INTERVAL_SECONDS
        ),
        'sellshort_safety_sl_buffer_percent': (
            SELLSHORT_SAFETY_SL_BUFFER_PERCENT
        ),
    }


def _build_analysis_journal(
    *,
    settings: Settings,
    run_paths,
    run_id: str,
    profile: str,
) -> AnalysisJournal:
    debug_enabled = settings.journal_detail_level in {'debug', 'full'}
    return AnalysisJournal(
        trade_journal=JsonlJournal(
            str(run_paths.trades),
            run_id=run_id,
            stream_name='trades',
        ),
        errors_journal=JsonlJournal(
            str(run_paths.errors),
            run_id=run_id,
            stream_name='errors',
        ),
        debug_decisions_journal=(
            JsonlJournal(
                str(run_paths.debug_decisions),
                run_id=run_id,
                stream_name='debug_decisions',
            )
            if debug_enabled
            else None
        ),
        summary_path=str(run_paths.summary),
        partial_summary_path=str(run_paths.partial_summary),
        detail_level=settings.journal_detail_level,
        write_partial_summary=JOURNAL_WRITE_PARTIAL_SUMMARY,
        partial_summary_interval_minutes=(
            JOURNAL_PARTIAL_SUMMARY_INTERVAL_MINUTES
        ),
        run_id=run_id,
        strategy='TrendStrategy',
        profile=profile,
    )


def main() -> None:
    started_at = datetime.now(timezone.utc)
    run_id = build_run_id(started_at)
    run_status = 'running'
    settings = get_settings()
    run_paths = build_run_journal_paths(
        journal_path=settings.journal_path,
        run_id=run_id,
    )
    removed_runs = rotate_run_journals(
        runs_root=run_paths.root.parent,
        max_runs=settings.journal_max_runs,
        current_run_id=run_id,
    )
    archived_manifest_path = str(run_paths.manifest)
    archived_summary_path = str(run_paths.summary)
    configure_logging(
        level=settings.log_level,
        log_file_path=settings.app_log_path,
    )
    symbols = settings.watchlist_symbols()
    strategy_profile = BalancedStrategyConfig(
        breakeven_profile_name=settings.breakeven_profile,
    )
    instrument_registry = InstrumentRegistry(
        settings,
        instrument_configs=strategy_profile.instrument_configs,
    )
    instrument_registry.validate_supported_symbols(symbols)

    manifest = build_run_manifest(
        settings=settings,
        strategy_profile=strategy_profile,
        instrument_registry=instrument_registry,
        symbols=symbols,
        run_id=run_id,
        started_at=started_at,
        manifest_path=archived_manifest_path,
        summary_path=archived_summary_path,
    )
    manifest['models']['market_data'] = MARKET_DATA_MODEL_VERSION
    manifest['runtime']['market_data'] = build_market_data_manifest()
    manifest['runtime']['journals'] = {
        'run_root': str(run_paths.root),
        'compressed': True,
        'detail_level': settings.journal_detail_level,
        'write_partial_summary': JOURNAL_WRITE_PARTIAL_SUMMARY,
        'partial_summary_interval_minutes': (
            JOURNAL_PARTIAL_SUMMARY_INTERVAL_MINUTES
        ),
        'max_runs': settings.journal_max_runs,
        'removed_runs': list(removed_runs),
    }
    write_run_manifest(archived_manifest_path, manifest)
    write_run_manifest(settings.run_manifest_path, manifest)

    clients = build_runtime_clients(settings)
    strategies = build_strategies(symbols, instrument_registry)
    candle_builders = build_candle_builders(symbols)
    trading_session_service = trading_session_service_from_settings(settings)
    trading_session_state = TradingSessionState()
    risk_manager = build_risk_manager(settings, instrument_registry)
    candidate_economics_estimator = build_candidate_economics_estimator(
        instrument_registry
    )
    executor = TradeExecutor(clients.execution_broker)
    position_tracker = PositionTracker()
    position_store = PositionStore(settings.position_store_path)
    pending_close_store = PendingCloseStore(settings.position_store_path)
    cooldown_store = TradeCooldownStore(settings.position_store_path)
    cooldown_guard = TradeCooldownGuard(cooldown_store)
    pending_entry_manager = PendingEntryManager()
    market_data_validator = MarketDataValidator()
    market_context_service = MarketContextService(
        instrument_registry=instrument_registry,
        benchmark_symbols=settings.benchmark_symbols_by_asset_class(),
    )
    multi_timeframe_service = FullSessionMultiTimeframeService(
        {
            symbol: instrument_registry.config_for(symbol).multi_timeframe
            for symbol in symbols
        }
    )
    trade_journal = _build_analysis_journal(
        settings=settings,
        run_paths=run_paths,
        run_id=run_id,
        profile=strategy_profile.name,
    )
    market_journal = RawDataJournal(
        JsonlJournal(
            str(run_paths.market),
            run_id=run_id,
            stream_name='market',
        ),
        trade_journal.record_raw_event,
    )
    candle_journal = RawDataJournal(
        JsonlJournal(
            str(run_paths.candles),
            run_id=run_id,
            stream_name='candles',
        ),
        trade_journal.record_raw_event,
    )
    heartbeat = RuntimeHeartbeat(settings.runtime_heartbeat_minutes)
    runtime = EventDrivenMarketRuntime(
        settings=settings,
        symbols=symbols,
        run_id=run_id,
        strategy_profile=strategy_profile,
        instrument_registry=instrument_registry,
        execution_broker=clients.execution_broker,
        rest_market_data=clients.rest_market_data,
        live_market_data=clients.live_market_data,
        strategies=strategies,
        candle_builders=candle_builders,
        trading_session_service=trading_session_service,
        trading_session_state=trading_session_state,
        risk_manager=risk_manager,
        candidate_economics_estimator=candidate_economics_estimator,
        executor=executor,
        position_tracker=position_tracker,
        position_store=position_store,
        pending_close_store=pending_close_store,
        cooldown_store=cooldown_store,
        cooldown_guard=cooldown_guard,
        pending_entry_manager=pending_entry_manager,
        market_data_validator=market_data_validator,
        market_context_service=market_context_service,
        multi_timeframe_service=multi_timeframe_service,
        trade_journal=trade_journal,
        market_journal=market_journal,
        candle_journal=candle_journal,
        heartbeat=heartbeat,
        is_broker_authorization_error=is_broker_authorization_error,
    )
    trade_journal.write(
        'runtime_started',
        {
            'run_id': run_id,
            'symbols': symbols,
            'strategy_profile': strategy_profile.name,
            'breakeven_profile': strategy_profile.breakeven_profile_name,
            'market_data_mode': 'websocket',
            'execution_mode': settings.broker,
            'run_journal_root': str(run_paths.root),
            'rotated_run_ids': list(removed_runs),
        },
    )
    logger.info(
        'Starting Goblin! | run_id=%s | broker=%s | market_data=websocket | '
        'strategy_profile=%s | watchlist=%s | run_logs=%s',
        run_id,
        settings.broker,
        strategy_profile.name,
        symbols,
        run_paths.root,
    )

    try:
        pending_closes = pending_close_store.load_all()
        startup_open_states = restore_persisted_positions_batched(
            position_store=position_store,
            position_tracker=position_tracker,
            risk_manager=risk_manager,
            broker=clients.execution_broker,
            trade_journal=trade_journal,
            is_broker_authorization_error=is_broker_authorization_error,
        )
        runtime.broker_operations.restore_pending_closes(
            pending_closes,
            open_states=startup_open_states,
            observed_at=datetime.now(timezone.utc),
        )
        run_status = runtime.run()
    except Exception as exc:
        run_status = 'failed'
        if is_broker_authorization_error(exc):
            logger.critical('Broker authorization failed. Stopping Goblin.')
            trade_journal.write(
                'broker_authorization_error',
                {'stage': 'event_runtime', 'message': str(exc)},
            )
        else:
            logger.exception('Goblin runtime failed: %s', exc)
            trade_journal.write(
                'error',
                {'stage': 'event_runtime', 'message': str(exc)},
            )
        raise
    finally:
        if run_status == 'running':
            run_status = 'completed'
        for symbol in symbols:
            runtime._write_partial_timeframe_bars(
                symbol,
                multi_timeframe_service.reset_symbol(symbol),
            )
        trade_journal.write(
            'runtime_stopped',
            {
                'run_id': run_id,
                'status': run_status,
                'loop_id': runtime.loop_id,
            },
        )
        summary = trade_journal.finalize()
        summary.setdefault('market_data', {})['model_version'] = (
            MARKET_DATA_MODEL_VERSION
        )
        write_run_manifest(settings.daily_summary_path, summary)
        write_run_manifest(archived_summary_path, summary)
        finalize_run_manifest(
            archived_manifest_path,
            status=run_status,
            summary=summary,
        )
        finalize_run_manifest(
            settings.run_manifest_path,
            status=run_status,
            summary=summary,
        )


if __name__ == '__main__':
    main()
