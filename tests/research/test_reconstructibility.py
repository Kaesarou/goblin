from app.research.pipeline import RESEARCH_FEATURE_NAMES
from app.research.reconstructibility import (
    ResearchReconstructibility,
    feature_reconstructibility,
    reconstructibility_contract_metadata,
)
from app.research.research_state import (
    CANDLE_RESEARCH_FEATURE_NAMES,
    MARKET_CONTEXT_RESEARCH_FEATURE_NAMES,
)


def test_every_research_feature_has_a_machine_readable_classification():
    mapping = feature_reconstructibility()

    assert set(mapping) == set(RESEARCH_FEATURE_NAMES)
    assert mapping['return_5m_percent'] == (
        ResearchReconstructibility.EXACT_HISTORICAL
    )
    assert mapping['market_regime'] == (
        ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY
    )
    assert mapping['benchmark_snapshot_age_seconds'] == (
        ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY
    )
    assert mapping['breadth_coverage_ratio'] == (
        ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY
    )
    assert mapping['micro_10s_mid_tick_imbalance'] == (
        ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY
    )
    assert mapping['micro_10s_quote_rate_hz'] == (
        ResearchReconstructibility.PROSPECTIVE_ONLY
    )
    assert mapping['micro_30s_interarrival_median_ms'] == (
        ResearchReconstructibility.PROSPECTIVE_ONLY
    )
    assert mapping['micro_60s_last_value_change_ratio'] == (
        ResearchReconstructibility.PROSPECTIVE_ONLY
    )


def test_candles_are_exact_but_side_neutral_context_is_proxy_historically():
    mapping = feature_reconstructibility()

    assert all(
        mapping[name] == ResearchReconstructibility.EXACT_HISTORICAL
        for name in CANDLE_RESEARCH_FEATURE_NAMES
    )
    assert all(
        mapping[name]
        == ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY
        for name in MARKET_CONTEXT_RESEARCH_FEATURE_NAMES
    )


def test_prospective_only_historical_materialization_is_null():
    metadata = reconstructibility_contract_metadata()

    assert metadata['version'] == 'research_reconstructibility_v2'
    assert metadata['historical_materialization_policy'][
        ResearchReconstructibility.PROSPECTIVE_ONLY
    ] == 'null'
    assert 'accepted unchanged WebSocket messages' in (
        metadata['historical_market_stream_scope']
    )
    assert metadata['family_classification'][
        'm1_candle_features'
    ] == ResearchReconstructibility.EXACT_HISTORICAL
    assert metadata['family_classification'][
        'side_neutral_market_context'
    ] == ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY
    assert metadata['family_classification'][
        'latest_accepted_price_change_quote_and_spread'
    ] == ResearchReconstructibility.EXACT_HISTORICAL
    assert metadata['family_classification'][
        'unchanged_patch_and_payload_schema_presence'
    ] == ResearchReconstructibility.PROSPECTIVE_ONLY
