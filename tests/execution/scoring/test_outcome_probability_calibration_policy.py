import pytest

from app.execution.scoring.frozen_logistic import (
    FrozenOutcomeProbabilityModel,
)
from app.execution.scoring.outcome_probability_model_contract import (
    OUTCOME_PROBABILITY_CALIBRATION_POLICY_VERSION,
    SUPPORTED_DIRECTION_SEGMENTS,
)


def test_calibration_policy_is_explicit_and_complete():
    model = FrozenOutcomeProbabilityModel.load()

    assert model.artifact_sha256 == (
        '57cf61302346288ffdb76bc66f134437bd75a9e4064f30569f2a54b47606b55e'
    )
    assert model.calibration_policy_version == (
        OUTCOME_PROBABILITY_CALIBRATION_POLICY_VERSION
    )
    assert model.calibration_policy_sha256 == (
        '8b0e9d3a882434541690af1e0d40b566d99fd0ffb47e5eeafa5186e357f95943'
    )
    assert set(model.direction_segments) == set(
        SUPPORTED_DIRECTION_SEGMENTS
    )


def test_only_eu_sell_uses_less_compressive_calibration():
    model = FrozenOutcomeProbabilityModel.load()
    eu_sell = model.direction_segments['EQUITY_EU_SELL']

    assert eu_sell.model_weight == pytest.approx(0.6)
    assert eu_sell.segment_prior_weight == pytest.approx(0.4)
    assert 0.6 + 0.4 * eu_sell.segment_prior > 0.71

    for segment, direction_model in model.direction_segments.items():
        if segment == 'EQUITY_EU_SELL':
            continue
        assert direction_model.model_weight == pytest.approx(0.5)
        assert direction_model.segment_prior_weight == pytest.approx(0.5)
