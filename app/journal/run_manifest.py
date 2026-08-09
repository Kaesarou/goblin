import hashlib
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.execution.breakeven_profile import BREAKEVEN_PROFILE_CONTRACT_VERSION
from app.execution.entry_decision import ENTRY_DECISION_MODEL_VERSION
from app.execution.position_close_reason import POSITION_CLOSE_TAXONOMY_VERSION
from app.execution.position_lifecycle_engine import (
    POSITION_LIFECYCLE_CONTRACT_VERSION,
)
from app.execution.position_models import POSITION_ECONOMICS_CONTRACT_VERSION
from app.execution.scoring.frozen_logistic import (
    FrozenOutcomeProbabilityModel,
)
from app.execution.scoring.managed_outcome import FrozenManagedOutcomeModel
from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_OUTCOME_FEATURE_CONTRACT_VERSION,
    MANAGED_OUTCOME_MODEL_VERSION,
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.execution.scoring.market_context_scorer import (
    MARKET_CONTEXT_SCORER_VERSION,
)
from app.execution.scoring.multi_timeframe_scorer import (
    MULTI_TIMEFRAME_SCORER_VERSION,
)
from app.execution.scoring.outcome_probability_model_contract import (
    MINIMUM_DIRECTION_EDGE,
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
)
from app.execution.scoring.tp_feasibility import (
    TP_FEASIBILITY_MODEL_VERSION,
)
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.analysis_journal import ENTRY_DECISION_SCHEMA_VERSION
from app.journal.analysis_ready_summary import (
    ANALYSIS_READY_SUMMARY_SCHEMA_VERSION,
)
from app.journal.daily_summary import DAILY_SUMMARY_SCHEMA_VERSION
from app.journal.serialization import serialize_value
from app.market.data_quality import (
    QUOTE_QUALITY_CONTRACT_VERSION,
    quote_quality_contract_metadata,
)
from app.market.market_context import MARKET_CONTEXT_VERSION
from app.market.models import EXECUTABLE_PRICE_CONTRACT_VERSION
from app.market.timeframes import (
    BASE_TIMEFRAME,
    MULTI_TIMEFRAME_MODEL_VERSION,
    SUPPORTED_TIMEFRAMES,
)
from app.risk.trade_cooldown import TRADE_COOLDOWN_CONTRACT_VERSION
from app.risk.trade_cost_model import TRADE_COST_CONTRACT_VERSION

_SENSITIVE_SETTINGS = {'ETORO_API_KEY', 'ETORO_USER_KEY'}
RUN_MANIFEST_SCHEMA_VERSION = 13


def build_run_id(started_at: datetime | None = None) -> str:
    actual_started_at = started_at or datetime.now(UTC)
    return actual_started_at.strftime('run_%Y%m%dT%H%M%S_%fZ')


def run_artifact_path(base_path: str, run_id: str) -> str:
    path = Path(base_path)
    return str(path.parent / 'runs' / run_id / path.name)


def resolve_git_commit() -> str | None:
    for variable_name in ('GIT_COMMIT', 'GITHUB_SHA', 'SOURCE_VERSION'):
        value = os.getenv(variable_name)
        if value and value.strip().lower() not in {'unknown', 'local'}:
            return value.strip()
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def resolve_code_fingerprint(
    source_root: str | Path | None = None,
) -> str | None:
    root = (
        Path(source_root)
        if source_root is not None
        else Path(__file__).resolve().parents[1]
    )
    source_files = sorted(
        path for path in root.rglob('*.py') if path.is_file()
    )
    if not source_files:
        return None
    digest = hashlib.sha256()
    for source_file in source_files:
        relative_path = source_file.relative_to(root).as_posix()
        digest.update(relative_path.encode('utf-8'))
        digest.update(b'\0')
        digest.update(source_file.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def build_run_manifest(
    *,
    settings: Settings,
    strategy_profile: Any,
    instrument_registry: InstrumentRegistry,
    symbols: list[str],
    run_id: str,
    started_at: datetime,
    manifest_path: str | None = None,
    summary_path: str | None = None,
) -> dict[str, Any]:
    symbol_profiles = {
        symbol: instrument_registry.resolve(symbol)
        for symbol in symbols
    }
    benchmark_symbols = {
        asset_class.value: list(configured_symbols)
        for asset_class, configured_symbols
        in settings.benchmark_symbols_by_asset_class().items()
    }
    actual_manifest_path = manifest_path or settings.run_manifest_path
    actual_summary_path = summary_path or settings.daily_summary_path
    outcome_probability_model = FrozenOutcomeProbabilityModel.load()
    managed_outcome_model = FrozenManagedOutcomeModel.load()
    direction_segments = {
        name: {
            'feature_family': segment.feature_family,
            'training_status': segment.training_status,
            'source_segment': segment.source_segment,
            'training_rows': segment.training_rows,
            'segment_prior': segment.segment_prior,
            'model_weight': segment.model_weight,
            'segment_prior_weight': segment.segment_prior_weight,
        }
        for name, segment
        in outcome_probability_model.direction_segments.items()
    }
    return {
        'schema_version': RUN_MANIFEST_SCHEMA_VERSION,
        'run_id': run_id,
        'status': 'running',
        'started_at': started_at,
        'ended_at': None,
        'code': {
            'git_commit': resolve_git_commit(),
            'source_sha256': resolve_code_fingerprint(),
            'python_version': platform.python_version(),
        },
        'models': {
            'market_context': MARKET_CONTEXT_VERSION,
            'market_context_score': MARKET_CONTEXT_SCORER_VERSION,
            'multi_timeframe': MULTI_TIMEFRAME_MODEL_VERSION,
            'multi_timeframe_score': MULTI_TIMEFRAME_SCORER_VERSION,
            'entry_decision': ENTRY_DECISION_MODEL_VERSION,
            'tp_feasibility': TP_FEASIBILITY_MODEL_VERSION,
            'outcome_probability': OUTCOME_PROBABILITY_MODEL_VERSION,
            'outcome_probability_features': (
                OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
            ),
            'outcome_probability_artifact_sha256': (
                outcome_probability_model.artifact_sha256
            ),
            'outcome_probability_direction_dataset_sha256': (
                outcome_probability_model.provenance.get('dataset_sha256')
            ),
            'outcome_probability_activity_dataset_sha256': (
                outcome_probability_model.provenance.get(
                    'activity_dataset_sha256'
                )
            ),
            'outcome_probability_training_asset_classes': list(
                outcome_probability_model.training_asset_classes
            ),
            'outcome_probability_supported_segments': list(
                outcome_probability_model.supported_segments
            ),
            'outcome_probability_direction_margin': MINIMUM_DIRECTION_EDGE,
            'outcome_probability_direction_segments': direction_segments,
            'managed_outcome': MANAGED_OUTCOME_MODEL_VERSION,
            'managed_outcome_features': (
                MANAGED_OUTCOME_FEATURE_CONTRACT_VERSION
            ),
            'managed_outcome_artifact_sha256': (
                managed_outcome_model.artifact_sha256
            ),
            'managed_outcome_training_asset_classes': list(
                managed_outcome_model.training_asset_classes
            ),
            'managed_outcome_supported_segments': list(
                managed_outcome_model.supported_segments
            ),
            'managed_outcome_provenance': dict(
                managed_outcome_model.provenance
            ),
            'managed_outcome_runtime_role': 'active_equity_and_crypto',
        },
        'strategy': {
            'name': 'TrendStrategy',
            'profile': strategy_profile.name,
            'profile_config': strategy_profile,
            'selection_policy': MANAGED_SELECTION_POLICY_VERSION,
            'breakeven_profile_contract': (
                BREAKEVEN_PROFILE_CONTRACT_VERSION
            ),
            'breakeven_profile': strategy_profile.breakeven_profile_name,
            'breakeven_trigger_percent': {
                asset_class.value: config.risk.breakeven_trigger_percent
                for asset_class, config
                in strategy_profile.instrument_configs.items()
            },
        },
        'runtime': {
            'contracts': {
                'executable_prices': EXECUTABLE_PRICE_CONTRACT_VERSION,
                'position_lifecycle': POSITION_LIFECYCLE_CONTRACT_VERSION,
                'position_economics': POSITION_ECONOMICS_CONTRACT_VERSION,
                'trade_costs': TRADE_COST_CONTRACT_VERSION,
                'position_close_taxonomy': (
                    POSITION_CLOSE_TAXONOMY_VERSION
                ),
                'trade_cooldown': TRADE_COOLDOWN_CONTRACT_VERSION,
                'quote_quality': QUOTE_QUALITY_CONTRACT_VERSION,
            },
            'economic_convention': {
                'signal_and_candle_price': 'last_execution',
                'buy_executable_exit_price': 'bid',
                'sell_executable_exit_price': 'ask',
                'post_trade_spread_deduction': False,
                'post_trade_deducted_costs': 'explicit_only',
                'broker_close_fill_priority': True,
            },
            'quote_quality_policy': quote_quality_contract_metadata(),
            'watchlist': symbols,
            'context_benchmarks': benchmark_symbols,
            'symbol_profiles': symbol_profiles,
            'settings': sanitized_settings_snapshot(settings),
            'multi_timeframe': {
                'base_timeframe_seconds': BASE_TIMEFRAME.value,
                'supported_timeframes_seconds': [
                    timeframe.value for timeframe in SUPPORTED_TIMEFRAMES
                ],
                'supported_timeframes': [
                    timeframe.name.lower()
                    for timeframe in SUPPORTED_TIMEFRAMES
                ],
                'sampling_source': 'event_driven_websocket',
                'config_by_symbol': {
                    symbol: instrument_registry.config_for(
                        symbol
                    ).multi_timeframe
                    for symbol in symbols
                },
            },
        },
        'analysis_sources': {
            'run_id': run_id,
            'schemas': {
                'entry_decision': ENTRY_DECISION_SCHEMA_VERSION,
                'daily_summary': DAILY_SUMMARY_SCHEMA_VERSION,
                'analysis_ready_summary': (
                    ANALYSIS_READY_SUMMARY_SCHEMA_VERSION
                ),
            },
            'market_stream': settings.market_log_path,
            'candle_stream': settings.candle_journal_path,
            'trade_stream': settings.journal_path,
            'error_stream': settings.errors_journal_path,
            'raw_market_retained': True,
            'raw_candles_retained': True,
            'multi_timeframe_bars_retained': True,
            'multi_timeframe_candidate_snapshots_retained': True,
            'candidate_id_enabled': True,
            'pending_lineage_enabled': True,
            'entry_routing_retained': True,
            'managed_stop_updates_retained': True,
            'entry_horizon_rejections_retained': True,
            'analysis_ready_entry_fields': [
                'schema_version',
                'candidate_id',
                'origin_candidate_id',
                'pending_entry_id',
                'candidate_timestamp',
                'symbol',
                'side',
                'segment',
                'entry_reference_price',
                'signal_price',
                'executable_entry_estimate',
                'broker_entry_fill_price',
                'pnl_entry_price',
                'bid',
                'ask',
                'last',
                'spread',
                'executable_entry_price',
                'observed_spread_percent',
                'profile_key',
                'sl_tp_source',
                'effective_stop_loss_percent',
                'effective_take_profit_percent',
                'estimated_total_cost_percent',
                'estimated_explicit_cost_percent',
                'probability_score',
                'base_score',
                'directional_score',
                'market_context_score',
                'raw_market_context_score',
                'effective_market_context_contribution',
                'market_context_components',
                'multi_timeframe_score',
                'multi_timeframe_components',
                'tp_feasibility_score',
                'tp_feasibility_contribution',
                'movement_consumed_to_tp_ratio',
                'entry_freshness_score',
                'entry_route_action',
                'entry_route_reason',
                'extension_to_tp_ratio',
                'selection_outcome',
                'selection_reason',
                'touch_probability',
                'direction_probability',
                'tp_probability',
                'sl_probability',
                'neither_probability',
                'direction_break_even_probability',
                'direction_edge',
                'outcome_probability',
                'outcome_probability_model_version',
                'managed_protection_probability',
                'managed_positive_probability',
                'managed_expected_net_return_percent',
                'managed_edge',
                'managed_outcome_model_version',
                'selection_policy_version',
                'relative_spread_ratio',
                'relative_spread_percentile',
                'relative_spread_recent_change',
                'relative_spread_available',
            ],
        },
        'files': {
            'manifest': actual_manifest_path,
            'latest_manifest': settings.run_manifest_path,
            'summary': actual_summary_path,
            'latest_summary': settings.daily_summary_path,
            'partial_summary': settings.partial_daily_summary_path,
            'trades': settings.journal_path,
            'errors': settings.errors_journal_path,
            'market': settings.market_log_path,
            'candles': settings.candle_journal_path,
            'debug_decisions': settings.debug_decisions_journal_path,
        },
    }


def sanitized_settings_snapshot(settings: Settings) -> dict[str, Any]:
    values = settings.model_dump(by_alias=True)
    return {
        key: value
        for key, value in values.items()
        if key not in _SENSITIVE_SETTINGS
    }


def write_run_manifest(path: str, manifest: dict[str, Any]) -> None:
    _write_json_atomically(Path(path), manifest)


def finalize_run_manifest(
    path: str,
    *,
    ended_at: datetime | None = None,
    status: str = 'completed',
    summary: dict[str, Any] | None = None,
) -> None:
    manifest_path = Path(path)
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['status'] = status
    manifest['ended_at'] = ended_at or datetime.now(UTC)
    if summary is not None:
        manifest['result'] = {
            'market_snapshots': summary.get('market_data', {}).get(
                'snapshots', 0
            ),
            'market_data_rejected': summary.get('market_data', {}).get(
                'rejected', 0
            ),
            'market_data_quarantined': summary.get(
                'market_data', {}
            ).get('quarantined', 0),
            'candles_closed': summary.get('market_data', {}).get(
                'candles_closed', 0
            ),
            'timeframe_bars_closed': summary.get(
                'multi_timeframe', {}
            ).get('closed_total', 0),
            'timeframe_bars_incomplete': summary.get(
                'multi_timeframe', {}
            ).get('incomplete_total', 0),
            'ready_for_selection': summary.get(
                'entry_routing', {}
            ).get('ready_for_selection', 0),
            'wait_for_retest': summary.get(
                'entry_routing', {}
            ).get('wait_for_retest', 0),
            'skip': summary.get('entry_routing', {}).get('skip', 0),
            'orders_submitted': summary.get('orders', {}).get(
                'submitted', 0
            ),
            'positions_opened': summary.get('positions', {}).get(
                'opened', 0
            ),
            'positions_closed': summary.get('positions', {}).get(
                'closed', 0
            ),
            'errors': summary.get('errors', {}).get('total', 0),
        }
    _write_json_atomically(manifest_path, manifest)


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + '.tmp')
    temporary_path.write_text(
        json.dumps(
            serialize_value(value),
            ensure_ascii=False,
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )
    temporary_path.replace(path)
