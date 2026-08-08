from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Floating-point reductions performed by BLAS can vary with thread scheduling.
# The frozen calibration contract requires byte-for-byte reproducible artifacts,
# so calibration always runs with a single numerical worker.
for _thread_environment_variable in (
    'BLIS_NUM_THREADS',
    'MKL_NUM_THREADS',
    'NUMEXPR_NUM_THREADS',
    'OMP_NUM_THREADS',
    'OPENBLAS_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS',
):
    os.environ[_thread_environment_variable] = '1'

import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, roc_auc_score

from app.backtesting.managed_v2_labels import ManagedV2LifecycleLabeler
from app.execution.candidate_economics import CandidateEconomicsEstimator
from app.execution.candidate_readiness import CandidateReadiness
from app.execution.position_tracker import PositionTracker
from app.execution.scoring.frozen_logistic import FrozenLogisticModel
from app.execution.scoring.managed_v2_features import (
    ManagedV2FeatureSet,
    extract_managed_v2_features,
)
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_ARTIFACT_SCHEMA_VERSION,
    MANAGED_V2_ECONOMICS_MODEL_VERSION,
    MANAGED_V2_FEATURE_CONTRACT_VERSION,
    MANAGED_V2_FLOOR_POLICY_VERSION,
    MANAGED_V2_LABEL_CONTRACT_VERSION,
    MANAGED_V2_MODEL_VERSION,
    MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
    MANAGED_V2_PATH_MODEL_VERSION,
    MANAGED_V2_SELECTION_POLICY_VERSION,
    MANAGED_V2_SUPPORTED_SEGMENTS,
    MANAGED_V2_TRAINING_ASSET_CLASSES,
    feature_names_for,
)
from app.execution.scoring.outcome_probability import (
    CandidateOutcomeProbabilityEvaluator,
)
from app.execution.scoring.tp_feasibility import (
    CandidateTpFeasibilityEvaluator,
)
from app.execution.strategy_segment import StrategySegment
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.market.data_quality import (
    MarketDataStatus,
    MarketDataValidator,
    quote_quality_contract_metadata,
)
from app.market.relative_spread import (
    build_relative_spread_context,
    compact_relative_spread_history,
)
from app.risk.position_sizing import FixedPercentPositionSizing
from app.risk.risk_manager import RiskManager
from app.runtime.candidate_flow import attach_entry_decisions
from app.runtime.trading_session_window import (
    trading_session_service_from_settings,
)
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from scripts.replay_breakeven_profiles import (
    ReplayDayPlan,
    _build_settings,
    _load_candidate_batches,
    _market_events,
    _parse_plan,
    _source_provenance,
)

CALIBRATION_RANDOM_STATE = 20260808
CALIBRATION_LOGISTIC_C = 0.15
CALIBRATION_RIDGE_ALPHA = 20.0
CALIBRATION_RIDGE_SOLVER = 'svd'
CALIBRATION_MODEL_WEIGHT = 0.75
CALIBRATION_PRIOR_WEIGHT = 0.25


@dataclass
class _CandidateRow:
    candidate_id: str
    day: str
    segment: StrategySegment
    features: ManagedV2FeatureSet
    opportunity: int | None = None
    path_quality: int | None = None
    net_return_percent: float | None = None
    close_reason: str | None = None
    mfe_percent: float | None = None
    mae_percent: float | None = None


@dataclass(frozen=True)
class _PreparedMatrix:
    values: np.ndarray
    imputes: np.ndarray
    means: np.ndarray
    scales: np.ndarray
    missing_names: tuple[str, ...]
    missing_means: np.ndarray
    missing_scales: np.ndarray


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Fit the frozen segment-first MANAGED V2 artifact.'
    )
    parser.add_argument('plan', type=Path)
    parser.add_argument('artifact_directory', type=Path)
    parser.add_argument('report_json', type=Path)
    parser.add_argument('--dataset-json', type=Path)
    parser.add_argument('--input-dataset-json', type=Path)
    parser.add_argument(
        '--train-through',
        type=date.fromisoformat,
        default=date(2026, 7, 28),
    )
    args = parser.parse_args()

    raw_plan = json.loads(args.plan.read_text(encoding='utf-8'))
    timezone_name = str(raw_plan.get('timezone') or 'Europe/Paris')
    timezone = ZoneInfo(timezone_name)
    days = _parse_plan(raw_plan, base_directory=args.plan.resolve().parent)
    if args.input_dataset_json is None:
        rows, extraction = _extract_rows(days, timezone, timezone_name)
    else:
        rows = [
            _row_from_payload(item)
            for item in json.loads(
                args.input_dataset_json.read_text(encoding='utf-8')
            )
        ]
        extraction = {
            'reused_dataset_sha256': _dataset_sha256(rows),
            'rows': len(rows),
        }
    training = [
        row for row in rows if date.fromisoformat(row.day) <= args.train_through
    ]
    validation = [
        row for row in rows if date.fromisoformat(row.day) > args.train_through
    ]
    if not training or not validation:
        raise RuntimeError('MANAGED V2 requires non-empty train and validation sets.')

    source = _source_provenance(days)
    dataset_sha = _dataset_sha256(rows)
    artifacts, train_predictions = _fit_segments(training)
    manifest = _manifest(
        artifacts=artifacts,
        training=training,
        dataset_sha256=dataset_sha,
        source_provenance=source,
        plan=days,
    )
    _write_artifact(args.artifact_directory, manifest, artifacts)
    artifact_sha256, artifact_file_sha256 = _artifact_hashes(
        args.artifact_directory,
        manifest,
    )
    validation_predictions = _predict_rows(validation, artifacts)
    report = {
        'schema_version': MANAGED_V2_ARTIFACT_SCHEMA_VERSION,
        'model_version': MANAGED_V2_MODEL_VERSION,
        'feature_contract_version': MANAGED_V2_FEATURE_CONTRACT_VERSION,
        'label_contract_version': MANAGED_V2_LABEL_CONTRACT_VERSION,
        'selection_policy_version': MANAGED_V2_SELECTION_POLICY_VERSION,
        'artifact_sha256': artifact_sha256,
        'artifact_file_sha256': artifact_file_sha256,
        'dataset_sha256': dataset_sha,
        'training_dates': sorted({row.day for row in training}),
        'validation_dates': sorted({row.day for row in validation}),
        'training_rows': len(training),
        'validation_rows': len(validation),
        'extraction': extraction,
        'training_metrics_oof': _metrics(training, train_predictions),
        'validation_metrics': _metrics(validation, validation_predictions),
        'segments': {
            segment.value: {
                'training_rows': sum(
                    row.segment is segment for row in training
                ),
                'validation_rows': sum(
                    row.segment is segment for row in validation
                ),
                'floors': artifacts[segment]['floors'],
                'training_status': artifacts[segment]['training_status'],
            }
            for segment in MANAGED_V2_SUPPORTED_SEGMENTS
        },
        'source_provenance': source,
        'quote_quality_contract': quote_quality_contract_metadata(),
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    if args.dataset_json is not None:
        args.dataset_json.parent.mkdir(parents=True, exist_ok=True)
        args.dataset_json.write_text(
            json.dumps([_row_payload(row) for row in rows], sort_keys=True)
            + '\n',
            encoding='utf-8',
        )


def _extract_rows(
    days: list[ReplayDayPlan],
    timezone: ZoneInfo,
    timezone_name: str,
) -> tuple[list[_CandidateRow], dict[str, Any]]:
    rows: list[_CandidateRow] = []
    counters: Counter[str] = Counter()
    by_day: dict[str, dict[str, int]] = {}
    for day_plan in days:
        print(f'label-start day={day_plan.day.isoformat()}', flush=True)
        batches, manifests = _load_candidate_batches(day_plan, timezone)
        settings = _build_settings(
            batches=batches,
            manifests=manifests,
            timezone_name=timezone_name,
        )
        profile = BalancedStrategyConfig()
        registry = InstrumentRegistry(
            settings,
            instrument_configs=profile.instrument_configs,
        )
        risk = RiskManager(settings, FixedPercentPositionSizing(), registry)
        economics = CandidateEconomicsEstimator(
            FixedPercentPositionSizing(),
            registry,
        )
        tp = CandidateTpFeasibilityEvaluator()
        outcome = CandidateOutcomeProbabilityEvaluator()
        session = trading_session_service_from_settings(settings)
        labeler = ManagedV2LifecycleLabeler()
        validator = MarketDataValidator()
        history: dict[str, deque] = defaultdict(deque)
        pending: dict[str, _CandidateRow] = {}
        batch_index = 0
        recent_keys: deque[tuple[Any, ...]] = deque()
        recent_set: set[tuple[Any, ...]] = set()
        processed = 0
        streams = [
            _market_events(source, day_plan.day, timezone, index)
            for index, source in enumerate(day_plan.sources)
        ]
        for occurred_at, _, sequence, snapshot in heapq.merge(*streams):
            while (
                batch_index < len(batches)
                and batches[batch_index].occurred_at <= occurred_at
            ):
                batch = batches[batch_index]
                for candidate in batch.candidates:
                    prepared = _prepare_candidate(
                        candidate=candidate,
                        account_equity=batch.account_equity,
                        occurred_at=batch.occurred_at,
                        prior_snapshots=list(history[candidate.symbol]),
                        registry=registry,
                        risk=risk,
                        economics=economics,
                        tp=tp,
                        outcome=outcome,
                    )
                    if isinstance(prepared, str):
                        counters[prepared] += 1
                        continue
                    row, position = prepared
                    if row.candidate_id in pending:
                        counters['duplicate_candidate_id'] += 1
                        continue
                    pending[row.candidate_id] = row
                    labeler.add(
                        candidate_id=row.candidate_id,
                        position=position,
                    )
                    counters['label_candidates'] += 1
                for snapshots in history.values():
                    compact_relative_spread_history(snapshots)
                batch_index += 1

            key = (
                snapshot.symbol,
                snapshot.timestamp,
                snapshot.bid,
                snapshot.ask,
                snapshot.last,
            )
            if key in recent_set:
                counters['duplicate_market_snapshot'] += 1
                continue
            recent_keys.append(key)
            recent_set.add(key)
            if len(recent_keys) > 20_000:
                recent_set.discard(recent_keys.popleft())

            try:
                quality_config = registry.config_for(
                    snapshot.symbol
                ).market_data_quality
            except ValueError:
                processed += 1
                continue
            validation = validator.validate(
                snapshot,
                quality_config,
                now=occurred_at,
            )
            if validation.status is not MarketDataStatus.ACCEPTED:
                counters[f'quote_{validation.status.value}'] += 1
                processed += 1
                continue
            asset_class = registry.resolve(snapshot.symbol).asset_class
            session_decision = session.evaluate(
                asset_class=asset_class,
                now=occurred_at,
            )
            labeler.on_snapshot(
                snapshot,
                force_close=session_decision.force_close_required,
            )
            history[snapshot.symbol].append(snapshot)
            for labels in labeler.consume_completed():
                row = pending.pop(labels.candidate_id)
                row.opportunity = labels.opportunity
                row.path_quality = labels.path_quality
                row.net_return_percent = labels.net_return_percent
                row.close_reason = labels.close_reason
                row.mfe_percent = labels.mfe_percent
                row.mae_percent = labels.mae_percent
                rows.append(row)
            processed += 1
            if processed % 250_000 == 0:
                print(
                    f'label-progress day={day_plan.day.isoformat()} '
                    f'market_events={processed} sequence={sequence}',
                    flush=True,
                )
        counters['unresolved_at_day_end'] += len(
            labeler.pending_candidate_ids()
        )
        by_day[day_plan.day.isoformat()] = {
            'candidate_universe': sum(
                len(batch.candidates) for batch in batches
            ),
            'labeled_rows': sum(
                row.day == day_plan.day.isoformat() for row in rows
            ),
            'unresolved': len(labeler.pending_candidate_ids()),
        }
        print(
            f'label-complete day={day_plan.day.isoformat()} '
            f"rows={by_day[day_plan.day.isoformat()]['labeled_rows']}",
            flush=True,
        )
    return rows, {'counts': dict(counters), 'by_day': by_day}


def _prepare_candidate(
    *,
    candidate,
    account_equity: float,
    occurred_at: datetime,
    prior_snapshots: list,
    registry: InstrumentRegistry,
    risk: RiskManager,
    economics: CandidateEconomicsEstimator,
    tp: CandidateTpFeasibilityEvaluator,
    outcome: CandidateOutcomeProbabilityEvaluator,
):
    asset_class = registry.resolve(candidate.symbol).asset_class
    if asset_class not in {AssetClass.EQUITY_EU, AssetClass.EQUITY_US}:
        return 'non_equity'
    segment = StrategySegment.from_asset_and_side(
        asset_class,
        candidate.signal.action,
    )
    prior = [
        snapshot
        for snapshot in prior_snapshots
        if snapshot.timestamp < candidate.snapshot.timestamp
    ]
    spread = build_relative_spread_context(
        current=candidate.snapshot,
        prior_snapshots=prior,
    )
    context = (
        None
        if candidate.market_context is None
        else replace(candidate.market_context, spread=spread)
    )
    candidate = replace(
        candidate,
        segment=segment,
        market_context=context,
    )
    evaluated = economics.evaluate(candidate, account_equity)
    evaluated = tp.evaluate(
        evaluated_candidate=evaluated,
        risk_profile=risk.risk_profile_for(candidate.symbol),
    )
    evaluated = attach_entry_decisions([evaluated])[0]
    evaluated = outcome.evaluate(
        evaluated_candidate=evaluated,
        risk_profile=risk.risk_profile_for(candidate.symbol),
    )
    if evaluated.entry_decision is None:
        return 'entry_decision_missing'
    if evaluated.entry_decision.action.value != 'ready_for_selection':
        return f'entry_{evaluated.entry_decision.action.value}'
    if evaluated.readiness == CandidateReadiness.REJECT:
        return 'readiness_reject'
    candidate = evaluated.candidate
    if candidate.tp_feasibility_hard_rejection_reason is not None:
        return 'tp_feasibility_reject'
    if candidate.touch_probability is None:
        return 'touch_probability_missing'
    if (
        evaluated.economics.expected_net_profit_percent
        < evaluated.economics.min_expected_net_profit_percent
    ):
        return 'hard_economics_reject'
    plan = risk.evaluate(
        signal=candidate.signal,
        snapshot=candidate.snapshot,
        account_equity=account_equity,
        session_key=candidate.session_key,
        effective_sl_tp=evaluated.effective_sl_tp,
    )
    if not plan.approved:
        return f'risk_reject:{plan.reason}'
    entry = candidate.snapshot.executable_entry_price(candidate.signal.action)
    adjusted = risk.adjust_trade_plan_to_entry_price(
        trade_plan=plan,
        entry_price=entry,
    )
    tracker = PositionTracker()
    position = tracker.record_open_position(
        position_id=f'label:{candidate.candidate_id}',
        trade_plan=adjusted,
        signal_price=candidate.snapshot.last,
        executable_entry_estimate=entry,
        broker_entry_fill_price=None,
        opened_at=occurred_at,
    )
    return (
        _CandidateRow(
            candidate_id=candidate.candidate_id,
            day=occurred_at.date().isoformat(),
            segment=segment,
            features=extract_managed_v2_features(evaluated),
        ),
        position,
    )


def _fit_segments(
    rows: list[_CandidateRow],
) -> tuple[
    dict[StrategySegment, dict[str, Any]],
    dict[str, dict[str, float]],
]:
    artifacts: dict[StrategySegment, dict[str, Any]] = {}
    oof_predictions: dict[str, dict[str, float]] = {}
    for segment in MANAGED_V2_SUPPORTED_SEGMENTS:
        segment_rows = [row for row in rows if row.segment is segment]
        if not segment_rows:
            raise RuntimeError(f'No MANAGED V2 training rows for {segment.value}.')
        days = sorted({row.day for row in segment_rows})
        for held_out in days:
            fit_rows = [row for row in segment_rows if row.day != held_out]
            holdout = [row for row in segment_rows if row.day == held_out]
            opportunity_model = _fit_probability(
                fit_rows,
                feature_names_for(segment, 'opportunity'),
                label='opportunity',
                version=MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
            )
            path_model = _fit_probability(
                [row for row in fit_rows if row.path_quality is not None],
                feature_names_for(segment, 'path'),
                label='path_quality',
                version=MANAGED_V2_PATH_MODEL_VERSION,
            )
            for row in holdout:
                oof_predictions[row.candidate_id] = {
                    'opportunity': _predict_probability(
                        opportunity_model,
                        row.features.opportunity,
                    ),
                    'path': _predict_probability(
                        path_model,
                        row.features.path,
                    ),
                }
        opportunity_model = _fit_probability(
            segment_rows,
            feature_names_for(segment, 'opportunity'),
            label='opportunity',
            version=MANAGED_V2_OPPORTUNITY_MODEL_VERSION,
        )
        path_rows = [row for row in segment_rows if row.path_quality is not None]
        path_model = _fit_probability(
            path_rows,
            feature_names_for(segment, 'path'),
            label='path_quality',
            version=MANAGED_V2_PATH_MODEL_VERSION,
        )
        economics_rows = [
            row
            for row in segment_rows
            if row.opportunity == 1
            and row.path_quality == 1
            and row.net_return_percent is not None
        ]
        for held_out in days:
            fit_economics_rows = [
                row
                for row in economics_rows
                if row.day != held_out
            ]
            holdout_economics_rows = [
                row
                for row in economics_rows
                if row.day == held_out
            ]
            held_out_economics_model = _fit_linear(
                [
                    row.features.economics(
                        opportunity_probability=oof_predictions[
                            row.candidate_id
                        ]['opportunity'],
                        path_probability=oof_predictions[row.candidate_id][
                            'path'
                        ],
                    )
                    for row in fit_economics_rows
                ],
                [
                    float(row.net_return_percent)
                    for row in fit_economics_rows
                ],
                feature_names_for(segment, 'economics'),
            )
            for row in holdout_economics_rows:
                features = row.features.economics(
                    opportunity_probability=oof_predictions[
                        row.candidate_id
                    ]['opportunity'],
                    path_probability=oof_predictions[row.candidate_id][
                        'path'
                    ],
                )
                oof_predictions[row.candidate_id]['economics'] = (
                    _clamp_economics_prediction(
                        _predict_linear(held_out_economics_model, features),
                        row.features.raw,
                    )
                )
        economics_features = [
            row.features.economics(
                opportunity_probability=oof_predictions[row.candidate_id][
                    'opportunity'
                ],
                path_probability=oof_predictions[row.candidate_id]['path'],
            )
            for row in economics_rows
        ]
        economics_model = _fit_linear(
            economics_features,
            [float(row.net_return_percent) for row in economics_rows],
            feature_names_for(segment, 'economics'),
        )
        opportunity_prior = float(opportunity_model['prior'])
        path_prior = float(path_model['prior'])
        artifacts[segment] = {
            'segment': segment.value,
            'training_status': _training_status(
                opportunity_model,
                path_model,
                economics_model,
            ),
            'features': {
                component: list(feature_names_for(segment, component))
                for component in ('opportunity', 'path', 'economics')
            },
            'floors': {
                'policy_version': MANAGED_V2_FLOOR_POLICY_VERSION,
                'opportunity_probability': round(
                    min(0.60, max(0.35, opportunity_prior)),
                    6,
                ),
                'path_probability': round(
                    min(0.70, max(0.50, path_prior)),
                    6,
                ),
                'expected_net_return_percent': 0.0,
            },
            'opportunity': opportunity_model,
            'path': path_model,
            'economics': economics_model,
        }
    return artifacts, oof_predictions


def _fit_probability(
    rows: list[_CandidateRow],
    feature_names: tuple[str, ...],
    *,
    label: str,
    version: str,
) -> dict[str, Any]:
    labels = np.asarray([int(getattr(row, label)) for row in rows], dtype=int)
    feature_maps = [
        row.features.opportunity
        if label == 'opportunity'
        else row.features.path
        for row in rows
    ]
    matrix = _prepare_matrix(feature_maps, feature_names)
    positive_rows = int(labels.sum()) if len(labels) else 0
    prior = (
        positive_rows / len(labels)
        if len(labels)
        else 0.5
    )
    trainable = len(labels) >= 25 and len(set(labels.tolist())) == 2
    if trainable:
        model = LogisticRegression(
            C=CALIBRATION_LOGISTIC_C,
            max_iter=2_000,
            random_state=CALIBRATION_RANDOM_STATE,
        ).fit(matrix.values, labels)
        intercept = float(model.intercept_[0])
        coefficients = model.coef_[0]
        model_weight = CALIBRATION_MODEL_WEIGHT
        prior_weight = CALIBRATION_PRIOR_WEIGHT
        status = 'trained'
    else:
        intercept = _logit(prior)
        coefficients = np.zeros(matrix.values.shape[1], dtype=float)
        model_weight = 0.0
        prior_weight = 1.0
        status = 'intercept_only'
    return {
        'version': version,
        'training_status': status,
        'training_rows': len(labels),
        'positive_rows': positive_rows,
        'prior': prior,
        'model_weight': model_weight,
        'prior_weight': prior_weight,
        'model': _model_payload(
            intercept,
            coefficients,
            feature_names,
            matrix,
        ),
    }


def _fit_linear(
    feature_maps: list[dict[str, float | None]],
    labels: list[float],
    feature_names: tuple[str, ...],
) -> dict[str, Any]:
    matrix = _prepare_matrix(feature_maps, feature_names)
    values = np.asarray(labels, dtype=float)
    if len(values) >= 10:
        model = Ridge(
            alpha=CALIBRATION_RIDGE_ALPHA,
            solver=CALIBRATION_RIDGE_SOLVER,
        ).fit(
            matrix.values,
            values,
        )
        intercept = float(model.intercept_)
        coefficients = model.coef_
        status = 'trained'
    else:
        intercept = float(values.mean()) if len(values) else 0.0
        coefficients = np.zeros(matrix.values.shape[1], dtype=float)
        status = 'intercept_only'
    return {
        'version': MANAGED_V2_ECONOMICS_MODEL_VERSION,
        'training_status': status,
        'training_rows': len(values),
        'model': _model_payload(
            intercept,
            coefficients,
            feature_names,
            matrix,
        ),
    }


def _prepare_matrix(
    feature_maps: list[dict[str, float | None]],
    feature_names: tuple[str, ...],
) -> _PreparedMatrix:
    if not feature_maps:
        feature_maps = [{}]
    raw = np.asarray(
        [
            [
                np.nan
                if features.get(name) is None
                else float(features[name])
                for name in feature_names
            ]
            for features in feature_maps
        ],
        dtype=float,
    )
    imputes = np.asarray(
        [
            float(np.nanmedian(raw[:, index]))
            if not np.isnan(raw[:, index]).all()
            else 0.0
            for index in range(len(feature_names))
        ]
    )
    imputed = np.where(np.isnan(raw), imputes, raw)
    means = imputed.mean(axis=0)
    scales = imputed.std(axis=0)
    scales = np.where(scales <= 1e-12, 1.0, scales)
    normalized = (imputed - means) / scales
    missing_names = tuple(
        name
        for index, name in enumerate(feature_names)
        if np.isnan(raw[:, index]).any()
    )
    if missing_names:
        missing = np.asarray(
            [
                [1.0 if features.get(name) is None else 0.0 for name in missing_names]
                for features in feature_maps
            ]
        )
        missing_means = missing.mean(axis=0)
        missing_scales = missing.std(axis=0)
        missing_scales = np.where(missing_scales <= 1e-12, 1.0, missing_scales)
        normalized_missing = (missing - missing_means) / missing_scales
        values = np.hstack((normalized, normalized_missing))
    else:
        missing_means = np.asarray([], dtype=float)
        missing_scales = np.asarray([], dtype=float)
        values = normalized
    return _PreparedMatrix(
        values=values,
        imputes=imputes,
        means=means,
        scales=scales,
        missing_names=missing_names,
        missing_means=missing_means,
        missing_scales=missing_scales,
    )


def _model_payload(
    intercept: float,
    coefficients: np.ndarray,
    feature_names: tuple[str, ...],
    matrix: _PreparedMatrix,
) -> dict[str, Any]:
    numeric_count = len(feature_names)
    return {
        'intercept': intercept,
        'numeric': [
            {
                'feature': name,
                'coefficient': float(coefficients[index]),
                'impute': float(matrix.imputes[index]),
                'mean': float(matrix.means[index]),
                'scale': float(matrix.scales[index]),
            }
            for index, name in enumerate(feature_names)
        ],
        'missing_indicators': [
            {
                'feature': name,
                'coefficient': float(coefficients[numeric_count + index]),
                'mean': float(matrix.missing_means[index]),
                'scale': float(matrix.missing_scales[index]),
            }
            for index, name in enumerate(matrix.missing_names)
        ],
        'categorical': [],
    }


def _predict_probability(
    component: dict[str, Any],
    features: dict[str, float | None],
) -> float:
    raw = FrozenLogisticModel.from_dict(component['model']).predict(features)
    return min(
        1.0,
        max(
            0.0,
            float(component['model_weight']) * raw
            + float(component['prior_weight']) * float(component['prior']),
        ),
    )


def _predict_linear(
    component: dict[str, Any],
    features: dict[str, float | None],
) -> float:
    model = component['model']
    result = float(model['intercept'])
    for term in model['numeric']:
        raw = features.get(term['feature'])
        value = float(term['impute']) if raw is None else float(raw)
        result += (
            (value - float(term['mean']))
            / float(term['scale'])
            * float(term['coefficient'])
        )
    for term in model.get('missing_indicators', ()):
        value = 1.0 if features.get(term['feature']) is None else 0.0
        result += (
            (value - float(term['mean']))
            / float(term['scale'])
            * float(term['coefficient'])
        )
    return result


def _predict_rows(
    rows: list[_CandidateRow],
    artifacts: dict[StrategySegment, dict[str, Any]],
) -> dict[str, dict[str, float]]:
    predictions: dict[str, dict[str, float]] = {}
    for row in rows:
        artifact = artifacts[row.segment]
        opportunity = _predict_probability(
            artifact['opportunity'],
            dict(row.features.opportunity),
        )
        path = _predict_probability(
            artifact['path'],
            dict(row.features.path),
        )
        economics_features = row.features.economics(
            opportunity_probability=opportunity,
            path_probability=path,
        )
        economics = _predict_linear(
            artifact['economics'],
            economics_features,
        )
        predictions[row.candidate_id] = {
            'opportunity': opportunity,
            'path': path,
            'economics': _clamp_economics_prediction(
                economics,
                row.features.raw,
            ),
        }
    return predictions


def _clamp_economics_prediction(
    value: float,
    raw_features: dict[str, float | None],
) -> float:
    """Apply the same plan bounds as the frozen runtime estimator."""

    cost = max(
        0.0,
        _required_finite_feature(
            raw_features,
            'estimated_total_cost_percent',
        ),
    )
    stop = max(
        0.0,
        _required_finite_feature(
            raw_features,
            'effective_stop_loss_percent',
        ),
    )
    take_profit = _required_finite_feature(
        raw_features,
        'effective_take_profit_percent',
    )
    lower = -(stop + cost)
    upper = max(lower, take_profit - cost)
    return min(upper, max(lower, value))


def _required_finite_feature(
    features: dict[str, float | None],
    name: str,
) -> float:
    value = features.get(name)
    if value is None or not math.isfinite(float(value)):
        raise RuntimeError(
            f'MANAGED V2 economics bound feature is unavailable: {name}.'
        )
    return float(value)


def _metrics(
    rows: list[_CandidateRow],
    predictions: dict[str, dict[str, float]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for segment in MANAGED_V2_SUPPORTED_SEGMENTS:
        items = [row for row in rows if row.segment is segment]
        opportunity_labels = [int(row.opportunity) for row in items]
        opportunity_predictions = [
            predictions[row.candidate_id]['opportunity'] for row in items
        ]
        path_items = [row for row in items if row.path_quality is not None]
        path_labels = [int(row.path_quality) for row in path_items]
        path_predictions = [
            predictions[row.candidate_id]['path'] for row in path_items
        ]
        economics_items = [
            row
            for row in items
            if row.opportunity == 1
            and row.path_quality == 1
            and row.net_return_percent is not None
        ]
        expected = [
            predictions[row.candidate_id].get('economics')
            for row in economics_items
            if predictions[row.candidate_id].get('economics') is not None
        ]
        realized = [
            float(row.net_return_percent)
            for row in economics_items
        ]
        result[segment.value] = {
            'rows': len(items),
            'opportunity': _classification_metrics(
                opportunity_labels,
                opportunity_predictions,
            ),
            'path': _classification_metrics(
                path_labels,
                path_predictions,
            ),
            'economics': {
                'rows': len(realized),
                'expected_mean_percent': _mean(expected),
                'realized_mean_percent': _mean(realized),
                'mae_percent': _mean(
                    [abs(left - right) for left, right in zip(expected, realized)]
                ),
                'correlation': _correlation(expected, realized),
            },
            'close_reasons': dict(
                Counter(row.close_reason for row in items if row.close_reason)
            ),
        }
    return result


def _classification_metrics(
    labels: list[int],
    predictions: list[float],
) -> dict[str, Any]:
    auc = (
        float(roc_auc_score(labels, predictions))
        if labels and len(set(labels)) == 2
        else None
    )
    return {
        'rows': len(labels),
        'positive_rate': _mean([float(value) for value in labels]),
        'predicted_mean': _mean(predictions),
        'auc': auc,
        'brier': (
            float(brier_score_loss(labels, predictions)) if labels else None
        ),
        'calibration': _calibration_bins(labels, predictions),
    }


def _calibration_bins(
    labels: list[int],
    predictions: list[float],
) -> list[dict[str, Any]]:
    bins = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        upper = lower + 0.2
        indices = [
            index
            for index, prediction in enumerate(predictions)
            if lower <= prediction < upper
            or (upper >= 1.0 and prediction == 1.0)
        ]
        if not indices:
            continue
        bins.append(
            {
                'lower': lower,
                'upper': upper,
                'rows': len(indices),
                'predicted_mean': _mean([predictions[index] for index in indices]),
                'observed_rate': _mean([float(labels[index]) for index in indices]),
            }
        )
    return bins


def _manifest(
    *,
    artifacts: dict[StrategySegment, dict[str, Any]],
    training: list[_CandidateRow],
    dataset_sha256: str,
    source_provenance: dict[str, Any],
    plan: list[ReplayDayPlan],
) -> dict[str, Any]:
    return {
        'schema_version': MANAGED_V2_ARTIFACT_SCHEMA_VERSION,
        'version': MANAGED_V2_MODEL_VERSION,
        'feature_contract_version': MANAGED_V2_FEATURE_CONTRACT_VERSION,
        'label_contract_version': MANAGED_V2_LABEL_CONTRACT_VERSION,
        'selection_policy_version': MANAGED_V2_SELECTION_POLICY_VERSION,
        'training_asset_classes': list(MANAGED_V2_TRAINING_ASSET_CLASSES),
        'supported_segments': [
            segment.value for segment in MANAGED_V2_SUPPORTED_SEGMENTS
        ],
        'provenance': {
            'training_dates': sorted({row.day for row in training}),
            'training_rows': len(training),
            'dataset_sha256': dataset_sha256,
            'source_provenance_sha256': _canonical_sha256(source_provenance),
            'source_runs': sorted(source_provenance['runs']),
            'incomplete_training_dates': sorted(
                day.day.isoformat()
                for day in plan
                if day.incomplete
                and any(row.day == day.day.isoformat() for row in training)
            ),
            'labels': {
                'opportunity': (
                    'executable MFE reaches the unchanged live breakeven '
                    'trigger within the lifecycle horizon, including the '
                    'counterfactual path after an earlier initial stop'
                ),
                'path': (
                    'conditional on Opportunity=1, the protection threshold '
                    'is reached strictly before the initial stop'
                ),
                'economics': (
                    'conditional on Opportunity=1 and Path=1, net lifecycle '
                    'return using side-aware executable prices and explicit '
                    'costs once; no second spread deduction'
                ),
            },
            'regularization': {
                'logistic_C': CALIBRATION_LOGISTIC_C,
                'ridge_alpha': CALIBRATION_RIDGE_ALPHA,
                'ridge_solver': CALIBRATION_RIDGE_SOLVER,
                'model_weight': CALIBRATION_MODEL_WEIGHT,
                'segment_prior_weight': CALIBRATION_PRIOR_WEIGHT,
                'random_state': CALIBRATION_RANDOM_STATE,
                'numerical_threads': 1,
            },
            'floor_policy': (
                'training segment prior clipped to [0.35,0.60] for '
                'Opportunity and [0.50,0.70] for Path; Economics fixed at '
                '0.00% net; validation dates never consulted'
            ),
            'quote_quality_contract': quote_quality_contract_metadata(),
            'continuous_training': False,
            'automatic_retuning': False,
        },
        'segment_files': {
            segment.value: f'{segment.value}.json'
            for segment in artifacts
        },
    }


def _write_artifact(
    directory: Path,
    manifest: dict[str, Any],
    artifacts: dict[StrategySegment, dict[str, Any]],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob('*.json'):
        old.unlink()
    for segment, artifact in artifacts.items():
        (directory / f'{segment.value}.json').write_text(
            json.dumps(artifact, separators=(',', ':'), sort_keys=True) + '\n',
            encoding='utf-8',
        )
    (directory / 'manifest.json').write_text(
        json.dumps(manifest, separators=(',', ':'), sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _artifact_hashes(
    directory: Path,
    manifest: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    ordered_names = [
        'manifest.json',
        *(
            str(manifest['segment_files'][segment.value])
            for segment in MANAGED_V2_SUPPORTED_SEGMENTS
        ),
    ]
    payloads = {
        name: (directory / name).read_bytes()
        for name in ordered_names
    }
    digest = hashlib.sha256()
    for name in ordered_names:
        digest.update(payloads[name])
    return (
        digest.hexdigest(),
        {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in payloads.items()
        },
    )


def _training_status(*components: dict[str, Any]) -> str:
    return (
        'trained'
        if all(component['training_status'] == 'trained' for component in components)
        else 'contains_intercept_only_component'
    )


def _dataset_sha256(rows: list[_CandidateRow]) -> str:
    return _canonical_sha256([_row_payload(row) for row in rows])


def _row_payload(row: _CandidateRow) -> dict[str, Any]:
    return {
        'candidate_id': row.candidate_id,
        'day': row.day,
        'segment': row.segment.value,
        'features': {
            'opportunity': dict(row.features.opportunity),
            'path': dict(row.features.path),
            'raw': dict(row.features.raw),
        },
        'labels': {
            'opportunity': row.opportunity,
            'path_quality': row.path_quality,
            'net_return_percent': row.net_return_percent,
            'close_reason': row.close_reason,
            'mfe_percent': row.mfe_percent,
            'mae_percent': row.mae_percent,
        },
    }


def _row_from_payload(value: dict[str, Any]) -> _CandidateRow:
    features = value['features']
    labels = value['labels']
    return _CandidateRow(
        candidate_id=str(value['candidate_id']),
        day=str(value['day']),
        segment=StrategySegment(value['segment']),
        features=ManagedV2FeatureSet(
            segment=StrategySegment(value['segment']),
            opportunity=dict(features['opportunity']),
            path=dict(features['path']),
            raw=dict(features['raw']),
        ),
        opportunity=int(labels['opportunity']),
        path_quality=(
            None
            if labels.get('path_quality') is None
            else int(labels['path_quality'])
        ),
        net_return_percent=(
            None
            if labels.get('net_return_percent') is None
            else float(labels['net_return_percent'])
        ),
        close_reason=labels.get('close_reason'),
        mfe_percent=(
            None
            if labels.get('mfe_percent') is None
            else float(labels['mfe_percent'])
        ),
        mae_percent=(
            None
            if labels.get('mae_percent') is None
            else float(labels['mae_percent'])
        ),
    )


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        separators=(',', ':'),
        sort_keys=True,
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _logit(value: float) -> float:
    clipped = min(1 - 1e-6, max(1e-6, value))
    return math.log(clipped / (1 - clipped))


def _mean(values: list[float]) -> float | None:
    return None if not values else float(sum(values) / len(values))


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(right) < 2:
        return None
    if np.std(left) <= 1e-12 or np.std(right) <= 1e-12:
        return None
    return float(np.corrcoef(left, right)[0, 1])


if __name__ == '__main__':
    main()
