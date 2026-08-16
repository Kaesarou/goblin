from __future__ import annotations

from enum import StrEnum

from app.research.microstructure import MICROSTRUCTURE_FEATURE_NAMES
from app.research.research_state import (
    CANDLE_RESEARCH_FEATURE_NAMES,
    MARKET_CONTEXT_RESEARCH_FEATURE_NAMES,
)

RESEARCH_RECONSTRUCTIBILITY_CONTRACT_VERSION = (
    'research_reconstructibility_v2'
)


class ResearchReconstructibility(StrEnum):
    EXACT_HISTORICAL = 'EXACT_HISTORICAL'
    HISTORICAL_PRICE_CHANGE_PROXY = 'HISTORICAL_PRICE_CHANGE_PROXY'
    PROSPECTIVE_ONLY = 'PROSPECTIVE_ONLY'


_PROSPECTIVE_ONLY_SUFFIXES = (
    'quote_count',
    'quote_rate_hz',
    'temporal_coverage_ratio',
    'last_value_change_ratio',
    'interarrival_median_ms',
    'interarrival_burstiness',
)


def feature_reconstructibility() -> dict[str, str]:
    mapping = {
        name: ResearchReconstructibility.EXACT_HISTORICAL.value
        for name in CANDLE_RESEARCH_FEATURE_NAMES
    }
    mapping.update(
        {
            name: ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY.value
            for name in MARKET_CONTEXT_RESEARCH_FEATURE_NAMES
        }
    )
    mapping.update(
        {
            name: (
                ResearchReconstructibility.PROSPECTIVE_ONLY.value
                if any(
                    name.endswith(suffix)
                    for suffix in _PROSPECTIVE_ONLY_SUFFIXES
                )
                else ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY.value
            )
            for name in MICROSTRUCTURE_FEATURE_NAMES
        }
    )
    return mapping


def reconstructibility_contract_metadata() -> dict[str, object]:
    mapping = feature_reconstructibility()
    return {
        'version': RESEARCH_RECONSTRUCTIBILITY_CONTRACT_VERSION,
        'classes': {
            ResearchReconstructibility.EXACT_HISTORICAL.value: (
                'Recomputable from retained historical inputs when those '
                'inputs are present and the prospective contract does not '
                'depend on accepted unchanged WebSocket messages.'
            ),
            ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY.value: (
                'May be reconstructed only from retained accepted '
                'price-changing events and must be named/labelled as a proxy.'
            ),
            ResearchReconstructibility.PROSPECTIVE_ONLY.value: (
                'Requires the prospective accepted WebSocket event stream; '
                'historical materialization must be null/unavailable.'
            ),
        },
        'feature_class_by_name': mapping,
        'family_classification': {
            'm1_candle_features': (
                ResearchReconstructibility.EXACT_HISTORICAL.value
            ),
            'side_neutral_market_context': (
                ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY.value
            ),
            'latest_accepted_price_change_quote_and_spread': (
                ResearchReconstructibility.EXACT_HISTORICAL.value
            ),
            'microstructure_price_change_path': (
                ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY.value
            ),
            'exact_websocket_message_activity_and_interarrival': (
                ResearchReconstructibility.PROSPECTIVE_ONLY.value
            ),
            'unchanged_patch_and_payload_schema_presence': (
                ResearchReconstructibility.PROSPECTIVE_ONLY.value
            ),
        },
        'historical_materialization_policy': {
            ResearchReconstructibility.EXACT_HISTORICAL.value: 'value',
            ResearchReconstructibility.HISTORICAL_PRICE_CHANGE_PROXY.value: (
                'explicitly_named_proxy_or_null'
            ),
            ResearchReconstructibility.PROSPECTIVE_ONLY.value: 'null',
        },
        'historical_market_stream_scope': (
            'accepted price-changing snapshots only; accepted unchanged '
            'WebSocket messages, unchanged patch activity and patch field '
            'presence are not retained'
        ),
        'context_proxy_reason': (
            'Prospective benchmark/breadth/sector availability and freshness '
            'can depend on accepted unchanged WebSocket messages that were not '
            'retained by historical market_price_changed streams.'
        ),
    }
