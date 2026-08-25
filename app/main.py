import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import Settings, get_settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.journal.analysis_journal import AnalysisJournal
from app.journal.jsonl_journal import JsonlJournal
from app.journal.raw_data_journal import RawDataJournal
from app.journal.run_paths import build_run_journal_paths, rotate_run_journals
from app.market.data_quality import MarketDataValidator
from app.market.market_context import MarketContextService
from app.market.session_timeframe_service import FullSessionMultiTimeframeService
from app.market_data.candle_stream import QualityAwareCandleBuilder
from app.market_data.models import MARKET_DATA_MODEL_VERSION
from app.research.payload_schema_observer import EtoroPayloadSchemaObserver
from app.research.pipeline import SideNeutralResearchPipeline
from app.research.summary import empty_research_summary, write_research_summary
from app.runtime.factories import build_runtime_clients
from app.runtime.runtime_heartbeat import RuntimeHeartbeat
from app.runtime.runtime_policy import (
    JOURNAL_PARTIAL_SUMMARY_INTERVAL_MINUTES,
    JOURNAL_WRITE_PARTIAL_SUMMARY,
)
from app.runtime.trading_session_window import trading_session_service_from_settings
from app.utils.logging import configure_logging
from app.v3.config import RecoverabilityConfig, etoro5_research_config
from app.v3.economics import BrokerCostSchedule, EconomicsModel
from app.v3.features import OnlineFeatureEngine
from app.v3.manifest import (
    build_run_id,
    build_v3_run_manifest,
    finalize_run_manifest,
    write_run_manifest,
)
from app.v3.persistence import InventoryEvent, InventoryEventStore
from app.v3.planner import InventoryPlanner
from app.v3.recoverability import RecoverabilityScorer
from app.v3.risk import InventoryRiskPolicy
from app.v3.runtime import GoblinV3Runtime
from app.v3.state_store import V3RuntimeStateStore

logger = logging.getLogger(__name__)

V3_PROFILE = "RR5_ETORO5_PROSPECTIVE_VALIDATION"
ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST = 1.0


def build_candle_builders(
    symbols: list[str],
) -> dict[str, QualityAwareCandleBuilder]:
    return {symbol: QualityAwareCandleBuilder() for symbol in symbols}


def _build_analysis_journal(
    *,
    settings: Settings,
    run_paths,
    run_id: str,
    strategy: str,
    profile: str,
) -> AnalysisJournal:
    debug_enabled = settings.journal_detail_level in {"debug", "full"}
    return AnalysisJournal(
        trade_journal=JsonlJournal(
            str(run_paths.trades),
            run_id=run_id,
            stream_name="trades",
        ),
        errors_journal=JsonlJournal(
            str(run_paths.errors),
            run_id=run_id,
            stream_name="errors",
        ),
        debug_decisions_journal=(
            JsonlJournal(
                str(run_paths.debug_decisions),
                run_id=run_id,
                stream_name="debug_decisions",
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
        strategy=strategy,
        profile=profile,
    )


def _write_disabled_research_summary(
    *,
    path: Path,
    run_id: str,
    updated_at: datetime,
) -> None:
    try:
        write_research_summary(
            path,
            empty_research_summary(
                run_id=run_id,
                enabled=False,
                updated_at=updated_at,
            ),
        )
    except Exception:
        logger.exception(
            "Disabled research summary write failed | path=%s",
            path,
        )


def _assert_v3_execution_mode(broker: str) -> None:
    normalized = broker.strip().lower()
    if normalized == "etoro_live":
        raise RuntimeError(
            "Goblin V3 is not prospectively validated for live capital. "
            "Use paper or etoro_demo until an explicit promotion decision."
        )
    if normalized not in {"paper", "etoro_demo"}:
        raise RuntimeError(f"Unsupported V3 broker mode: {broker}")


def _assert_v3_universe(
    symbols: list[str],
    instrument_registry: InstrumentRegistry,
) -> None:
    unsupported = [
        symbol
        for symbol in symbols
        if instrument_registry.resolve(symbol).asset_class
        not in {AssetClass.EQUITY_EU, AssetClass.EQUITY_US}
    ]
    if unsupported:
        raise RuntimeError(
            "Goblin V3 RR5 currently has prospective authority for equity "
            "validation only; unsupported watchlist symbols: "
            + ", ".join(sorted(unsupported))
        )


def _inventory_event_sink(trade_journal: AnalysisJournal):
    def sink(event: InventoryEvent) -> None:
        trade_journal.write(
            "v3_inventory_event",
            {
                "inventory_event_type": event.event_type,
                "event_id": event.event_id,
                "inventory_id": event.inventory_id,
                "occurred_at": event.occurred_at,
                "strategy_version": event.strategy_version,
                "model_version": event.model_version,
                "payload": event.payload,
            },
        )

    return sink


def _build_research_pipeline(
    *,
    settings: Settings,
    symbols: list[str],
    instrument_registry: InstrumentRegistry,
    market_context_service: MarketContextService,
    multi_timeframe_service: FullSessionMultiTimeframeService,
    run_paths,
    run_id: str,
    started_at: datetime,
    latest_payload_schema_path: Path,
):
    if not settings.research_enabled:
        _write_disabled_research_summary(
            path=run_paths.research_summary,
            run_id=run_id,
            updated_at=started_at,
        )
        return None

    research_symbols = {}
    asset_class_by_symbol = {}
    for symbol in symbols:
        asset_class = instrument_registry.resolve(symbol).asset_class
        asset_class_by_symbol[symbol] = asset_class
        if asset_class in {AssetClass.EQUITY_EU, AssetClass.EQUITY_US}:
            research_symbols[symbol] = asset_class
    for asset_class, benchmark_symbols in (
        settings.benchmark_symbols_by_asset_class().items()
    ):
        for benchmark_symbol in benchmark_symbols:
            asset_class_by_symbol.setdefault(benchmark_symbol, asset_class)

    pipeline = SideNeutralResearchPipeline(
        research_symbols=research_symbols,
        asset_class_by_symbol=asset_class_by_symbol,
        journal=JsonlJournal(
            str(run_paths.research),
            run_id=run_id,
            stream_name="research",
            compact=True,
        ),
        payload_schema_observer=EtoroPayloadSchemaObserver(
            run_id=run_id,
            paths=(
                run_paths.etoro_payload_schema,
                latest_payload_schema_path,
            ),
        ),
        market_context_service=market_context_service,
        multi_timeframe_service=multi_timeframe_service,
        collection_started_at=started_at,
        run_id=run_id,
        summary_path=str(run_paths.research_summary),
    )
    pipeline.flush()
    return pipeline


def main() -> None:
    started_at = datetime.now(timezone.utc)
    run_id = build_run_id(started_at)
    run_status = "running"
    settings = get_settings()
    _assert_v3_execution_mode(settings.broker)

    run_paths = build_run_journal_paths(
        journal_path=settings.journal_path,
        run_id=run_id,
    )
    removed_runs = rotate_run_journals(
        runs_root=run_paths.root.parent,
        max_runs=settings.journal_max_runs,
        current_run_id=run_id,
    )
    configure_logging(
        level=settings.log_level,
        log_file_path=settings.app_log_path,
    )

    symbols = settings.watchlist_symbols()
    instrument_registry = InstrumentRegistry(settings)
    instrument_registry.validate_supported_symbols(symbols)
    _assert_v3_universe(symbols, instrument_registry)

    config = etoro5_research_config()
    config = type(config)(
        strategy=config.strategy,
        risk=config.risk,
        recoverability=RecoverabilityConfig(enabled=False),
        economics=config.economics,
        hedge=config.hedge,
    )
    recoverability_scorer = RecoverabilityScorer.from_default_artifact()
    planner = InventoryPlanner(
        config=config,
        recoverability_scorer=recoverability_scorer,
        risk_policy=InventoryRiskPolicy(config.risk),
        economics_model=EconomicsModel(
            config.economics,
            BrokerCostSchedule(
                fixed_fee_per_fill=ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST,
            ),
        ),
    )

    trade_journal = _build_analysis_journal(
        settings=settings,
        run_paths=run_paths,
        run_id=run_id,
        strategy=config.strategy.name,
        profile=V3_PROFILE,
    )
    market_journal = RawDataJournal(
        JsonlJournal(
            str(run_paths.market),
            run_id=run_id,
            stream_name="market",
        ),
        trade_journal.record_raw_event,
        state_observer=lambda event_type, payload: trade_journal.write(
            event_type,
            payload,
        ),
    )
    candle_journal = RawDataJournal(
        JsonlJournal(
            str(run_paths.candles),
            run_id=run_id,
            stream_name="candles",
        ),
        trade_journal.record_raw_event,
    )

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
    latest_payload_schema_path = (
        Path(settings.journal_path).parent / "etoro_payload_schema.json"
    )
    research_pipeline = _build_research_pipeline(
        settings=settings,
        symbols=symbols,
        instrument_registry=instrument_registry,
        market_context_service=market_context_service,
        multi_timeframe_service=multi_timeframe_service,
        run_paths=run_paths,
        run_id=run_id,
        started_at=started_at,
        latest_payload_schema_path=latest_payload_schema_path,
    )

    manifest = build_v3_run_manifest(
        settings=settings,
        config=config,
        recoverability_scorer=recoverability_scorer,
        instrument_registry=instrument_registry,
        symbols=symbols,
        run_id=run_id,
        started_at=started_at,
        run_paths=run_paths,
        latest_payload_schema_path=latest_payload_schema_path,
        removed_runs=removed_runs,
    )
    write_run_manifest(run_paths.manifest, manifest)
    write_run_manifest(settings.run_manifest_path, manifest)

    clients = build_runtime_clients(
        settings,
        websocket_payload_observer=(
            None
            if research_pipeline is None
            else research_pipeline.observe_websocket_payload
        ),
    )
    event_store = InventoryEventStore(
        settings.position_store_path,
        event_sink=_inventory_event_sink(trade_journal),
    )
    state_store = V3RuntimeStateStore(settings.position_store_path)
    asset_class_by_symbol = {
        symbol: instrument_registry.resolve(symbol).asset_class
        for symbol in symbols
    }
    runtime = GoblinV3Runtime(
        settings=settings,
        symbols=symbols,
        run_id=run_id,
        instrument_registry=instrument_registry,
        execution_broker=clients.execution_broker,
        rest_market_data=clients.rest_market_data,
        live_market_data=clients.live_market_data,
        candle_builders=build_candle_builders(symbols),
        trading_session_service=trading_session_service_from_settings(settings),
        market_context_service=market_context_service,
        multi_timeframe_service=multi_timeframe_service,
        market_data_validator=MarketDataValidator(),
        planner=planner,
        config=config,
        feature_engine=OnlineFeatureEngine(asset_class_by_symbol),
        event_store=event_store,
        runtime_state_store=state_store,
        trade_journal=trade_journal,
        market_journal=market_journal,
        candle_journal=candle_journal,
        heartbeat=RuntimeHeartbeat(settings.runtime_heartbeat_minutes),
        research_pipeline=research_pipeline,
    )

    trade_journal.write(
        "runtime_started",
        {
            "run_id": run_id,
            "strategy": config.strategy.name,
            "profile": V3_PROFILE,
            "market_data_mode": "websocket",
            "execution_mode": settings.broker,
            "recoverability_enabled": False,
            "hedge_execution_enabled": False,
            "live_capital_authority": False,
            "broker_cost_assumption": {
                "fixed_fee_per_request_usd": (
                    ETORO_FIXED_FEE_ASSUMPTION_PER_REQUEST
                ),
                "authority": "research_estimate_not_broker_actual",
            },
            "run_journal_root": str(run_paths.root),
            "rotated_run_ids": list(removed_runs),
        },
    )
    logger.info(
        "Starting Goblin V3 | run_id=%s | broker=%s | strategy=%s | "
        "watchlist=%s | run_logs=%s",
        run_id,
        settings.broker,
        config.strategy.name,
        symbols,
        run_paths.root,
    )

    try:
        runtime.run()
        run_status = "completed"
    except Exception as exc:
        run_status = "failed"
        logger.exception("Goblin V3 runtime failed: %s", exc)
        trade_journal.write(
            "error",
            {
                "stage": "v3_runtime",
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        raise
    finally:
        if research_pipeline is not None:
            research_pipeline.flush()
        else:
            _write_disabled_research_summary(
                path=run_paths.research_summary,
                run_id=run_id,
                updated_at=datetime.now(timezone.utc),
            )
        trade_journal.write(
            "runtime_stopped",
            {
                "run_id": run_id,
                "status": run_status,
                "loop_id": runtime.loop_id,
                "v3_metrics": runtime._heartbeat_metrics(),
            },
        )
        summary = trade_journal.finalize()
        summary.setdefault("market_data", {})["model_version"] = (
            MARKET_DATA_MODEL_VERSION
        )
        write_run_manifest(settings.daily_summary_path, summary)
        write_run_manifest(run_paths.summary, summary)
        for manifest_path in (run_paths.manifest, settings.run_manifest_path):
            finalize_run_manifest(
                manifest_path,
                status=run_status,
                summary=summary,
                runtime_metrics=runtime._heartbeat_metrics(),
            )


if __name__ == "__main__":
    main()
