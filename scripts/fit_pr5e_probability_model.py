from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.execution.scoring.pr5e_model_contract import (
    ACTIVITY_CATEGORICAL_FEATURES,
    ACTIVITY_NUMERIC_FEATURES,
    DIRECTION_CATEGORICAL_FEATURES,
    DIRECTION_NUMERIC_FEATURES,
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
    TRAINING_ASSET_CLASSES,
)

OUTCOME_COLUMN = 'counterfactual_outcome'
MODEL_POPULATION_ACTIONS = {
    'ready_for_selection',
    'wait_for_retest',
    'skip',
}
RANKING_REASONS = {
    'candidate_selection_score_too_low',
    'candidate_selection_outside_top_n',
}
EXPECTED_DATASET_SHA256 = (
    '4608e799936ac007292dcb5cfc1894880893e3f9544d0b7a33096d23b5bb8290'
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Fit and freeze the PR5-E two-stage probability model.'
    )
    parser.add_argument('candidates_csv', type=Path)
    parser.add_argument('output_json', type=Path)
    arguments = parser.parse_args()

    dataset_sha256 = hashlib.sha256(
        arguments.candidates_csv.read_bytes()
    ).hexdigest()
    if dataset_sha256 != EXPECTED_DATASET_SHA256:
        parser.error(
            'Dataset hash does not match the frozen PR5-E v1 cohort. '
            'Create a new model and feature-contract version before fitting '
            'another cohort.'
        )

    raw = pd.read_csv(arguments.candidates_csv, low_memory=False)
    frame = _add_derived_features(raw)
    label_complete = _boolean_series(frame['label_complete'])
    population = frame[
        frame['entry_route_action'].isin(MODEL_POPULATION_ACTIONS)
        & label_complete
        & frame['asset_class'].isin(TRAINING_ASSET_CLASSES)
    ].copy()
    decisive = population[
        population[OUTCOME_COLUMN].isin(('TP_FIRST', 'SL_FIRST'))
    ].copy()

    activity = _logistic_pipeline(
        list(ACTIVITY_NUMERIC_FEATURES),
        list(ACTIVITY_CATEGORICAL_FEATURES),
        regularization=0.10,
    )
    activity.fit(population, population['target_touch'])
    direction = _logistic_pipeline(
        list(DIRECTION_NUMERIC_FEATURES),
        list(DIRECTION_CATEGORICAL_FEATURES),
        regularization=0.01,
    )
    direction.fit(decisive, decisive['target_tp_given_touch'])

    validation, oof_predictions = _leave_one_day_out_validation(
        population
    )
    artifact = {
        'version': OUTCOME_PROBABILITY_MODEL_VERSION,
        'feature_contract_version': (
            OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
        ),
        'training_asset_classes': list(TRAINING_ASSET_CLASSES),
        'activity': _export_pipeline(
            activity,
            list(ACTIVITY_NUMERIC_FEATURES),
            list(ACTIVITY_CATEGORICAL_FEATURES),
        ),
        'direction': _export_pipeline(
            direction,
            list(DIRECTION_NUMERIC_FEATURES),
            list(DIRECTION_CATEGORICAL_FEATURES),
        ),
        'provenance': {
            'cohort_days': sorted(
                str(value)
                for value in population['collection_day'].unique()
            ),
            'dataset_sha256': dataset_sha256,
            'library_versions': {
                'numpy': np.__version__,
                'pandas': pd.__version__,
                'scikit_learn': sklearn.__version__,
            },
            'hyperparameters': {
                'activity_logistic_c': 0.10,
                'direction_logistic_c': 0.01,
                'solver': 'lbfgs',
                'random_state': 42,
            },
            'training_rows': len(population),
            'decisive_training_rows': len(decisive),
            'outcomes': {
                str(name): int(count)
                for name, count
                in population[OUTCOME_COLUMN].value_counts().items()
            },
            'validation': validation,
            'challenger_replay': _challenger_replay(
                frame,
                oof_predictions,
            ),
        },
    }
    arguments.output_json.parent.mkdir(parents=True, exist_ok=True)
    arguments.output_json.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def _add_derived_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    session = result.apply(_session_coordinates, axis=1)
    result['session_minutes'] = session.map(lambda value: value[0])
    result['session_progress'] = session.map(lambda value: value[1])
    result['reward_to_risk_ratio'] = (
        result['effective_take_profit_percent']
        / result['effective_stop_loss_percent'].replace(0, np.nan)
    )
    result['net_reward_to_risk_ratio'] = (
        result['expected_net_profit_percent']
        / (
            result['effective_stop_loss_percent']
            + result['estimated_total_cost_percent']
        ).replace(0, np.nan)
    )
    m5_aligned = result['m5_alignment'].eq('aligned').astype(float)
    consumed = pd.to_numeric(
        result['movement_consumed_to_tp_ratio'],
        errors='coerce',
    )
    result['m5_aligned_x_consumed'] = m5_aligned * consumed
    result['target_touch'] = (
        ~result[OUTCOME_COLUMN].eq('NEITHER')
    ).astype(int)
    result['target_tp_given_touch'] = result[OUTCOME_COLUMN].eq(
        'TP_FIRST'
    ).astype(int)
    return result


def _leave_one_day_out_validation(
    population: pd.DataFrame,
) -> tuple[dict[str, float | str], pd.DataFrame]:
    p_touch = pd.Series(np.nan, index=population.index, dtype=float)
    p_tp = pd.Series(np.nan, index=population.index, dtype=float)
    p_direction = pd.Series(
        np.nan,
        index=population.index,
        dtype=float,
    )
    for held_out_day in sorted(population['collection_day'].unique()):
        train = population[
            population['collection_day'].ne(held_out_day)
        ]
        test = population[
            population['collection_day'].eq(held_out_day)
        ]
        activity = _logistic_pipeline(
            list(ACTIVITY_NUMERIC_FEATURES),
            list(ACTIVITY_CATEGORICAL_FEATURES),
            regularization=0.10,
        )
        activity.fit(train, train['target_touch'])
        touch = _positive_class_probability(activity, test)

        decisive_train = train[
            train[OUTCOME_COLUMN].isin(('TP_FIRST', 'SL_FIRST'))
        ]
        direction = _logistic_pipeline(
            list(DIRECTION_NUMERIC_FEATURES),
            list(DIRECTION_CATEGORICAL_FEATURES),
            regularization=0.01,
        )
        direction.fit(
            decisive_train,
            decisive_train['target_tp_given_touch'],
        )
        conditional = _positive_class_probability(direction, test)
        p_touch.loc[test.index] = touch
        p_tp.loc[test.index] = touch * conditional
        p_direction.loc[test.index] = conditional

    target_tp = population[OUTCOME_COLUMN].eq('TP_FIRST').astype(int)
    decisive = population[OUTCOME_COLUMN].isin(
        ('TP_FIRST', 'SL_FIRST')
    )
    if (
        p_touch.isna().any()
        or p_tp.isna().any()
        or p_direction.isna().any()
    ):
        raise RuntimeError('Out-of-day validation left missing predictions.')
    validation = {
        'method': 'leave_one_collection_day_out',
        'tp_probability_auc': float(
            roc_auc_score(target_tp, p_tp)
        ),
        'tp_probability_brier': float(
            brier_score_loss(target_tp, p_tp)
        ),
        'tp_probability_ece': _fixed_bin_ece(
            target_tp.to_numpy(),
            p_tp.to_numpy(),
        ),
        'direction_auc_tp_vs_sl': float(
            roc_auc_score(
                target_tp[decisive],
                p_direction[decisive],
            )
        ),
    }
    predictions = pd.DataFrame(
        {
            'p_touch': p_touch,
            'p_direction': p_direction,
            'p_tp': p_tp,
        },
        index=population.index,
    )
    return validation, predictions


def _challenger_replay(
    frame: pd.DataFrame,
    predictions: pd.DataFrame,
) -> dict:
    ranking = frame[
        frame['selection_outcome'].eq('selected')
        | frame['selection_reason'].isin(RANKING_REASONS)
    ].copy()
    ranking = ranking.join(predictions, how='left')
    if ranking[['p_touch', 'p_direction', 'p_tp']].isna().any().any():
        raise RuntimeError('Challenger replay has missing OOF predictions.')
    loss = (
        ranking['effective_stop_loss_percent']
        + ranking['estimated_total_cost_percent']
    )
    ranking['direction_break_even'] = loss / (
        ranking['expected_net_profit_percent'] + loss
    )
    ranking['direction_edge'] = (
        ranking['p_direction'] - ranking['direction_break_even']
    )

    selected_indices: list[int] = []
    for _, window in ranking.groupby('window_id', dropna=False):
        asset_class = str(window.iloc[0]['asset_class'])
        limit = 2 if asset_class == 'EQUITY_US' else 1
        selected_indices.extend(
            window.sort_values(
                [
                    'direction_edge',
                    'directional_score',
                    'candidate_id',
                ],
                ascending=[False, False, True],
            )
            .head(limit)
            .index.tolist()
        )
    top_n = ranking.loc[selected_indices]
    selected = top_n[
        top_n['p_tp'].ge(0.10)
        & top_n['p_touch'].lt(0.50)
    ]
    outcomes = selected[OUTCOME_COLUMN].value_counts()
    daily_net_mean = {
        str(day): float(group['counterfactual_net_result_percent'].mean())
        for day, group in selected.groupby('collection_day')
    }
    return {
        'ranking_rows': len(ranking),
        'selection_order': (
            'direction_edge_top_n_then_probability_gates_no_backfill'
        ),
        'minimum_tp_probability': 0.10,
        'maximum_touch_probability_exclusive': 0.50,
        'selected': len(selected),
        'tp_first': int(outcomes.get('TP_FIRST', 0)),
        'sl_first': int(outcomes.get('SL_FIRST', 0)),
        'neither': int(outcomes.get('NEITHER', 0)),
        'net_mean_percent': float(
            selected['counterfactual_net_result_percent'].mean()
        ),
        'net_sum_percent': float(
            selected['counterfactual_net_result_percent'].sum()
        ),
        'daily_net_mean_percent': daily_net_mean,
    }


def _positive_class_probability(
    pipeline: Pipeline,
    frame: pd.DataFrame,
) -> np.ndarray:
    classes = list(pipeline.named_steps['model'].classes_)
    return pipeline.predict_proba(frame)[:, classes.index(1)]


def _fixed_bin_ece(
    target: np.ndarray,
    probability: np.ndarray,
) -> float:
    edges = np.array(
        [
            0.0,
            0.05,
            0.075,
            0.10,
            0.125,
            0.15,
            0.20,
            0.25,
            0.30,
            0.40,
            0.50,
            1.0,
        ]
    )
    labels = pd.cut(
        probability,
        bins=edges,
        include_lowest=True,
        duplicates='drop',
    )
    table = pd.DataFrame(
        {
            'target': target,
            'probability': probability,
            'bin': labels,
        }
    )
    total = len(table)
    return float(
        sum(
            len(group)
            / total
            * abs(
                group['target'].mean()
                - group['probability'].mean()
            )
            for _, group in table.groupby('bin', observed=True)
        )
    )


def _boolean_series(values: pd.Series) -> pd.Series:
    if values.dtype == bool:
        return values
    return values.astype(str).str.strip().str.lower().isin(
        {'1', 'true', 'yes'}
    )


def _session_coordinates(row: pd.Series) -> tuple[float, float]:
    timestamp = pd.to_datetime(row['candidate_timestamp'], utc=True)
    match = re.fullmatch(
        r'[^:]+:'
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}):'
        r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})',
        str(row['session_key']),
    )
    if match is not None:
        start = pd.to_datetime(match.group(1), utc=True)
        end = pd.to_datetime(match.group(2), utc=True)
        duration = (end - start).total_seconds() / 60.0
        minutes = (timestamp - start).total_seconds() / 60.0
        if duration > 0:
            return minutes, float(np.clip(minutes / duration, -0.25, 1.25))
    is_us = str(row['asset_class']) == 'EQUITY_US'
    start_minutes = 13 * 60 + 30 if is_us else 7 * 60
    end_minutes = 20 * 60 if is_us else 15 * 60 + 30
    absolute_minutes = (
        timestamp.hour * 60
        + timestamp.minute
        + timestamp.second / 60.0
    )
    minutes = absolute_minutes - start_minutes
    duration = end_minutes - start_minutes
    return minutes, float(np.clip(minutes / duration, -0.25, 1.25))


def _logistic_pipeline(
    numeric_features: list[str],
    categorical_features: list[str],
    *,
    regularization: float,
) -> Pipeline:
    numeric = Pipeline(
        [
            (
                'imputer',
                SimpleImputer(strategy='median', add_indicator=True),
            ),
            ('scale', StandardScaler()),
        ]
    )
    categorical = Pipeline(
        [
            ('imputer', SimpleImputer(strategy='most_frequent')),
            (
                'one_hot',
                OneHotEncoder(
                    handle_unknown='ignore',
                    min_frequency=10,
                    sparse_output=False,
                ),
            ),
        ]
    )
    return Pipeline(
        [
            (
                'features',
                ColumnTransformer(
                    [
                        ('numeric', numeric, numeric_features),
                        (
                            'categorical',
                            categorical,
                            categorical_features,
                        ),
                    ],
                    remainder='drop',
                    verbose_feature_names_out=True,
                ),
            ),
            (
                'model',
                LogisticRegression(
                    C=regularization,
                    max_iter=5000,
                    solver='lbfgs',
                    random_state=42,
                ),
            ),
        ]
    )


def _export_pipeline(
    pipeline: Pipeline,
    numeric_features: list[str],
    categorical_features: list[str],
) -> dict:
    preprocessor = pipeline.named_steps['features']
    coefficients = pipeline.named_steps['model'].coef_[0]
    numeric_pipeline = preprocessor.named_transformers_['numeric']
    numeric_imputer = numeric_pipeline.named_steps['imputer']
    scaler = numeric_pipeline.named_steps['scale']
    numeric_output_count = len(scaler.mean_)

    value = {
        'intercept': float(
            pipeline.named_steps['model'].intercept_[0]
        ),
        'numeric': [],
        'missing_indicators': [],
        'categorical': [],
    }
    for index, feature in enumerate(numeric_features):
        value['numeric'].append(
            {
                'feature': feature,
                'impute': float(numeric_imputer.statistics_[index]),
                'mean': float(scaler.mean_[index]),
                'scale': float(scaler.scale_[index]),
                'coefficient': float(coefficients[index]),
            }
        )
    for position, feature_index in enumerate(
        numeric_imputer.indicator_.features_
    ):
        coefficient_index = len(numeric_features) + position
        value['missing_indicators'].append(
            {
                'feature': numeric_features[int(feature_index)],
                'mean': float(scaler.mean_[coefficient_index]),
                'scale': float(scaler.scale_[coefficient_index]),
                'coefficient': float(
                    coefficients[coefficient_index]
                ),
            }
        )

    categorical_pipeline = preprocessor.named_transformers_[
        'categorical'
    ]
    categorical_imputer = categorical_pipeline.named_steps['imputer']
    encoder = categorical_pipeline.named_steps['one_hot']
    coefficient_offset = numeric_output_count
    for index, feature in enumerate(categorical_features):
        categories = [
            str(item)
            for item in encoder.categories_[index]
        ]
        raw_infrequent = encoder.infrequent_categories_[index]
        infrequent = (
            []
            if raw_infrequent is None
            else [str(item) for item in raw_infrequent]
        )
        frequent = [
            item for item in categories if item not in set(infrequent)
        ]
        encoded_categories = []
        for category in frequent:
            encoded_categories.append(
                {
                    'category': category,
                    'coefficient': float(
                        coefficients[coefficient_offset]
                    ),
                }
            )
            coefficient_offset += 1
        infrequent_coefficient = None
        if infrequent:
            infrequent_coefficient = float(
                coefficients[coefficient_offset]
            )
            coefficient_offset += 1
        value['categorical'].append(
            {
                'feature': feature,
                'impute': str(
                    categorical_imputer.statistics_[index]
                ),
                'categories': encoded_categories,
                'infrequent_categories': infrequent,
                'infrequent_coefficient': infrequent_coefficient,
            }
        )
    if coefficient_offset != len(coefficients):
        raise RuntimeError(
            'Exported coefficient count does not match fitted model.'
        )
    return value


if __name__ == '__main__':
    main()
