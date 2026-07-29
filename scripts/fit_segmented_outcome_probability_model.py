from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.execution.scoring.outcome_probability_model_contract import (
    ACTIVITY_CATEGORICAL_FEATURES,
    ACTIVITY_NUMERIC_FEATURES,
    DIRECTION_FEATURES_BY_SEGMENT,
    MINIMUM_DIRECTION_EDGE,
    OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION,
    OUTCOME_PROBABILITY_MODEL_VERSION,
    SUPPORTED_DIRECTION_SEGMENTS,
    TRAINING_ASSET_CLASSES,
)

EXPECTED_ACTIVITY_DATASET_SHA256 = (
    '4613e958702210226e9b397be2449e5e311f77e3dccc04d8c0cf81be0ceb1628'
)
EXPECTED_DIRECTION_DATASET_SHA256 = (
    '54314272bbdfe5ecff83a7a9eae99ec9c36c4e547a73145f8ce5241923d7cb06'
)
MODEL_WEIGHT = 0.5
SEGMENT_PRIOR_WEIGHT = 0.5
DIRECTION_SETTINGS = {
    'EQUITY_EU_BUY': ('core', 0.1),
    'EQUITY_EU_SELL': ('mtf', 0.01),
    'EQUITY_US_BUY': ('geometry', 1.0),
    'EQUITY_US_SELL': ('mtf', 1.0),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            'Fit the frozen segmented outcome-probability V2 artifact. '
            'The activity model remains the exact PR5-E P_TOUCH model; '
            'only P_DIRECTION is refitted on the five-day all-candidate cohort.'
        )
    )
    parser.add_argument('activity_candidates_csv', type=Path)
    parser.add_argument('direction_candidates_csv', type=Path)
    parser.add_argument('output_json', type=Path)
    arguments = parser.parse_args()

    _require_hash(
        arguments.activity_candidates_csv,
        EXPECTED_ACTIVITY_DATASET_SHA256,
        'activity',
    )
    _require_hash(
        arguments.direction_candidates_csv,
        EXPECTED_DIRECTION_DATASET_SHA256,
        'direction',
    )

    activity_frame = pd.read_csv(
        arguments.activity_candidates_csv,
        low_memory=False,
    )
    direction_frame = _prepare_direction_frame(
        pd.read_csv(arguments.direction_candidates_csv, low_memory=False)
    )

    activity = _activity_pipeline()
    activity.fit(activity_frame, activity_frame['target_touch'])
    direction_frame['touch_probability'] = _positive_probability(
        activity,
        direction_frame,
    )

    segments: dict[str, dict] = {}
    for segment, (feature_family, regularization) in (
        DIRECTION_SETTINGS.items()
    ):
        asset_class, side = segment.rsplit('_', 1)
        decisive = direction_frame[
            direction_frame['asset_class'].eq(asset_class)
            & direction_frame['side'].eq(side)
            & direction_frame['outcome'].isin(('TP_FIRST', 'SL_FIRST'))
        ].copy()
        numeric, categorical = DIRECTION_FEATURES_BY_SEGMENT[segment]
        model = _direction_pipeline(
            list(numeric),
            list(categorical),
            regularization=regularization,
        )
        model.fit(decisive, decisive['target_direction'])
        segments[segment] = {
            'feature_family': feature_family,
            'training_status': 'trained',
            'source_segment': None,
            'training_rows': len(decisive),
            'segment_prior': float(decisive['target_direction'].mean()),
            'model_weight': MODEL_WEIGHT,
            'segment_prior_weight': SEGMENT_PRIOR_WEIGHT,
            'model': _export_pipeline(
                model,
                list(numeric),
                list(categorical),
                add_missing_indicators=False,
            ),
        }

    for side in ('BUY', 'SELL'):
        source = f'EQUITY_US_{side}'
        target = f'CRYPTO_{side}'
        transferred = json.loads(json.dumps(segments[source]))
        transferred.update(
            training_status='provisional_transfer',
            source_segment=source,
            training_rows=0,
        )
        segments[target] = transferred

    if tuple(segments) != SUPPORTED_DIRECTION_SEGMENTS:
        raise RuntimeError('Generated segment order does not match contract.')

    artifact = {
        'version': OUTCOME_PROBABILITY_MODEL_VERSION,
        'feature_contract_version': (
            OUTCOME_PROBABILITY_FEATURE_CONTRACT_VERSION
        ),
        'training_asset_classes': list(TRAINING_ASSET_CLASSES),
        'supported_segments': list(SUPPORTED_DIRECTION_SEGMENTS),
        'activity': _export_pipeline(
            activity,
            list(ACTIVITY_NUMERIC_FEATURES),
            list(ACTIVITY_CATEGORICAL_FEATURES),
            add_missing_indicators=True,
        ),
        'direction_segments': segments,
        'provenance': {
            'dataset_sha256': EXPECTED_DIRECTION_DATASET_SHA256,
            'activity_dataset_sha256': EXPECTED_ACTIVITY_DATASET_SHA256,
            'cohort_days': sorted(
                str(item) for item in direction_frame['day'].unique()
            ),
            'training_rows': len(direction_frame),
            'decisive_training_rows': int(
                direction_frame['outcome'].isin(
                    ('TP_FIRST', 'SL_FIRST')
                ).sum()
            ),
            'outcomes': {
                str(name): int(count)
                for name, count
                in direction_frame['outcome'].value_counts().items()
            },
            'activity_frozen_from': 'outcome_probability_v1',
            'activity_training_days': [
                '2026-07-22',
                '2026-07-23',
                '2026-07-24',
            ],
            'activity_training_rows': len(activity_frame),
            'direction_training_days': sorted(
                str(item) for item in direction_frame['day'].unique()
            ),
            'direction_margin': MINIMUM_DIRECTION_EDGE,
            'calibration_shrinkage': {
                'model_weight': MODEL_WEIGHT,
                'segment_prior_weight': SEGMENT_PRIOR_WEIGHT,
            },
            'library_versions': {
                'numpy': np.__version__,
                'pandas': pd.__version__,
                'scikit_learn': sklearn.__version__,
            },
            'validation': {
                'segmented_direction_cross_day_auc': 0.643258051846033,
                'segmented_direction_cross_day_brier': 0.2016480614208268,
                'strict_external_auc': 0.588,
                'strict_external_brier': 0.217,
            },
            'crypto': {
                'status': 'provisional_transfer',
                'training_rows': 0,
                'buy_source': 'EQUITY_US_BUY',
                'sell_source': 'EQUITY_US_SELL',
            },
        },
    }
    arguments.output_json.parent.mkdir(parents=True, exist_ok=True)
    encoded_artifact = json.dumps(
        artifact,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    wrapper = {
        'encoding': 'gzip+base64',
        'payload': base64.b64encode(
            gzip.compress(encoded_artifact, compresslevel=9, mtime=0)
        ).decode('ascii'),
    }
    arguments.output_json.write_text(
        json.dumps(wrapper, separators=(',', ':')) + '\n',
        encoding='utf-8',
    )


def _require_hash(path: Path, expected: str, label: str) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(
            f'{label} dataset hash {actual} does not match frozen cohort '
            f'{expected}. Create a new model version for another cohort.'
        )


def _prepare_direction_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result['effective_take_profit_percent'] = result['tp_percent']
    result['effective_stop_loss_percent'] = result['sl_percent']
    result['horizon_minutes'] = np.select(
        [
            result['profile_key'].eq('eu_trend_buy_v1'),
            result['asset_class'].eq('EQUITY_US'),
        ],
        [180.0, 60.0],
        default=60.0,
    )
    side_sign = np.where(result['side'].eq('BUY'), 1.0, -1.0)
    for source, target in (
        ('session_move_percent', 'aligned_session_move'),
        ('snapshot_momentum_percent', 'aligned_snapshot_momentum'),
        ('benchmark_momentum_percent', 'aligned_benchmark_momentum'),
        (
            'symbol_relative_strength_percent',
            'aligned_symbol_relative_strength',
        ),
        ('m5_return', 'aligned_m5_return'),
        ('m15_return', 'aligned_m15_return'),
        ('m5_velocity', 'aligned_m5_velocity'),
        ('m15_velocity', 'aligned_m15_velocity'),
        ('m5_acceleration', 'aligned_m5_acceleration'),
        ('m15_acceleration', 'aligned_m15_acceleration'),
    ):
        result[target] = (
            pd.to_numeric(result[source], errors='coerce') * side_sign
        )
    result['close_quality'] = np.where(
        result['side'].eq('BUY'),
        result['close_position_percent'],
        100.0 - result['close_position_percent'],
    )
    result['target_direction'] = result['outcome'].eq('TP_FIRST').astype(int)
    return result


def _activity_pipeline() -> Pipeline:
    numeric = Pipeline(
        [
            ('imputer', SimpleImputer(strategy='median', add_indicator=True)),
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
    return _pipeline(
        numeric,
        categorical,
        list(ACTIVITY_NUMERIC_FEATURES),
        list(ACTIVITY_CATEGORICAL_FEATURES),
        regularization=0.1,
    )


def _direction_pipeline(
    numeric_features: list[str],
    categorical_features: list[str],
    *,
    regularization: float,
) -> Pipeline:
    numeric = Pipeline(
        [
            ('imputer', SimpleImputer(strategy='median')),
            ('scale', StandardScaler()),
        ]
    )
    categorical = Pipeline(
        [
            ('imputer', SimpleImputer(strategy='most_frequent')),
            (
                'one_hot',
                OneHotEncoder(handle_unknown='ignore', sparse_output=False),
            ),
        ]
    )
    return _pipeline(
        numeric,
        categorical,
        numeric_features,
        categorical_features,
        regularization=regularization,
    )


def _pipeline(
    numeric: Pipeline,
    categorical: Pipeline,
    numeric_features: list[str],
    categorical_features: list[str],
    *,
    regularization: float,
) -> Pipeline:
    return Pipeline(
        [
            (
                'features',
                ColumnTransformer(
                    [
                        ('numeric', numeric, numeric_features),
                        ('categorical', categorical, categorical_features),
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


def _positive_probability(model: Pipeline, frame: pd.DataFrame) -> np.ndarray:
    classes = list(model.named_steps['model'].classes_)
    return model.predict_proba(frame)[:, classes.index(1)]


def _export_pipeline(
    pipeline: Pipeline,
    numeric_features: list[str],
    categorical_features: list[str],
    *,
    add_missing_indicators: bool,
) -> dict:
    preprocessor = pipeline.named_steps['features']
    coefficients = pipeline.named_steps['model'].coef_[0]
    numeric_pipeline = preprocessor.named_transformers_['numeric']
    numeric_imputer = numeric_pipeline.named_steps['imputer']
    scaler = numeric_pipeline.named_steps['scale']
    value = {
        'intercept': float(pipeline.named_steps['model'].intercept_[0]),
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
    if add_missing_indicators:
        for position, feature_index in enumerate(
            numeric_imputer.indicator_.features_
        ):
            coefficient_index = len(numeric_features) + position
            value['missing_indicators'].append(
                {
                    'feature': numeric_features[int(feature_index)],
                    'mean': float(scaler.mean_[coefficient_index]),
                    'scale': float(scaler.scale_[coefficient_index]),
                    'coefficient': float(coefficients[coefficient_index]),
                }
            )

    categorical_pipeline = preprocessor.named_transformers_['categorical']
    categorical_imputer = categorical_pipeline.named_steps['imputer']
    encoder = categorical_pipeline.named_steps['one_hot']
    coefficient_offset = len(scaler.mean_)
    for index, feature in enumerate(categorical_features):
        categories = [str(item) for item in encoder.categories_[index]]
        raw_infrequent = getattr(encoder, 'infrequent_categories_', None)
        infrequent = (
            []
            if raw_infrequent is None or raw_infrequent[index] is None
            else [str(item) for item in raw_infrequent[index]]
        )
        frequent = [
            item for item in categories if item not in set(infrequent)
        ]
        encoded = []
        for category in frequent:
            encoded.append(
                {
                    'category': category,
                    'coefficient': float(coefficients[coefficient_offset]),
                }
            )
            coefficient_offset += 1
        infrequent_coefficient = None
        if infrequent:
            infrequent_coefficient = float(coefficients[coefficient_offset])
            coefficient_offset += 1
        value['categorical'].append(
            {
                'feature': feature,
                'impute': str(categorical_imputer.statistics_[index]),
                'categories': encoded,
                'infrequent_categories': infrequent,
                'infrequent_coefficient': infrequent_coefficient,
            }
        )
    if coefficient_offset != len(coefficients):
        raise RuntimeError('Frozen coefficient count does not match model.')
    return value


if __name__ == '__main__':
    main()
