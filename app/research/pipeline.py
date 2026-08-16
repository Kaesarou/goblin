from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from app.instruments.models import AssetClass
from app.journal.jsonl_journal import JsonlJournal
from app.market.market_context import SideNeutralResearchMarketContext
from app.market.models import MarketSnapshot, PriceSource
from app.market.timeframes import Timeframe
from app.research.microstructure import (
    MICROSTRUCTURE_CONTRACT_VERSION,
    MICROSTRUCTURE_FEATURE_NAMES,
    EtoroMicrostructureAccumulator,
)
from app.research.payload_schema_observer import EtoroPayloadSchemaObserver
from app.research.research_state import (
    RESEARCH_BASE_FEATURE_NAMES,
    RESEARCH_CAUSAL_CUTOFF_CONVENTION,
    RESEARCH_SAMPLING_CADENCE_MINUTES,
    RESEARCH_STATE_CONTRACT_VERSION,
    RESEARCH_STATE_SCHEMA_VERSION,
    build_candle_research_features,
)
from app.research.summary import (
    empty_research_summary,
    write_research_summary,
)

logger = logging.getLogger(__name__)

RESEARCH_EVENT_TYPE = 'research_state'
RESEARCH_LATEST_QUOTE_MAX_OBSERVATIONS_PER_SYMBOL = 4096
RESEARCH_FEATURE_NAMES = (
    *RESEARCH_BASE_FEATURE_NAMES,
    *MICROSTRUCTURE_FEATURE_NAMES,
)
RESEARCH_FEATURE_SET_SHA256 = hashlib.sha256(
    json.dumps(
        RESEARCH_FEATURE_NAMES,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()


@dataclass(frozen=True)
class ResearchBoundaryResult:
    state_at: datetime
    expected_state_count: int
    emitted_state_count: int
    skipped_not_tradable_count: int
    state_calculation_failure_count: int
    journal_write_failure_count: int
    calculation_duration_ms: float
    persistence_duration_ms: float
    total_duration_ms: float


@dataclass(frozen=True)
class _BuiltResearchState:
    symbol: str
    state_id: str
    record: dict[str, Any]


class SideNeutralResearchPipeline:
    """Read-only sidecar for market-state and eToro schema research."""

    def __init__(
        self,
        *,
        research_symbols: Mapping[str, AssetClass],
        asset_class_by_symbol: Mapping[str, AssetClass],
        journal: JsonlJournal,
        payload_schema_observer: EtoroPayloadSchemaObserver,
        market_context_service,
        multi_timeframe_service,
        collection_started_at: datetime | None = None,
        run_id: str | None = None,
        summary_path: str | None = None,
    ) -> None:
        self.research_symbols = {
            symbol.strip().upper(): asset_class
            for symbol, asset_class in research_symbols.items()
            if asset_class in {AssetClass.EQUITY_EU, AssetClass.EQUITY_US}
        }
        self.asset_class_by_symbol = {
            symbol.strip().upper(): asset_class
            for symbol, asset_class in asset_class_by_symbol.items()
        }
        self.journal = journal
        self.payload_schema_observer = payload_schema_observer
        self.market_context_service = market_context_service
        self.multi_timeframe_service = multi_timeframe_service
        self.run_id = run_id or getattr(journal, 'run_id', None) or 'unknown'
        self.summary_path = summary_path
        self.collection_started_at = (
            datetime.min.replace(tzinfo=UTC)
            if collection_started_at is None
            else _as_utc(collection_started_at)
        )
        self.microstructure = EtoroMicrostructureAccumulator()
        self._accepted_snapshots: dict[
            str,
            deque[tuple[MarketSnapshot, str]],
        ] = defaultdict(
            lambda: deque(
                maxlen=RESEARCH_LATEST_QUOTE_MAX_OBSERVATIONS_PER_SYMBOL
            )
        )
        self._emitted_state_ids_by_symbol: dict[str, set[str]] = {}
        self._processed_boundaries: set[datetime] = set()
        self.failure_count = 0
        self.journal_failure_count = 0
        self.expected_state_count = 0
        self.emitted_state_count = 0
        self.skipped_not_tradable_count = 0
        self.duplicate_prevented_count = 0
        self.state_calculation_failure_count = 0
        self.states_missing_quote_count = 0
        self.states_missing_candle_count = 0
        self.states_with_micro_10s_count = 0
        self.states_with_micro_30s_count = 0
        self.states_with_micro_60s_count = 0
        self.first_state_at: datetime | None = None
        self.last_state_at: datetime | None = None
        self.boundary_count = 0
        self.last_boundary_at: datetime | None = None
        self.last_boundary_duration_ms: float | None = None
        self.last_boundary_calculation_ms: float | None = None
        self.last_boundary_persistence_ms: float | None = None
        self.maximum_boundary_duration_ms: float | None = None
        self.maximum_boundary_calculation_ms: float | None = None
        self.maximum_boundary_persistence_ms: float | None = None
        self.total_research_processing_ms = 0.0
        self.summary_write_failure_count = 0

    @property
    def sampling_cadence_minutes(self) -> int:
        return RESEARCH_SAMPLING_CADENCE_MINUTES

    def observe_websocket_payload(
        self,
        symbol: str,
        patch: Mapping[str, object],
        merged: Mapping[str, object],
        observed_at: datetime,
    ) -> None:
        normalized_symbol = symbol.strip().upper()
        self.payload_schema_observer.observe_payload(
            patch=patch,
            merged=merged,
            observed_at=observed_at,
            asset_class=self.asset_class_by_symbol.get(normalized_symbol),
        )

    def observe_accepted_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        source: object = 'websocket',
    ) -> None:
        normalized_symbol = snapshot.symbol.strip().upper()
        if normalized_symbol not in self.research_symbols:
            return
        try:
            source_name = str(getattr(source, 'value', source)).lower()
            self._accepted_snapshots[normalized_symbol].append(
                (snapshot, source_name)
            )
            if source_name == 'websocket':
                self.microstructure.observe(snapshot)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('microstructure_observe', exc)

    def maybe_emit(
        self,
        *,
        symbol: str,
        state_at: datetime,
        session_decision,
    ) -> bool:
        cutoff = _as_utc(state_at)
        normalized_symbol = symbol.strip().upper()
        asset_class = self.research_symbols.get(normalized_symbol)
        if not self._state_is_expected(
            asset_class=asset_class,
            state_at=cutoff,
            session_decision=session_decision,
        ):
            return False
        self.expected_state_count += 1
        try:
            built = self._build_state(
                symbol=normalized_symbol,
                asset_class=asset_class,
                state_at=cutoff,
                session_decision=session_decision,
            )
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self.state_calculation_failure_count += 1
            self._record_failure('research_state_calculation', exc)
            self._persist_summary(updated_at=cutoff)
            return False
        if built is None:
            self.duplicate_prevented_count += 1
            self._persist_summary(updated_at=cutoff)
            return False
        if self._write_built_states([built]) != 1:
            self._persist_summary(updated_at=cutoff)
            return False
        self._record_emitted_state(built)
        self._persist_summary(updated_at=cutoff)
        return True

    def emit_boundary(
        self,
        *,
        symbols: list[str],
        state_at: datetime,
        session_decisions: Mapping[str, object],
    ) -> ResearchBoundaryResult:
        cutoff = _as_utc(state_at)
        if not _is_cadence_boundary(cutoff) or cutoff <= self.collection_started_at:
            return self._empty_boundary_result(cutoff)
        if cutoff in self._processed_boundaries:
            self.duplicate_prevented_count += 1
            self._persist_summary(updated_at=cutoff)
            return self._empty_boundary_result(cutoff)
        self._processed_boundaries.add(cutoff)
        self.boundary_count += 1
        self.last_boundary_at = cutoff

        started = perf_counter()
        built_states: list[_BuiltResearchState] = []
        expected = 0
        skipped = 0
        calculation_failures = 0
        for symbol in dict.fromkeys(symbol.strip().upper() for symbol in symbols):
            asset_class = self.research_symbols.get(symbol)
            if asset_class is None:
                continue
            session_decision = session_decisions.get(symbol)
            if not self._state_is_expected(
                asset_class=asset_class,
                state_at=cutoff,
                session_decision=session_decision,
            ):
                skipped += 1
                continue
            expected += 1
            try:
                built = self._build_state(
                    symbol=symbol,
                    asset_class=asset_class,
                    state_at=cutoff,
                    session_decision=session_decision,
                )
            except Exception as exc:  # noqa: BLE001 - per-symbol boundary
                calculation_failures += 1
                self._record_failure('research_state_calculation', exc)
                continue
            if built is None:
                self.duplicate_prevented_count += 1
                continue
            built_states.append(built)

        calculation_ms = _milliseconds(perf_counter() - started)
        persistence_started = perf_counter()
        emitted = self._write_built_states(built_states)
        persistence_ms = _milliseconds(perf_counter() - persistence_started)
        journal_failures = len(built_states) - emitted
        if emitted == len(built_states):
            for built in built_states:
                self._record_emitted_state(built)

        self.expected_state_count += expected
        self.skipped_not_tradable_count += skipped
        self.state_calculation_failure_count += calculation_failures
        total_ms = _round_milliseconds(calculation_ms + persistence_ms)
        self._record_boundary_timings(
            calculation_ms=calculation_ms,
            persistence_ms=persistence_ms,
            total_ms=total_ms,
        )
        self._persist_summary(updated_at=cutoff)
        return ResearchBoundaryResult(
            state_at=cutoff,
            expected_state_count=expected,
            emitted_state_count=emitted,
            skipped_not_tradable_count=skipped,
            state_calculation_failure_count=calculation_failures,
            journal_write_failure_count=journal_failures,
            calculation_duration_ms=calculation_ms,
            persistence_duration_ms=persistence_ms,
            total_duration_ms=total_ms,
        )

    def reset_symbol(self, symbol: str) -> None:
        try:
            normalized_symbol = symbol.strip().upper()
            self.microstructure.reset_symbol(normalized_symbol)
            self._accepted_snapshots.pop(normalized_symbol, None)
            self._emitted_state_ids_by_symbol.pop(normalized_symbol, None)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('research_symbol_reset', exc)

    def flush(self) -> bool:
        schema_written = True
        try:
            schema_written = self.payload_schema_observer.flush(force=True)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('payload_schema_flush', exc)
            schema_written = False
        summary_written = self._persist_summary(updated_at=datetime.now(UTC))
        return schema_written and summary_written

    def _build_state(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        state_at: datetime,
        session_decision,
    ) -> _BuiltResearchState | None:
        normalized_symbol = symbol
        cutoff = _as_utc(state_at)
        session_key = str(session_decision.session_key)
        state_id = build_research_state_id(
            symbol=normalized_symbol,
            session_key=session_key,
            state_at=cutoff,
        )
        emitted_state_ids = self._emitted_state_ids_by_symbol.setdefault(
            normalized_symbol,
            set(),
        )
        if state_id in emitted_state_ids:
            return None

        latest_snapshot, latest_source = self._latest_accepted_before(
            normalized_symbol,
            cutoff,
        )

        bars = self.multi_timeframe_service.bars(
            normalized_symbol,
            Timeframe.M1,
            as_of=cutoff,
            complete_only=True,
        )
        candle_features = build_candle_research_features(
            bars=bars,
            state_at=cutoff,
            session_key=session_key,
            session_start_time=session_decision.session_start_time,
        )
        context = (
            self.market_context_service.build_side_neutral_research_context(
                symbol=normalized_symbol,
                as_of=cutoff,
            )
        )
        context_features = _flatten_market_context(context)
        microstructure_features = self.microstructure.features(
            symbol=normalized_symbol,
            state_at=cutoff,
        )
        feature_values = {
            **candle_features,
            **context_features,
            **microstructure_features,
        }
        available_count = sum(
            feature_values.get(name) is not None
            for name in RESEARCH_FEATURE_NAMES
        )
        expected_count = len(RESEARCH_FEATURE_NAMES)
        latest_closed_bar = max(
            (
                bar
                for bar in bars
                if bar.session_key == session_key
                if _as_utc(bar.candle.closed_at) <= cutoff
            ),
            key=lambda bar: _as_utc(bar.candle.closed_at),
            default=None,
        )
        latest_candle = (
            None if latest_closed_bar is None else latest_closed_bar.candle
        )
        latest_closed = (
            None
            if latest_candle is None
            else _as_utc(latest_candle.closed_at)
        )
        mid = (
            None
            if latest_snapshot is None
            else (latest_snapshot.bid + latest_snapshot.ask) / 2
        )
        spread = (
            None
            if latest_snapshot is None
            else latest_snapshot.ask - latest_snapshot.bid
        )
        last = None if latest_snapshot is None else latest_snapshot.last
        record: dict[str, Any] = {
            'schema_version': RESEARCH_STATE_SCHEMA_VERSION,
            'research_contract_version': (
                RESEARCH_STATE_CONTRACT_VERSION
            ),
            'microstructure_contract_version': (
                MICROSTRUCTURE_CONTRACT_VERSION
            ),
            'research_feature_set_sha256': (
                RESEARCH_FEATURE_SET_SHA256
            ),
            'research_state_id': state_id,
            'state_at': cutoff,
            'feature_cutoff_at': cutoff,
            'causal_cutoff_convention': (
                RESEARCH_CAUSAL_CUTOFF_CONVENTION
            ),
            'latest_market_timestamp': (
                None if latest_snapshot is None else latest_snapshot.timestamp
            ),
            'latest_market_received_at': (
                None
                if latest_snapshot is None
                else latest_snapshot.received_at or latest_snapshot.timestamp
            ),
            'latest_market_source': latest_source,
            'latest_closed_candle_timestamp': latest_closed,
            'symbol': normalized_symbol,
            'asset_class': asset_class.value,
            'session_key': session_key,
            'session_start_time': session_decision.session_start_time,
            'session_end_time': session_decision.session_end_time,
            'session_minute': _session_minute(
                cutoff,
                session_decision.session_start_time,
            ),
            'time_until_session_end_minutes': (
                _time_until_session_end_minutes(
                    cutoff,
                    session_decision.session_end_time,
                )
            ),
            'session_progress_ratio': _session_progress(
                cutoff,
                session_decision.session_start_time,
                session_decision.session_end_time,
            ),
            'weekday_utc': cutoff.weekday(),
            'minute_of_day_utc': cutoff.hour * 60 + cutoff.minute,
            'bid': (
                None if latest_snapshot is None else _round(latest_snapshot.bid)
            ),
            'ask': (
                None if latest_snapshot is None else _round(latest_snapshot.ask)
            ),
            'last': None if last is None else _round(last),
            'mid': None if mid is None else _round(mid),
            'observed_spread': (
                None if spread is None else _round(spread)
            ),
            'observed_spread_percent': (
                None
                if mid is None or mid <= 0 or spread is None
                else _round((spread / mid) * 100)
            ),
            'quote_available': latest_snapshot is not None,
            'last_is_broker_execution': (
                latest_snapshot is not None
                and latest_snapshot.price_source is PriceSource.BROKER_LAST
            ),
            'quote_freshness_seconds': (
                None
                if latest_snapshot is None
                else _round(
                    (
                        cutoff - _as_utc(latest_snapshot.timestamp)
                    ).total_seconds()
                )
            ),
            'quote_receive_freshness_seconds': (
                None
                if latest_snapshot is None
                else _round(
                    (
                        cutoff
                        - _as_utc(
                            latest_snapshot.received_at
                            or latest_snapshot.timestamp
                        )
                    ).total_seconds()
                )
            ),
            'latest_candle_available': latest_candle is not None,
            'latest_candle_sample_count': (
                None if latest_candle is None else latest_candle.sample_count
            ),
            'latest_candle_carried_forward': (
                None
                if latest_candle is None
                else latest_candle.carried_forward
            ),
            'latest_candle_quality_degraded': (
                None
                if latest_candle is None
                else latest_candle.quality_degraded
            ),
            'latest_candle_source_price_age_seconds': (
                None
                if latest_candle is None
                else latest_candle.source_price_age_seconds
            ),
            'feature_expected_count': expected_count,
            'feature_available_count': available_count,
            'feature_completeness_ratio': _round(
                available_count / expected_count
            ),
            'market_context_available': (
                context.latest_symbol_timestamp is not None
            ),
            'micro_10s_available': (
                microstructure_features['micro_10s_quote_count'] >= 2
            ),
            'micro_30s_available': (
                microstructure_features['micro_30s_quote_count'] >= 2
            ),
            'micro_60s_available': (
                microstructure_features['micro_60s_quote_count'] >= 2
            ),
            **feature_values,
        }
        return _BuiltResearchState(
            symbol=normalized_symbol,
            state_id=state_id,
            record=record,
        )

    def _state_is_expected(
        self,
        *,
        asset_class: AssetClass | None,
        state_at: datetime,
        session_decision,
    ) -> bool:
        return (
            asset_class is not None
            and _is_cadence_boundary(state_at)
            and state_at > self.collection_started_at
            and _session_is_research_tradable(session_decision, state_at)
        )

    def _write_built_states(
        self,
        built_states: list[_BuiltResearchState],
    ) -> int:
        if not built_states:
            return 0
        try:
            written = self.journal.write_many(
                (RESEARCH_EVENT_TYPE, built.record)
                for built in built_states
            )
        except Exception:
            written = 0
            logger.exception('Research journal batch write raised unexpectedly')
        if written != len(built_states):
            failed = len(built_states)
            self.journal_failure_count += failed
            self.failure_count += 1
            logger.error(
                'Research journal batch was not persisted | expected=%d | '
                'written=%d',
                failed,
                written,
            )
            return 0
        return written

    def _record_emitted_state(self, built: _BuiltResearchState) -> None:
        self._emitted_state_ids_by_symbol.setdefault(
            built.symbol,
            set(),
        ).add(built.state_id)
        record = built.record
        state_at = _as_utc(record['state_at'])
        self.emitted_state_count += 1
        if not record['quote_available']:
            self.states_missing_quote_count += 1
        if not record['latest_candle_available']:
            self.states_missing_candle_count += 1
        if record['micro_10s_available']:
            self.states_with_micro_10s_count += 1
        if record['micro_30s_available']:
            self.states_with_micro_30s_count += 1
        if record['micro_60s_available']:
            self.states_with_micro_60s_count += 1
        if self.first_state_at is None or state_at < self.first_state_at:
            self.first_state_at = state_at
        if self.last_state_at is None or state_at > self.last_state_at:
            self.last_state_at = state_at

    def _record_boundary_timings(
        self,
        *,
        calculation_ms: float,
        persistence_ms: float,
        total_ms: float,
    ) -> None:
        self.last_boundary_calculation_ms = calculation_ms
        self.last_boundary_persistence_ms = persistence_ms
        self.last_boundary_duration_ms = total_ms
        self.maximum_boundary_calculation_ms = _maximum_optional(
            self.maximum_boundary_calculation_ms,
            calculation_ms,
        )
        self.maximum_boundary_persistence_ms = _maximum_optional(
            self.maximum_boundary_persistence_ms,
            persistence_ms,
        )
        self.maximum_boundary_duration_ms = _maximum_optional(
            self.maximum_boundary_duration_ms,
            total_ms,
        )
        self.total_research_processing_ms = _round_milliseconds(
            self.total_research_processing_ms + total_ms
        )

    def health_snapshot(self, *, updated_at: datetime) -> dict[str, Any]:
        result = empty_research_summary(
            run_id=self.run_id,
            enabled=True,
            updated_at=updated_at,
        )
        result.update(
            {
                'expected_state_count': self.expected_state_count,
                'emitted_state_count': self.emitted_state_count,
                'skipped_not_tradable_count': (
                    self.skipped_not_tradable_count
                ),
                'duplicate_prevented_count': self.duplicate_prevented_count,
                'state_calculation_failure_count': (
                    self.state_calculation_failure_count
                ),
                'journal_write_failure_count': self.journal_failure_count,
                'payload_schema_failure_count': getattr(
                    self.payload_schema_observer,
                    'failure_count',
                    0,
                ),
                'states_missing_quote_count': self.states_missing_quote_count,
                'states_missing_candle_count': (
                    self.states_missing_candle_count
                ),
                'states_with_micro_10s_count': (
                    self.states_with_micro_10s_count
                ),
                'states_with_micro_30s_count': (
                    self.states_with_micro_30s_count
                ),
                'states_with_micro_60s_count': (
                    self.states_with_micro_60s_count
                ),
                'first_state_at': self.first_state_at,
                'last_state_at': self.last_state_at,
                'boundary_count': self.boundary_count,
                'last_boundary_at': self.last_boundary_at,
                'last_boundary_duration_ms': (
                    self.last_boundary_duration_ms
                ),
                'last_boundary_calculation_ms': (
                    self.last_boundary_calculation_ms
                ),
                'last_boundary_persistence_ms': (
                    self.last_boundary_persistence_ms
                ),
                'maximum_boundary_duration_ms': (
                    self.maximum_boundary_duration_ms
                ),
                'maximum_boundary_calculation_ms': (
                    self.maximum_boundary_calculation_ms
                ),
                'maximum_boundary_persistence_ms': (
                    self.maximum_boundary_persistence_ms
                ),
                'total_research_processing_ms': (
                    self.total_research_processing_ms
                ),
                'research_journal_open_count': getattr(
                    self.journal,
                    'open_count',
                    0,
                ),
                'summary_write_failure_count': (
                    self.summary_write_failure_count
                ),
            }
        )
        return result

    def _persist_summary(self, *, updated_at: datetime) -> bool:
        if self.summary_path is None:
            return True
        try:
            write_research_summary(
                self.summary_path,
                self.health_snapshot(updated_at=updated_at),
            )
            return True
        except Exception:
            self.summary_write_failure_count += 1
            self.failure_count += 1
            logger.exception(
                'Research summary write failed | path=%s',
                self.summary_path,
            )
            return False

    @staticmethod
    def _empty_boundary_result(state_at: datetime) -> ResearchBoundaryResult:
        return ResearchBoundaryResult(
            state_at=state_at,
            expected_state_count=0,
            emitted_state_count=0,
            skipped_not_tradable_count=0,
            state_calculation_failure_count=0,
            journal_write_failure_count=0,
            calculation_duration_ms=0.0,
            persistence_duration_ms=0.0,
            total_duration_ms=0.0,
        )

    def _latest_accepted_before(
        self,
        symbol: str,
        state_at: datetime,
    ) -> tuple[MarketSnapshot | None, str | None]:
        for snapshot, source in reversed(
            self._accepted_snapshots.get(symbol, ())
        ):
            received_at = snapshot.received_at or snapshot.timestamp
            if (
                _as_utc(snapshot.timestamp) < state_at
                and _as_utc(received_at) < state_at
            ):
                return snapshot, source
        return None, None

    def _record_failure(self, stage: str, _exc: Exception) -> None:
        self.failure_count += 1
        logger.exception(
            'Research sidecar failure | stage=%s',
            stage,
        )


def build_research_state_id(
    *,
    symbol: str,
    session_key: str,
    state_at: datetime,
) -> str:
    identity = '|'.join(
        (
            RESEARCH_STATE_CONTRACT_VERSION,
            symbol.strip().upper(),
            session_key,
            _as_utc(state_at).isoformat(),
        )
    )
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return f'rs1_{digest}'


def _flatten_market_context(
    context: SideNeutralResearchMarketContext,
) -> dict[str, Any]:
    benchmark = context.benchmark
    breadth = context.breadth
    sector = context.sector
    spread = context.spread
    return {
        'market_regime': context.regime.value,
        'context_latest_symbol_timestamp': (
            context.latest_symbol_timestamp
        ),
        'symbol_session_return_percent': (
            context.symbol_session_return_percent
        ),
        'symbol_relative_strength_percent': (
            context.symbol_relative_strength_percent
        ),
        'benchmark_symbol': benchmark.symbol,
        'benchmark_available': benchmark.available,
        'benchmark_session_return_percent': (
            benchmark.session_return_percent
        ),
        'benchmark_momentum_percent': benchmark.momentum_percent,
        'benchmark_spread_percent': benchmark.spread_percent,
        'benchmark_snapshot_age_seconds': (
            benchmark.snapshot_age_seconds
        ),
        'breadth_available': breadth.available,
        'breadth_valid_symbols': breadth.valid_symbols,
        'breadth_coverage_ratio': breadth.coverage_ratio,
        'breadth_advancing_ratio': breadth.advancing_ratio,
        'breadth_median_session_return_percent': (
            breadth.median_session_return_percent
        ),
        'sector': sector.sector,
        'sector_available': sector.available,
        'sector_valid_member_count': sector.valid_member_count,
        'sector_advancing_ratio': sector.advancing_ratio,
        'sector_median_session_return_percent': (
            sector.median_session_return_percent
        ),
        'relative_spread_available': spread.available,
        'relative_spread_ratio': spread.relative_to_median,
        'relative_spread_percentile': spread.reference_percentile,
        'relative_spread_recent_change': spread.recent_change_ratio,
    }


def _is_cadence_boundary(value: datetime) -> bool:
    return (
        value.second == 0
        and value.microsecond == 0
        and value.minute % RESEARCH_SAMPLING_CADENCE_MINUTES == 0
    )


def _session_is_research_tradable(decision, state_at: datetime) -> bool:
    if decision is None:
        return False
    if not decision.session_active or not decision.collect_snapshots:
        return False
    if decision.session_key is None:
        return False
    if (
        decision.session_start_time is not None
        and state_at < _as_utc(decision.session_start_time)
    ):
        return False
    remaining = _time_until_session_end_minutes(
        state_at,
        decision.session_end_time,
    )
    if remaining is not None:
        return remaining > 60
    return bool(decision.new_entries_allowed)


def _time_until_session_end_minutes(
    state_at: datetime,
    session_end_time: datetime | None,
) -> float | None:
    if session_end_time is None:
        return None
    return _round(
        max(
            0.0,
            (
                _as_utc(session_end_time) - _as_utc(state_at)
            ).total_seconds()
            / 60,
        ),
        digits=4,
    )


def _session_minute(
    state_at: datetime,
    session_start_time: datetime | None,
) -> int | None:
    if session_start_time is None:
        return None
    return max(
        0,
        int(
            (
                state_at - _as_utc(session_start_time)
            ).total_seconds()
            // 60
        ),
    )


def _session_progress(
    state_at: datetime,
    session_start_time: datetime | None,
    session_end_time: datetime | None,
) -> float | None:
    if session_start_time is None or session_end_time is None:
        return None
    start = _as_utc(session_start_time)
    end = _as_utc(session_end_time)
    duration = (end - start).total_seconds()
    if duration <= 0:
        return None
    return _round(
        min(1.0, max(0.0, (state_at - start).total_seconds() / duration))
    )


def _round(value: float, *, digits: int = 8) -> float:
    return round(float(value), digits)


def _milliseconds(seconds: float) -> float:
    return _round_milliseconds(seconds * 1000)


def _round_milliseconds(value: float) -> float:
    return round(float(value), 3)


def _maximum_optional(current: float | None, value: float) -> float:
    return value if current is None else max(current, value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
