from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.execution.entry_decision import ENTRY_DECISION_MODEL_VERSION
from app.execution.scoring.market_context_scorer import (
    MARKET_CONTEXT_SCORER_VERSION,
)
from app.execution.scoring.multi_timeframe_scorer import (
    MULTI_TIMEFRAME_SCORER_VERSION,
)
from app.execution.scoring.pr5e_model_contract import (
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
)
from app.execution.scoring.tp_feasibility import (
    TP_FEASIBILITY_MODEL_VERSION,
)
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.run_manifest import (
    build_run_manifest,
    resolve_code_fingerprint,
    run_artifact_path,
    sanitized_settings_snapshot,
)
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_run_manifest_captures_pr5e_contract_without_broker_secrets():
    settings = Settings(
        WATCHLIST='AAPL',
        EQUITY_US_SYMBOLS='AAPL',
        ETORO_API_KEY='secret-api',
        ETORO_USER_KEY='secret-user',
    )
    profile = BalancedStrategyConfig()
    registry = InstrumentRegistry(
        settings,
        instrument_configs=profile.instrument_configs,
    )
    manifest = build_run_manifest(
        settings=settings,
        strategy_profile=profile,
        instrument_registry=registry,
        symbols=['AAPL'],
        run_id='run-test',
        started_at=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        manifest_path='data/logs/runs/run-test/run_manifest.json',
        summary_path='data/logs/runs/run-test/daily_summary.json',
    )

    snapshot = manifest['runtime']['settings']
    assert manifest['schema_version'] == 10
    assert 'ETORO_API_KEY' not in snapshot
    assert 'ETORO_USER_KEY' not in snapshot
    assert manifest['strategy']['profile'] == 'balanced'
    assert manifest['runtime']['watchlist'] == ['AAPL']
    assert (
        manifest['runtime']['multi_timeframe']['sampling_source']
        == 'event_driven_websocket'
    )
    assert 'poll_interval_seconds' not in manifest['runtime']['multi_timeframe']
    assert manifest['analysis_sources']['pending_lineage_enabled'] is True
    assert manifest['analysis_sources']['managed_stop_updates_retained'] is True
    assert manifest['analysis_sources']['entry_horizon_rejections_retained'] is True
    fields = manifest['analysis_sources']['analysis_ready_entry_fields']
    for field in (
        'origin_candidate_id',
        'pending_entry_id',
        'profile_key',
        'market_context_score',
        'raw_market_context_score',
        'effective_market_context_contribution',
        'multi_timeframe_score',
        'tp_feasibility_score',
        'movement_consumed_to_tp_ratio',
        'entry_freshness_score',
        'extension_to_tp_ratio',
        'probability_score',
        'touch_probability',
        'direction_probability',
        'tp_probability',
        'sl_probability',
        'neither_probability',
        'direction_break_even_probability',
        'direction_edge',
        'outcome_probability_model_version',
    ):
        assert field in fields
    assert manifest['models']['entry_decision'] == ENTRY_DECISION_MODEL_VERSION
    assert manifest['models']['entry_decision'] == 'entry_router_v6'
    assert (
        manifest['models']['market_context_score']
        == MARKET_CONTEXT_SCORER_VERSION
    )
    assert manifest['models']['market_context_score'] == 'market_context_score_v3'
    assert (
        manifest['models']['multi_timeframe_score']
        == MULTI_TIMEFRAME_SCORER_VERSION
    )
    assert manifest['models']['multi_timeframe_score'] == 'multi_timeframe_score_v2'
    assert manifest['models']['tp_feasibility'] == TP_FEASIBILITY_MODEL_VERSION
    assert manifest['models']['tp_feasibility'] == 'tp_feasibility_score_v4'
    assert manifest['models']['outcome_probability'] == (
        OUTCOME_PROBABILITY_MODEL_VERSION
    )
    assert manifest['models']['outcome_probability'] == (
        'outcome_probability_v1'
    )
    assert manifest['models']['outcome_probability_features'] == (
        OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
    )
    assert manifest['models']['outcome_probability_features'] == (
        'pr5e_features_v1'
    )
    assert manifest['models']['outcome_probability_artifact_sha256'] == (
        '0507ab0788d4683a0b69587f49a920afa665af95584f5dec07383a64f3daf58f'
    )
    assert manifest[
        'models'
    ]['outcome_probability_training_dataset_sha256'] == (
        '4608e799936ac007292dcb5cfc1894880893e3f9544d0b7a33096d23b5bb8290'
    )
    assert manifest['models'][
        'outcome_probability_training_asset_classes'
    ] == ['EQUITY_EU', 'EQUITY_US']
    assert manifest['models']['multi_timeframe'] == 'multi_timeframe_features_v2'
    assert manifest['runtime']['multi_timeframe'][
        'supported_timeframes_seconds'
    ] == [60, 300, 900, 1800, 3600]
    assert manifest['code']['source_sha256']


def test_removed_runtime_settings_are_rejected():
    with pytest.raises(ValidationError):
        Settings(
            WATCHLIST='AAPL',
            EQUITY_US_SYMBOLS='AAPL',
            CANDLE_TIMEFRAME_SECONDS=300,
        )
    with pytest.raises(ValidationError):
        Settings(
            WATCHLIST='AAPL',
            EQUITY_US_SYMBOLS='AAPL',
            POLL_INTERVAL_SECONDS=15,
        )


def test_sanitized_settings_keeps_non_sensitive_operational_values():
    snapshot = sanitized_settings_snapshot(
        Settings(
            WATCHLIST='AAPL',
            EQUITY_US_SYMBOLS='AAPL',
            BROKER='paper',
            JOURNAL_DETAIL_LEVEL='debug',
        )
    )
    assert snapshot['WATCHLIST'] == 'AAPL'
    assert snapshot['BROKER'] == 'paper'
    assert snapshot['JOURNAL_DETAIL_LEVEL'] == 'debug'


def test_run_artifact_path_creates_stable_per_run_location():
    assert run_artifact_path(
        'data/logs/daily_summary.json',
        'run-123',
    ) == 'data/logs/runs/run-123/daily_summary.json'


def test_code_fingerprint_changes_when_source_changes(tmp_path):
    source_file = tmp_path / 'module.py'
    source_file.write_text('VALUE = 1\n', encoding='utf-8')
    first = resolve_code_fingerprint(tmp_path)
    source_file.write_text('VALUE = 2\n', encoding='utf-8')
    second = resolve_code_fingerprint(tmp_path)
    assert first and second and first != second
