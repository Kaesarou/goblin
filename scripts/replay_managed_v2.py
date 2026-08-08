from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.backtesting.replay_pricing import (
    ReplayFillExtraction,
    ReplayMode,
    historical_fills_from_journal_records,
)
from app.backtesting.stateful_managed_replay import StatefulManagedReplay
from app.execution.scoring.managed_outcome import FrozenManagedOutcomeModel
from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.execution.scoring.managed_v2 import FrozenManagedV2Model
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_DEPLOYMENT_STATUS,
    MANAGED_V2_SELECTION_POLICY_VERSION,
)
from app.journal.serialization import serialize_value
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from scripts.replay_breakeven_profiles import (
    ReplayDayPlan,
    _build_settings,
    _load_candidate_batches,
    _open_archive,
    _parse_plan,
    _run_day,
    _source_provenance,
)

MANAGED_V2_REPLAY_REPORT_SCHEMA_VERSION = 3
MANAGED_V1_LIVE_VALIDATION_SCENARIO = 'managed_v1_live_validation'
MANAGED_V1_HISTORICAL_SCENARIO = 'managed_v1'
MANAGED_V1_QUALITY_MATCHED_SCENARIO = 'managed_v1_quote_quality_v2'
MANAGED_V2_SCENARIO = 'managed_v2'
MANAGED_V2_REPLAY_SCENARIOS = (
    MANAGED_V1_LIVE_VALIDATION_SCENARIO,
    MANAGED_V1_HISTORICAL_SCENARIO,
    MANAGED_V1_QUALITY_MATCHED_SCENARIO,
    MANAGED_V2_SCENARIO,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Replay frozen MANAGED V1 and segment-first V2 policies.'
    )
    parser.add_argument('plan', type=Path)
    parser.add_argument('output_json', type=Path)
    parser.add_argument('--output-markdown', type=Path)
    args = parser.parse_args()

    raw_plan = json.loads(args.plan.read_text(encoding='utf-8'))
    timezone_name = str(raw_plan.get('timezone') or 'Europe/Paris')
    timezone = ZoneInfo(timezone_name)
    days = _parse_plan(raw_plan, base_directory=args.plan.resolve().parent)
    day_reports: list[dict[str, Any]] = []
    profile = BalancedStrategyConfig()
    v1_model = FrozenManagedOutcomeModel.load()
    v2_model = FrozenManagedV2Model.load()

    for day_plan in days:
        print(f'managed-replay-start day={day_plan.day.isoformat()}', flush=True)
        batches, manifests = _load_candidate_batches(day_plan, timezone)
        settings = _build_settings(
            batches=batches,
            manifests=manifests,
            timezone_name=timezone_name,
        )
        historical_fills = _load_historical_fills(day_plan, timezone)
        scenarios = {
            MANAGED_V1_LIVE_VALIDATION_SCENARIO: StatefulManagedReplay(
                settings=settings,
                strategy_profile=profile,
                scenario_name=MANAGED_V1_LIVE_VALIDATION_SCENARIO,
                replay_mode=ReplayMode.LIVE_VALIDATION,
                fill_ledger=historical_fills.ledger(),
                selection_policy_version=MANAGED_SELECTION_POLICY_VERSION,
                apply_quote_quality=False,
            ),
            MANAGED_V1_HISTORICAL_SCENARIO: StatefulManagedReplay(
                settings=settings,
                strategy_profile=profile,
                scenario_name=MANAGED_V1_HISTORICAL_SCENARIO,
                replay_mode=ReplayMode.COUNTERFACTUAL,
                selection_policy_version=MANAGED_SELECTION_POLICY_VERSION,
                apply_quote_quality=False,
            ),
            MANAGED_V1_QUALITY_MATCHED_SCENARIO: StatefulManagedReplay(
                settings=settings,
                strategy_profile=profile,
                scenario_name=MANAGED_V1_QUALITY_MATCHED_SCENARIO,
                replay_mode=ReplayMode.COUNTERFACTUAL,
                selection_policy_version=MANAGED_SELECTION_POLICY_VERSION,
                apply_quote_quality=True,
            ),
            MANAGED_V2_SCENARIO: StatefulManagedReplay(
                settings=settings,
                strategy_profile=profile,
                scenario_name=MANAGED_V2_SCENARIO,
                replay_mode=ReplayMode.COUNTERFACTUAL,
                selection_policy_version=MANAGED_V2_SELECTION_POLICY_VERSION,
                apply_quote_quality=True,
            ),
        }
        _run_day(
            day_plan=day_plan,
            batches=batches,
            scenarios=scenarios,
            timezone=timezone,
        )
        results = {name: replay.result() for name, replay in scenarios.items()}
        day_reports.append(
            {
                'date': day_plan.day.isoformat(),
                'incomplete': day_plan.incomplete,
                'candidate_universe': sum(
                    len(batch.candidates) for batch in batches
                ),
                'historical_fill_extraction': {
                    'position_open_events': (
                        historical_fills.position_open_events
                    ),
                    'broker_entry_fills': historical_fills.broker_entry_fills,
                    'broker_exit_fills': historical_fills.broker_exit_fills,
                    'missing_candidate_ids': (
                        historical_fills.missing_candidate_ids
                    ),
                    'ambiguous_legacy_entry_prices': (
                        historical_fills.ambiguous_legacy_entry_prices
                    ),
                },
                'scenarios': results,
                'comparison': _day_comparison(results),
            }
        )
        print(
            f'managed-replay-complete day={day_plan.day.isoformat()} '
            f"v1_live={results[MANAGED_V1_LIVE_VALIDATION_SCENARIO]['pnl']['realized_net']} "
            f"v1={results[MANAGED_V1_HISTORICAL_SCENARIO]['pnl']['realized_net']} "
            f"v1_quality={results[MANAGED_V1_QUALITY_MATCHED_SCENARIO]['pnl']['realized_net']} "
            f"v2={results[MANAGED_V2_SCENARIO]['pnl']['realized_net']}",
            flush=True,
        )

    aggregates = {
        name: _aggregate(day_reports, name)
        for name in MANAGED_V2_REPLAY_SCENARIOS
    }
    report = {
        'schema_version': MANAGED_V2_REPLAY_REPORT_SCHEMA_VERSION,
        'generated_at': datetime.now().astimezone(),
        'methodology': {
            'modes': [
                ReplayMode.LIVE_VALIDATION.value,
                ReplayMode.COUNTERFACTUAL.value,
            ],
            'candidate_universe': (
                'fixed journaled candidates; quote-quality replay does not '
                'claim to regenerate strategy candles or candidates'
            ),
            'live_validation_scenario': (
                MANAGED_V1_LIVE_VALIDATION_SCENARIO
            ),
            'live_validation_pricing': (
                'recorded broker fills when semantically explicit; otherwise '
                'validation_contractual_fallback, never an inferred broker fill'
            ),
            'live_validation_lifecycle': (
                'explicit historical entries become lifecycle-active at their '
                'recorded entry time; explicit historical exits close at their '
                'recorded fill time; missing fills use the shared lifecycle'
            ),
            'v1_selection_policy': MANAGED_SELECTION_POLICY_VERSION,
            'v2_selection_policy': MANAGED_V2_SELECTION_POLICY_VERSION,
            'entry_price': 'BUY ask; SELL bid',
            'exit_price': 'BUY bid; SELL ask',
            'costs': 'explicit costs once; no second spread deduction',
            'portfolio': (
                'unchanged cooldown, top-N, capacity and no-backfill contracts'
            ),
            'v1_quote_quality': 'historical accepted stream',
            'v1_quality_matched_quote_quality': (
                'quote_quality_v2 reapplied before lifecycle'
            ),
            'v2_quote_quality': 'quote_quality_v2 reapplied before lifecycle',
            'primary_comparison': (
                'managed_v2 versus managed_v1_quote_quality_v2; identical '
                'quote-quality policy isolates selection-policy effects'
            ),
        },
        'models': {
            'v1_version': v1_model.version,
            'v1_sha256': v1_model.artifact_sha256,
            'v2_version': v2_model.version,
            'v2_sha256': v2_model.artifact_sha256,
            'v2_provenance': dict(v2_model.provenance),
            'v2_deployment_status': MANAGED_V2_DEPLOYMENT_STATUS,
        },
        'source_provenance': _source_provenance(days),
        'historical_fill_extraction': _aggregate_fill_extraction(
            day_reports
        ),
        'days': day_reports,
        'aggregates': aggregates,
        'comparison': _aggregate_comparison(aggregates),
    }
    serializable = serialize_value(report)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(serializable, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(
            _markdown(serializable),
            encoding='utf-8',
        )


def _day_comparison(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    primary = _scenario_comparison(
        results[MANAGED_V1_QUALITY_MATCHED_SCENARIO],
        results[MANAGED_V2_SCENARIO],
    )
    return {
        **primary,
        'reference_scenario': MANAGED_V1_QUALITY_MATCHED_SCENARIO,
        'historical_v1_reference': _scenario_comparison(
            results[MANAGED_V1_HISTORICAL_SCENARIO],
            results[MANAGED_V2_SCENARIO],
        ),
    }


def _load_historical_fills(
    day_plan: ReplayDayPlan,
    timezone: ZoneInfo,
) -> ReplayFillExtraction:
    records: list[dict[str, Any]] = []
    event_markers = (
        b'position_opened',
        b'broker_close_fill_resolved',
        b'position_close_confirmed',
    )
    for source in day_plan.sources:
        reader, archive = _open_archive(source)
        try:
            with (
                archive.open(source.trades_member) as zipped,
                gzip.GzipFile(fileobj=zipped) as stream,
            ):
                for raw_line in stream:
                    if not any(marker in raw_line for marker in event_markers):
                        continue
                    record = json.loads(raw_line)
                    occurred_at = datetime.fromisoformat(
                        str(record['timestamp'])
                    )
                    if occurred_at.astimezone(timezone).date() != day_plan.day:
                        continue
                    records.append(record)
        finally:
            archive.close()
            reader.close()
    return historical_fills_from_journal_records(records)


def _scenario_comparison(
    reference: dict[str, Any],
    challenger: dict[str, Any],
) -> dict[str, Any]:
    reference_ids = {
        item['candidate_id'] for item in reference['trades']['details']
    }
    challenger_ids = {
        item['candidate_id'] for item in challenger['trades']['details']
    }
    return {
        'realized_net_delta': round(
            challenger['pnl']['realized_net']
            - reference['pnl']['realized_net'],
            4,
        ),
        'trade_count_delta': (
            challenger['trades']['count'] - reference['trades']['count']
        ),
        'overlap': len(reference_ids & challenger_ids),
        'v2_added': sorted(challenger_ids - reference_ids),
        'v2_removed': sorted(reference_ids - challenger_ids),
    }


def _aggregate(
    days: list[dict[str, Any]],
    scenario: str,
) -> dict[str, Any]:
    results = [day['scenarios'][scenario] for day in days]
    trades = [
        item
        for result in results
        for item in result['trades']['details']
    ]
    return {
        'realized_gross': round(
            sum(result['pnl']['realized_gross'] for result in results),
            4,
        ),
        'explicit_costs': round(
            sum(
                result['pnl']['explicit_costs_deducted']
                for result in results
            ),
            4,
        ),
        'realized_net': round(
            sum(result['pnl']['realized_net'] for result in results),
            4,
        ),
        'mark_to_market_net': round(
            sum(result['pnl']['mark_to_market_net'] for result in results),
            4,
        ),
        'max_realized_drawdown': _drawdown(trades),
        'max_intraday_equity_drawdown': max(
            (
                result['pnl']['max_intraday_equity_drawdown']
                for result in results
            ),
            default=0.0,
        ),
        'trade_count': len(trades),
        'by_segment': _by_segment(trades),
        'by_day': {
            day['date']: day['scenarios'][scenario]['pnl']['realized_net']
            for day in days
        },
        'by_close_reason': dict(
            Counter(item['close_reason'] for item in trades)
        ),
        'average_cost_per_trade': (
            round(
                sum(item['explicit_costs_deducted'] for item in trades)
                / len(trades),
                4,
            )
            if trades
            else None
        ),
        'average_mfe_percent': _mean(trades, 'mfe_percent'),
        'average_mae_percent': _mean(trades, 'mae_percent'),
        'by_entry_price_provenance': dict(
            Counter(item['entry_price_provenance'] for item in trades)
        ),
        'by_exit_price_provenance': dict(
            Counter(item['exit_price_provenance'] for item in trades)
        ),
        'prevented_by_capacity': sum(
            result['constraints'].get('prevented_by_capacity', 0)
            for result in results
        ),
        'prevented_by_cooldown': sum(
            result['constraints'].get('prevented_by_cooldown', 0)
            for result in results
        ),
        'quote_quality_quarantined': sum(
            result['constraints'].get('quote_quality_quarantined', 0)
            for result in results
        ),
        'trade_ids': sorted(item['candidate_id'] for item in trades),
    }


def _aggregate_fill_extraction(
    days: list[dict[str, Any]],
) -> dict[str, int]:
    keys = (
        'position_open_events',
        'broker_entry_fills',
        'broker_exit_fills',
        'missing_candidate_ids',
        'ambiguous_legacy_entry_prices',
    )
    return {
        key: sum(
            int(day['historical_fill_extraction'][key]) for day in days
        )
        for key in keys
    }


def _by_segment(trades: list[dict[str, Any]]) -> dict[str, Any]:
    segments = sorted(
        {f"{item['asset_class']}_{item['side']}" for item in trades}
    )
    return {
        segment: {
            'trades': len(items),
            'net_pnl': round(sum(item['net_pnl'] for item in items), 4),
            'gross_pnl': round(sum(item['gross_pnl'] for item in items), 4),
            'explicit_costs': round(
                sum(item['explicit_costs_deducted'] for item in items),
                4,
            ),
        }
        for segment in segments
        for items in [[
            item
            for item in trades
            if f"{item['asset_class']}_{item['side']}" == segment
        ]]
    }


def _aggregate_comparison(aggregates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    primary = _aggregate_scenario_comparison(
        aggregates[MANAGED_V1_QUALITY_MATCHED_SCENARIO],
        aggregates[MANAGED_V2_SCENARIO],
    )
    return {
        **primary,
        'reference_scenario': MANAGED_V1_QUALITY_MATCHED_SCENARIO,
        'historical_v1_reference': _aggregate_scenario_comparison(
            aggregates[MANAGED_V1_HISTORICAL_SCENARIO],
            aggregates[MANAGED_V2_SCENARIO],
        ),
    }


def _aggregate_scenario_comparison(
    reference: dict[str, Any],
    challenger: dict[str, Any],
) -> dict[str, Any]:
    reference_ids = set(reference['trade_ids'])
    challenger_ids = set(challenger['trade_ids'])
    return {
        'realized_net_delta': round(
            challenger['realized_net'] - reference['realized_net'],
            4,
        ),
        'trade_count_delta': (
            challenger['trade_count'] - reference['trade_count']
        ),
        'overlap': len(reference_ids & challenger_ids),
        'v2_added': len(challenger_ids - reference_ids),
        'v2_removed': len(reference_ids - challenger_ids),
        'v2_added_candidate_ids': sorted(challenger_ids - reference_ids),
        'v2_removed_candidate_ids': sorted(reference_ids - challenger_ids),
    }


def _drawdown(trades: list[dict[str, Any]]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for trade in sorted(trades, key=lambda item: item['closed_at']):
        cumulative += float(trade['net_pnl'])
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return round(maximum, 4)


def _mean(trades: list[dict[str, Any]], field: str) -> float | None:
    return (
        round(sum(float(item[field]) for item in trades) / len(trades), 4)
        if trades
        else None
    )


def _markdown(report: dict[str, Any]) -> str:
    comparison = report['comparison']
    empirical_status = (
        'REJET EMPIRIQUE — V2 reste strictement en shadow.'
        if comparison['realized_net_delta'] < 0
        else 'AUCUNE PROMOTION AUTOMATIQUE — validation shadow requise.'
    )
    lines = [
        '# Replay MANAGED V1 vs V2',
        '',
        f'**Statut : {empirical_status}**',
        '',
        (
            'Le comparatif primaire oppose V2 à V1 avec le même filtre '
            '`quote_quality_v2`. L’univers de candidats est celui des '
            'journaux ; le replay ne prétend pas régénérer les bougies ni '
            'les candidats.'
        ),
        '',
        '| Politique | Trades | Brut | Coûts | Net | Drawdown |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for name in MANAGED_V2_REPLAY_SCENARIOS:
        item = report['aggregates'][name]
        lines.append(
            f"| {name} | {item['trade_count']} | {item['realized_gross']:.4f} "
            f"| {item['explicit_costs']:.4f} | {item['realized_net']:.4f} "
            f"| {item['max_realized_drawdown']:.4f} |"
        )
    lines.extend(
        [
            '',
            '## Validation des prix broker',
            '',
            _fill_coverage_markdown(report),
            '',
            '## Effet isolé de la qualité des cotations',
            '',
            *_quote_quality_effect_markdown(report),
            '',
            '## Par segment',
            '',
            '| Segment | V1 trades | V1 net | V2 trades | V2 net |',
            '|---|---:|---:|---:|---:|',
        ]
    )
    segments = sorted(
        set(
            report['aggregates'][MANAGED_V1_QUALITY_MATCHED_SCENARIO][
                'by_segment'
            ]
        )
        | set(report['aggregates'][MANAGED_V2_SCENARIO]['by_segment'])
    )
    for segment in segments:
        v1 = report['aggregates'][MANAGED_V1_QUALITY_MATCHED_SCENARIO][
            'by_segment'
        ].get(
            segment,
            {'trades': 0, 'net_pnl': 0.0},
        )
        v2 = report['aggregates'][MANAGED_V2_SCENARIO]['by_segment'].get(
            segment,
            {'trades': 0, 'net_pnl': 0.0},
        )
        lines.append(
            f"| {segment} | {v1['trades']} | {v1['net_pnl']:.4f} "
            f"| {v2['trades']} | {v2['net_pnl']:.4f} |"
        )
    lines.extend(
        [
            '',
            '## Par journée',
            '',
            *_daily_results_markdown(report),
            '',
            '## Sélections',
            '',
            f"- Référence primaire : {comparison['reference_scenario']}",
            f"- Overlap : {comparison['overlap']}",
            f"- Ajoutés par V2 : {comparison['v2_added']}",
            f"- Retirés par V2 : {comparison['v2_removed']}",
            f"- Delta net : {comparison['realized_net_delta']:+.4f}",
            '',
        ]
    )

    return '\n'.join(lines)


def _daily_results_markdown(report: dict[str, Any]) -> list[str]:
    lines = [
        (
            '| Date | V1 validation live | V1 | V1 + qualité | V2 | '
            'Delta primaire |'
        ),
        '|---|---:|---:|---:|---:|---:|',
    ]
    has_incomplete_day = False
    for day in report['days']:
        scenarios = day['scenarios']
        incomplete = bool(day.get('incomplete'))
        has_incomplete_day = has_incomplete_day or incomplete
        date_label = f"{day['date']}{'*' if incomplete else ''}"
        lines.append(
            f'| {date_label} | '
            f"{scenarios[MANAGED_V1_LIVE_VALIDATION_SCENARIO]['pnl']['realized_net']:.4f} | "
            f"{scenarios[MANAGED_V1_HISTORICAL_SCENARIO]['pnl']['realized_net']:.4f} | "
            f"{scenarios[MANAGED_V1_QUALITY_MATCHED_SCENARIO]['pnl']['realized_net']:.4f} | "
            f"{scenarios[MANAGED_V2_SCENARIO]['pnl']['realized_net']:.4f} | "
            f"{day['comparison']['realized_net_delta']:+.4f} |"
        )
    if has_incomplete_day:
        lines.extend(
            [
                '',
                (
                    '\\* Journée incomplète : le tableau présente le net '
                    'réalisé. Le mark-to-market et les positions encore '
                    'ouvertes restent explicites dans le rapport JSON.'
                ),
            ]
        )
    return lines


def _fill_coverage_markdown(report: dict[str, Any]) -> str:
    fills = report['historical_fill_extraction']
    openings = int(fills['position_open_events'])
    entries = int(fills['broker_entry_fills'])
    exits = int(fills['broker_exit_fills'])
    entry_coverage = entries / openings * 100 if openings else 0.0
    exit_coverage = exits / openings * 100 if openings else 0.0
    return (
        f'{openings} ouvertures historiques ; {entries} entrées broker '
        f'explicites ({entry_coverage:.1f} %) et {exits} sorties broker '
        f'explicites ({exit_coverage:.1f} %). Les '
        f"{fills['ambiguous_legacy_entry_prices']} prix legacy ambigus restent "
        'des fallbacks contractuels nommés.'
    )


def _quote_quality_effect_markdown(report: dict[str, Any]) -> list[str]:
    historical = report['aggregates'][MANAGED_V1_HISTORICAL_SCENARIO]
    quality_matched = report['aggregates'][
        MANAGED_V1_QUALITY_MATCHED_SCENARIO
    ]
    historical_ids = set(historical['trade_ids'])
    quality_ids = set(quality_matched['trade_ids'])
    selection_changes = len(historical_ids ^ quality_ids)
    net_delta = round(
        float(quality_matched['realized_net'])
        - float(historical['realized_net']),
        4,
    )
    selection_summary = (
        'aucun identifiant de trade ne change'
        if selection_changes == 0
        else f'{selection_changes} identifiants de trades changent'
    )
    lines = [
        (
            f"{quality_matched['quote_quality_quarantined']} observations de "
            'cotation ont été mises en quarantaine. Sur cet univers '
            f'journalisé, {selection_summary} et le net V1 varie de '
            f'{net_delta:+.4f}.'
        ),
    ]
    changed_outcomes: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for day in report['days']:
        scenarios = day['scenarios']
        historical_trades = {
            trade['candidate_id']: trade
            for trade in scenarios[MANAGED_V1_HISTORICAL_SCENARIO]['trades'][
                'details'
            ]
        }
        quality_trades = {
            trade['candidate_id']: trade
            for trade in scenarios[MANAGED_V1_QUALITY_MATCHED_SCENARIO][
                'trades'
            ]['details']
        }
        for candidate_id in sorted(historical_trades.keys() & quality_trades):
            before = historical_trades[candidate_id]
            after = quality_trades[candidate_id]
            if round(float(after['net_pnl']) - float(before['net_pnl']), 4):
                changed_outcomes.append((day['date'], before, after))
    if not changed_outcomes:
        lines.append('Aucune issue de trade V1 n’est modifiée.')
        return lines
    lines.extend(['', 'Issues V1 effectivement modifiées :', ''])
    for day, before, after in changed_outcomes:
        outcome_delta = round(
            float(after['net_pnl']) - float(before['net_pnl']),
            4,
        )
        lines.append(
            f"- {day} `{before['symbol']}` (`{before['candidate_id']}`) : "
            f"`{before['close_reason']}` {float(before['net_pnl']):.4f} vers "
            f"`{after['close_reason']}` {float(after['net_pnl']):.4f}, "
            f'delta {outcome_delta:+.4f}.'
        )
    return lines


if __name__ == '__main__':
    main()
