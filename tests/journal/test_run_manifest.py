from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.execution.breakeven_profile import BreakevenProfileName
from app.execution.scoring.outcome_probability_model_contract import (
    MINIMUM_DIRECTION_EDGE,
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
    SUPPORTED_DIRECTION_SEGMENTS,
)
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.run_manifest import (
    build_run_manifest,
    resolve_code_fingerprint,
    run_artifact_path,
    sanitized_settings_snapshot,
)
from app.market.data_quality import QUOTE_QUALITY_CONTRACT_VERSION
from app.research.microstructure import MICROSTRUCTURE_CONTRACT_VERSION
from app.research.payload_schema_observer import (
    ETORO_PAYLOAD_SCHEMA_OBSERVER_VERSION,
)
from app.research.research_state import RESEARCH_STATE_CONTRACT_VERSION
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_run_manifest_captures_segmented_probability_contract():
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
        started_at=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
    )

    assert manifest['schema_version'] == 14
    assert 'ETORO_API_KEY' not in manifest['runtime']['settings']
    assert 'ETORO_USER_KEY' not in manifest['runtime']['settings']
    models = manifest['models']
    assert models['outcome_probability'] == OUTCOME_PROBABILITY_MODEL_VERSION
    assert models['outcome_probability_features'] == (
        OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
    )
    assert models['outcome_probability_artifact_sha256'] == (
        '57cf61302346288ffdb76bc66f134437bd75a9e4064f30569f2a54b47606b55e'
    )
    assert models['outcome_probability_direction_dataset_sha256'] == (
        '54314272bbdfe5ecff83a7a9eae99ec9c36c4e547a73145f8ce5241923d7cb06'
    )
    assert models['outcome_probability_activity_dataset_sha256'] == (
        '4613e958702210226e9b397be2449e5e311f77e3dccc04d8c0cf81be0ceb1628'
    )
    assert tuple(models['outcome_probability_supported_segments']) == (
        SUPPORTED_DIRECTION_SEGMENTS
    )
    assert models['outcome_probability_direction_margin'] == (
        MINIMUM_DIRECTION_EDGE
    )
    crypto = models['outcome_probability_direction_segments']['CRYPTO_BUY']
    assert crypto['training_status'] == 'provisional_transfer'
    assert crypto['source_segment'] == 'EQUITY_US_BUY'
    assert crypto['training_rows'] == 0
    assert models['outcome_probability_direction_segments'][
        'EQUITY_US_BUY'
    ]['training_status'] == 'trained'
    assert models['managed_outcome_runtime_role'] == 'active_equity_and_crypto'
    assert not any(key.startswith('managed_v2') for key in models)

    entry_fields = manifest['analysis_sources'][
        'analysis_ready_entry_fields'
    ]
    assert {
        'schema_version',
        'segment',
        'entry_reference_price',
        'bid',
        'ask',
        'last',
        'spread',
        'executable_entry_price',
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
    } <= set(entry_fields)
    assert not any('managed_v2' in field for field in entry_fields)

    assert manifest['strategy']['selection_policy'] == 'managed_edge_v1'
    assert not any(
        key.startswith('managed_v2') for key in manifest['strategy']
    )
    assert manifest['analysis_sources']['schemas'] == {
        'entry_decision': 2,
        'daily_summary': 10,
        'analysis_ready_summary': 13,
        'research_state': 1,
    }
    assert manifest['strategy']['breakeven_profile'] == (
        BreakevenProfileName.CORRECTED_BASELINE_V1
    )
    assert manifest['strategy']['breakeven_trigger_percent'] == {
        'CRYPTO': 0.2,
        'EQUITY_US': 0.6,
        'EQUITY_EU': 0.55,
    }
    assert manifest['runtime']['economic_convention'] == {
        'signal_and_candle_price': 'last_execution',
        'buy_executable_exit_price': 'bid',
        'sell_executable_exit_price': 'ask',
        'post_trade_spread_deduction': False,
        'post_trade_deducted_costs': 'explicit_only',
        'broker_close_fill_priority': True,
    }
    assert manifest['runtime']['contracts']['quote_quality'] == (
        QUOTE_QUALITY_CONTRACT_VERSION
    )
    assert manifest['runtime']['quote_quality_policy']['version'] == (
        QUOTE_QUALITY_CONTRACT_VERSION
    )
    research = manifest['runtime']['research']
    assert research['activation_status'] == 'active'
    assert research['strictly_read_only'] is True
    assert research['candidate_generator_independent'] is True
    assert research['journal_encoding'] == 'compact_jsonl_gzip'
    assert research['last_hour_excluded'] is True
    assert len(research['combined_feature_set_sha256']) == 64
    assert research['research_state']['version'] == (
        RESEARCH_STATE_CONTRACT_VERSION
    )
    assert research['research_state']['cadence_minutes'] == 5
    assert research['research_state']['side_neutral'] is True
    assert research['research_state']['candidate_required'] is False
    assert research['research_state']['quote_or_candle_required'] is False
    assert research['research_state']['collection_start_convention'] == (
        'state_at > runtime_started_at'
    )
    assert research['microstructure']['version'] == (
        MICROSTRUCTURE_CONTRACT_VERSION
    )
    assert research['microstructure']['input'] == (
        'accepted_websocket_market_snapshots_only'
    )
    assert research['payload_schema_observer']['version'] == (
        ETORO_PAYLOAD_SCHEMA_OBSERVER_VERSION
    )
    assert research['payload_schema_observer']['raw_payload_retained'] is False
    contracts = manifest['runtime']['contracts']
    assert contracts['research_state'] == RESEARCH_STATE_CONTRACT_VERSION
    assert contracts['microstructure'] == MICROSTRUCTURE_CONTRACT_VERSION
    assert contracts['etoro_payload_schema_observer'] == (
        ETORO_PAYLOAD_SCHEMA_OBSERVER_VERSION
    )
    assert manifest['analysis_sources']['research_stream'].startswith(
        'data/logs/'
    )
    assert manifest['analysis_sources']['etoro_payload_schema'].startswith(
        'data/logs/'
    )


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


def test_research_sidecar_can_be_explicitly_disabled():
    assert Settings(RESEARCH_ENABLED=False).research_enabled is False


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
