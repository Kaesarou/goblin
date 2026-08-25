from datetime import datetime, timezone

from app.v3.recoverability import RecoverabilityScorer


def test_default_artifact_scores_complete_feature_vector():
    scorer = RecoverabilityScorer.from_default_artifact()
    features = {
        name: mean
        for name, mean in zip(
            scorer.artifact.features,
            scorer.artifact.mean,
            strict=True,
        )
    }
    result = scorer.score(features, asof=datetime(2026, 8, 25, tzinfo=timezone.utc))
    assert result.valid
    assert 0.0 <= result.rank_quantile <= 1.0
    assert result.model_version.startswith("RECOVERABILITY_LONG_LOGIT_V1")


def test_missing_feature_fails_closed():
    scorer = RecoverabilityScorer.from_default_artifact()
    result = scorer.score({}, asof=datetime(2026, 8, 25, tzinfo=timezone.utc))
    assert not result.valid
    assert result.invalid_reason.startswith("missing_or_nonfinite:")
