import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution.candidate_economics import (
    CandidateEconomics,
    EvaluatedTradeCandidate,
)
from app.execution.candidate_selector import (
    CandidateSelectionConfig,
    rank_evaluated_trade_candidates,
    select_evaluated_trade_candidates,
)
from app.execution.entry_decision import EntryAction, EntryDecision
from app.execution.scoring import managed_v2
from app.execution.scoring.managed_v2 import (
    CandidateManagedV2Evaluator,
    FrozenManagedV2Model,
)
from app.execution.scoring.managed_v2_features import (
    extract_managed_v2_features,
    missing_feature_names,
)
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_ECONOMICS_MODEL_VERSION,
    MANAGED_V2_FEATURE_CONTRACT_VERSION,
    MANAGED_V2_LABEL_CONTRACT_VERSION,
    MANAGED_V2_MODEL_VERSION,
    MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
    MANAGED_V2_PATH_MODEL_VERSION,
    MANAGED_V2_SELECTION_POLICY_VERSION,
    MANAGED_V2_SUPPORTED_SEGMENTS,
    feature_names_for,
)
from app.execution.strategy_segment import StrategySegment
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.market.relative_spread import SPREAD_CONTEXT_VERSION, SpreadContext
from app.strategies.signals import Signal

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)


def _evaluated(
    segment: StrategySegment,
    *,
    benchmark_available: bool = True,
    spread_available: bool = True,
    future_mtf: bool = False,
) -> EvaluatedTradeCandidate:
    sign = 1.0 if segment.side == 'BUY' else -1.0
    benchmark = SimpleNamespace(
        available=benchmark_available,
        momentum_percent=0.12,
    )
    spread = SpreadContext(
        version=SPREAD_CONTEXT_VERSION,
        available=spread_available,
        current_percent=0.05,
        reference_median_percent=(0.04 if spread_available else None),
        relative_to_median=(1.25 if spread_available else None),
        reference_percentile=(0.80 if spread_available else None),
        recent_change_ratio=(0.10 if spread_available else None),
        reference_observations=40,
    )
    market_context = SimpleNamespace(
        benchmark=benchmark,
        symbol_relative_strength_percent=0.22,
        spread=spread,
    )
    latest_closed_at = NOW + timedelta(minutes=1) if future_mtf else NOW
    timeframe_features = {
        name: SimpleNamespace(
            latest_bar_closed_at=latest_closed_at,
            return_sample_percent=0.30 * sign,
            close_vs_fast_ema_percent=0.20 * sign,
            fast_vs_slow_ema_percent=0.10 * sign,
            velocity_percent_per_bar=0.04 * sign,
            acceleration_percent_per_bar=0.01 * sign,
        )
        for name in ('m15', 'm30', 'h1')
    }
    candidate = TradeCandidate(
        symbol='AMD' if segment.asset_class.value == 'EQUITY_US' else 'SAP.DE',
        snapshot=MarketSnapshot(
            symbol='AMD' if segment.asset_class.value == 'EQUITY_US' else 'SAP.DE',
            bid=99.975,
            ask=100.025,
            last=100.0,
            timestamp=NOW,
        ),
        candle=Candle(
            symbol='AMD' if segment.asset_class.value == 'EQUITY_US' else 'SAP.DE',
            timeframe_seconds=60,
            open=99.8,
            high=100.2,
            low=99.7,
            close=100.0,
            volume=None,
            opened_at=NOW - timedelta(minutes=1),
            closed_at=NOW,
        ),
        signal=Signal(
            action=segment.side,
            setup_quality=0.8,
            reason='test',
            metadata={
                'session_move_percent': 0.50 * sign,
                'snapshot_momentum_percent': 0.20 * sign,
                'atr_percent': 0.30,
                'regime_noise_ratio': 0.40,
            },
        ),
        rank_reason='test',
        candidate_id=f'candidate-{segment.value}',
        origin_candidate_id=f'candidate-{segment.value}',
        segment=segment,
        touch_probability=0.64,
        outcome_probability_metadata={
            'activity_features': {'session_progress': 0.35},
            'direction_features': {'aligned_benchmark_momentum': 99.0},
        },
        market_context=market_context,
        multi_timeframe_context=SimpleNamespace(
            features_by_timeframe=timeframe_features,
        ),
    )
    return EvaluatedTradeCandidate(
        candidate=candidate,
        economics=CandidateEconomics(
            position_value=500.0,
            expected_gross_profit=5.0,
            expected_net_profit=3.2,
            expected_net_profit_percent=0.64,
            estimated_total_cost=1.8,
            estimated_total_cost_percent=0.36,
            min_expected_net_profit_percent=0.10,
            required_min_expected_net_profit_amount=0.50,
            effective_take_profit_percent=1.0,
            effective_stop_loss_percent=0.70,
            cost_to_tp_ratio=0.36,
            reward_to_risk_ratio=1.43,
            net_reward_to_risk_ratio=0.60,
        ),
        tp_feasibility=SimpleNamespace(
            entry_freshness_score=88.0,
            movement_consumed_to_tp_ratio=0.30,
            atr_percent=0.30,
        ),
        entry_decision=EntryDecision(
            action=EntryAction.READY_FOR_SELECTION,
            reason='entry_conditions_satisfied',
        ),
    )


def test_segment_feature_contracts_are_sparse_and_distinct():
    assert 'm30_return_sample_aligned' in feature_names_for(
        StrategySegment.EQUITY_EU_BUY,
        'opportunity',
    )
    assert 'h1_return_sample_aligned' in feature_names_for(
        StrategySegment.EQUITY_EU_BUY,
        'opportunity',
    )
    assert 'm15_return_sample_aligned' in feature_names_for(
        StrategySegment.EQUITY_US_SELL,
        'opportunity',
    )
    assert 'm30_return_sample_aligned' not in feature_names_for(
        StrategySegment.EQUITY_US_BUY,
        'opportunity',
    )
    assert not any(
        name.startswith(('m15_', 'm30_', 'h1_'))
        for name in feature_names_for(
            StrategySegment.EQUITY_EU_SELL,
            'opportunity',
        )
    )


@pytest.mark.parametrize('segment', MANAGED_V2_SUPPORTED_SEGMENTS)
def test_each_segment_receives_exactly_its_own_contract(segment):
    features = extract_managed_v2_features(_evaluated(segment))

    assert features.segment is segment
    assert tuple(features.opportunity) == feature_names_for(
        segment,
        'opportunity',
    )
    assert tuple(features.path) == feature_names_for(segment, 'path')
    assert tuple(
        features.economics(
            opportunity_probability=0.6,
            path_probability=0.7,
        )
    ) == feature_names_for(segment, 'economics')


def test_optional_absence_is_explicit_and_never_borrowed_from_p_direction():
    item = _evaluated(
        StrategySegment.EQUITY_US_BUY,
        benchmark_available=False,
        spread_available=False,
    )
    features = extract_managed_v2_features(item)

    assert features.raw['aligned_benchmark_momentum'] is None
    assert features.raw['relative_spread_ratio'] is None
    assert 'aligned_benchmark_momentum' in missing_feature_names(
        features.opportunity
    )
    assert item.candidate.outcome_probability_metadata[
        'direction_features'
    ]['aligned_benchmark_momentum'] == 99.0


def test_benchmark_context_is_direct_and_side_aware():
    buy = extract_managed_v2_features(
        _evaluated(StrategySegment.EQUITY_US_BUY)
    )
    sell = extract_managed_v2_features(
        _evaluated(StrategySegment.EQUITY_US_SELL)
    )

    assert buy.raw['aligned_benchmark_momentum'] == pytest.approx(0.12)
    assert sell.raw['aligned_benchmark_momentum'] == pytest.approx(-0.12)


def test_future_multi_timeframe_value_fails_closed():
    with pytest.raises(RuntimeError, match='future information'):
        extract_managed_v2_features(
            _evaluated(
                StrategySegment.EQUITY_EU_BUY,
                future_mtf=True,
            )
        )


def test_frozen_artifact_has_separate_components_and_complete_provenance():
    model = FrozenManagedV2Model.load()
    result = CandidateManagedV2Evaluator().evaluate(
        evaluated_candidate=_evaluated(StrategySegment.EQUITY_US_BUY)
    )
    metadata = result.candidate.managed_v2_metadata

    assert model.version == MANAGED_V2_MODEL_VERSION
    assert metadata['opportunity_model_version'] == (
        MANAGED_V2_OPPORTUNITY_MODEL_VERSION
    )
    assert metadata['path_model_version'] == MANAGED_V2_PATH_MODEL_VERSION
    assert metadata['economics_model_version'] == (
        MANAGED_V2_ECONOMICS_MODEL_VERSION
    )
    assert metadata['feature_contract_version'] == (
        MANAGED_V2_FEATURE_CONTRACT_VERSION
    )
    assert metadata['label_contract_version'] == (
        MANAGED_V2_LABEL_CONTRACT_VERSION
    )
    assert metadata['segment'] == StrategySegment.EQUITY_US_BUY.value
    assert metadata['deployment_status'] == 'shadow_not_approved'
    assert metadata['artifact_sha256'] == model.artifact_sha256
    assert set(metadata) >= {'opportunity', 'path', 'economics', 'floors'}


@pytest.mark.parametrize(
    ('manifest_key', 'bad_value', 'message'),
    [
        ('schema_version', 999, 'artifact schema version mismatch'),
        ('version', 'wrong', 'aggregate model version mismatch'),
        ('feature_contract_version', 'wrong', 'feature contract mismatch'),
        ('label_contract_version', 'wrong', 'label contract mismatch'),
    ],
)
def test_artifact_and_contract_mismatches_fail_closed(
    tmp_path,
    monkeypatch,
    manifest_key,
    bad_value,
    message,
):
    source = Path('app/execution/scoring/models/managed_v2')
    target = tmp_path / 'models' / 'managed_v2'
    shutil.copytree(source, target)
    manifest_path = target / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest[manifest_key] = bad_value
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(managed_v2, 'files', lambda _package: tmp_path)

    with pytest.raises(RuntimeError, match=message):
        FrozenManagedV2Model.load()


def test_segment_feature_mismatch_fails_closed(tmp_path, monkeypatch):
    source = Path('app/execution/scoring/models/managed_v2')
    target = tmp_path / 'models' / 'managed_v2'
    shutil.copytree(source, target)
    segment_path = target / 'EQUITY_US_BUY.json'
    artifact = json.loads(segment_path.read_text(encoding='utf-8'))
    artifact['features']['path'].append('foreign_feature')
    segment_path.write_text(json.dumps(artifact), encoding='utf-8')
    monkeypatch.setattr(managed_v2, 'files', lambda _package: tmp_path)

    with pytest.raises(RuntimeError, match='feature contract mismatch'):
        FrozenManagedV2Model.load()


def test_artifact_quote_quality_contract_mismatch_fails_closed(
    tmp_path,
    monkeypatch,
):
    source = Path('app/execution/scoring/models/managed_v2')
    target = tmp_path / 'models' / 'managed_v2'
    shutil.copytree(source, target)
    manifest_path = target / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['provenance']['quote_quality_contract']['change_basis'] = 'wrong'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(managed_v2, 'files', lambda _package: tmp_path)

    with pytest.raises(RuntimeError, match='quote quality contract mismatch'):
        FrozenManagedV2Model.load()


def test_segment_model_term_mismatch_fails_closed(tmp_path, monkeypatch):
    source = Path('app/execution/scoring/models/managed_v2')
    target = tmp_path / 'models' / 'managed_v2'
    shutil.copytree(source, target)
    segment_path = target / 'EQUITY_US_BUY.json'
    artifact = json.loads(segment_path.read_text(encoding='utf-8'))
    artifact['opportunity']['model']['numeric'][0][
        'feature'
    ] = 'foreign_feature'
    segment_path.write_text(json.dumps(artifact), encoding='utf-8')
    monkeypatch.setattr(managed_v2, 'files', lambda _package: tmp_path)

    with pytest.raises(RuntimeError, match='model feature terms'):
        FrozenManagedV2Model.load()


@pytest.mark.parametrize(
    ('mutate', 'message'),
    [
        (
            lambda artifact: artifact['opportunity'].update(
                model_weight=-0.25,
                prior_weight=1.25,
            ),
            'probability.*model_weight',
        ),
        (
            lambda artifact: artifact['path'].update(prior=1.01),
            'probability.*prior',
        ),
        (
            lambda artifact: artifact['economics']['model']['numeric'][0].update(
                coefficient=float('nan')
            ),
            'Non-finite MANAGED V2 numeric value',
        ),
        (
            lambda artifact: artifact['economics'].update(training_rows=0),
            'row count',
        ),
        (
            lambda artifact: artifact['floors'].update(
                opportunity_probability=float('inf')
            ),
            'Non-finite MANAGED V2 numeric value',
        ),
    ],
)
def test_invalid_segment_artifact_numbers_fail_closed(
    tmp_path,
    monkeypatch,
    mutate,
    message,
):
    source = Path('app/execution/scoring/models/managed_v2')
    target = tmp_path / 'models' / 'managed_v2'
    shutil.copytree(source, target)
    segment_path = target / 'EQUITY_US_BUY.json'
    artifact = json.loads(segment_path.read_text(encoding='utf-8'))
    mutate(artifact)
    segment_path.write_text(json.dumps(artifact), encoding='utf-8')
    monkeypatch.setattr(managed_v2, 'files', lambda _package: tmp_path)

    with pytest.raises(RuntimeError, match=message):
        FrozenManagedV2Model.load()


@pytest.mark.parametrize(
    ('metadata_key', 'bad_value', 'message'),
    [
        ('feature_contract_version', 'wrong', 'feature contract mismatch'),
        ('label_contract_version', 'wrong', 'label contract mismatch'),
        (
            'selection_policy_version',
            'wrong',
            'selection_policy_version mismatch',
        ),
        ('segment', 'EQUITY_EU_BUY', 'segment mismatch'),
        ('deployment_status', 'active', 'deployment_status mismatch'),
    ],
)
def test_candidate_provenance_mismatch_fails_closed(
    metadata_key,
    bad_value,
    message,
):
    evaluated = CandidateManagedV2Evaluator().evaluate(
        evaluated_candidate=_evaluated(StrategySegment.EQUITY_US_BUY)
    )
    metadata = dict(evaluated.candidate.managed_v2_metadata)
    metadata[metadata_key] = bad_value
    evaluated = replace(
        evaluated,
        candidate=replace(
            evaluated.candidate,
            managed_v2_metadata=metadata,
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        select_evaluated_trade_candidates(
            [evaluated],
            CandidateSelectionConfig(top_n=1),
            selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
        )


@pytest.mark.parametrize(
    ('field', 'bad_value', 'message'),
    [
        (
            'managed_v2_opportunity_probability',
            float('nan'),
            'opportunity probability is non-finite',
        ),
        (
            'managed_v2_path_probability',
            1.01,
            'path probability is invalid',
        ),
        (
            'managed_v2_expected_net_return_percent',
            float('inf'),
            'economics is non-finite',
        ),
        (
            'managed_v2_ranking_score',
            -0.01,
            'ranking score is invalid',
        ),
    ],
)
def test_invalid_candidate_estimates_fail_closed(field, bad_value, message):
    evaluated = CandidateManagedV2Evaluator().evaluate(
        evaluated_candidate=_evaluated(StrategySegment.EQUITY_US_BUY)
    )
    evaluated = replace(
        evaluated,
        candidate=replace(evaluated.candidate, **{field: bad_value}),
    )

    with pytest.raises(RuntimeError, match=message):
        select_evaluated_trade_candidates(
            [evaluated],
            CandidateSelectionConfig(top_n=1),
            selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
        )


def test_managed_v2_ranking_is_deterministic():
    first = CandidateManagedV2Evaluator().evaluate(
        evaluated_candidate=_evaluated(StrategySegment.EQUITY_US_BUY)
    )
    second = replace(
        first,
        candidate=replace(
            first.candidate,
            candidate_id='candidate-a',
            managed_v2_ranking_score=0.20,
        ),
    )
    third = replace(
        first,
        candidate=replace(
            first.candidate,
            candidate_id='candidate-b',
            managed_v2_ranking_score=0.20,
        ),
    )

    ranked = rank_evaluated_trade_candidates(
        [third, second],
        selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
    )

    assert [item.candidate.candidate_id for item in ranked] == [
        'candidate-a',
        'candidate-b',
    ]


def test_managed_v2_top_n_is_applied_after_independent_component_gates():
    evaluated = CandidateManagedV2Evaluator().evaluate(
        evaluated_candidate=_evaluated(StrategySegment.EQUITY_US_BUY)
    )
    floors = evaluated.candidate.managed_v2_metadata['floors']

    def eligible(candidate_id, ranking_score):
        return replace(
            evaluated,
            candidate=replace(
                evaluated.candidate,
                candidate_id=candidate_id,
                managed_v2_opportunity_probability=(
                    float(floors['opportunity_probability']) + 0.01
                ),
                managed_v2_path_probability=(
                    float(floors['path_probability']) + 0.01
                ),
                managed_v2_expected_net_return_percent=(
                    float(floors['expected_net_return_percent']) + 0.01
                ),
                managed_v2_ranking_score=ranking_score,
            ),
        )

    selection = select_evaluated_trade_candidates(
        [eligible('second', 0.10), eligible('first', 0.20)],
        CandidateSelectionConfig(top_n=1),
        selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
    )

    assert selection.selected_candidates[0].candidate.candidate_id == 'first'
    assert selection.rejected_candidates[0].reason == (
        'candidate_selection_outside_top_n'
    )
    assert selection.rejected_candidates[0].selection_threshold_source == (
        'managed_v2_ranking_top_n'
    )


@pytest.mark.parametrize(
    ('field', 'floor_key', 'reason'),
    [
        (
            'managed_v2_opportunity_probability',
            'opportunity_probability',
            'candidate_selection_opportunity_below_floor',
        ),
        (
            'managed_v2_path_probability',
            'path_probability',
            'candidate_selection_path_below_floor',
        ),
        (
            'managed_v2_expected_net_return_percent',
            'expected_net_return_percent',
            'candidate_selection_economics_below_floor',
        ),
    ],
)
def test_opportunity_path_and_economics_gates_are_independent(
    field,
    floor_key,
    reason,
):
    evaluated = CandidateManagedV2Evaluator().evaluate(
        evaluated_candidate=_evaluated(StrategySegment.EQUITY_US_BUY)
    )
    floors = evaluated.candidate.managed_v2_metadata['floors']
    candidate = replace(
        evaluated.candidate,
        managed_v2_opportunity_probability=(
            float(floors['opportunity_probability']) + 0.01
        ),
        managed_v2_path_probability=(
            float(floors['path_probability']) + 0.01
        ),
        managed_v2_expected_net_return_percent=(
            float(floors['expected_net_return_percent']) + 0.01
        ),
    )
    candidate = replace(
        candidate,
        **{field: float(floors[floor_key]) - 0.01},
    )

    result = select_evaluated_trade_candidates(
        [replace(evaluated, candidate=candidate)],
        CandidateSelectionConfig(top_n=1),
        selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
    )

    assert result.selected_candidates == []
    assert result.rejected_candidates[0].reason == reason
