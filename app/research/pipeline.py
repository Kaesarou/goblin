from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.instruments.models import AssetClass
from app.journal.jsonl_journal import JsonlJournal
from app.market.market_context import SideNeutralResearchMarketContext
from app.market.models import Candle, MarketSnapshot
from app.market.timeframes import Timeframe
from app.market_data.models import MarketDataEvent
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

logger = logging.getLogger(__name__)

RESEARCH_EVENT_TYPE = 'research_state'
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
        self.microstructure = EtoroMicrostructureAccumulator()
        self._emitted_state_ids_by_symbol: dict[str, set[str]] = {}
        self.failure_count = 0
        self.journal_failure_count = 0

    def observe_payload_schema(self, event: MarketDataEvent) -> None:
        sample = event.payload_schema
        if sample is None:
            return
        try:
            self.payload_schema_observer.observe(
                sample,
                asset_class=self.asset_class_by_symbol.get(
                    event.symbol.strip().upper()
                ),
            )
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('payload_schema_observer', exc)

    def observe_accepted_snapshot(self, snapshot: MarketSnapshot) -> None:
        if snapshot.symbol not in self.research_symbols:
            return
        try:
            self.microstructure.observe(snapshot)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('microstructure_observe', exc)

    def maybe_emit(
        self,
        *,
        symbol: str,
        state_at: datetime,
        closed_candle: Candle,
        session_decision,
    ) -> bool:
        try:
            return self._maybe_emit(
                symbol=symbol,
                state_at=state_at,
                closed_candle=closed_candle,
                session_decision=session_decision,
            )
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('research_state_emit', exc)
            return False

    def reset_symbol(self, symbol: str) -> None:
        try:
            normalized_symbol = symbol.strip().upper()
            self.microstructure.reset_symbol(normalized_symbol)
            self._emitted_state_ids_by_symbol.pop(normalized_symbol, None)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('research_symbol_reset', exc)

    def flush(self) -> bool:
        try:
            return self.payload_schema_observer.flush(force=True)
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            self._record_failure('payload_schema_flush', exc)
            return False

    def _maybe_emit(
        self,
        *,
        symbol: str,
        state_at: datetime,
        closed_candle: Candle,
        session_decision,
    ) -> bool:
        normalized_symbol = symbol.strip().upper()
        asset_class = self.research_symbols.get(normalized_symbol)
        cutoff = _as_utc(state_at)
        if asset_class is None or not _is_cadence_boundary(cutoff):
            return False
        if not _session_is_research_tradable(session_decision, cutoff):
            return False
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
            return False

        latest = self.microstructure.latest_before(
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
        latest_closed = max(
            (
                _as_utc(bar.candle.closed_at)
                for bar in bars
                if _as_utc(bar.candle.closed_at) <= cutoff
            ),
            default=(
                _as_utc(closed_candle.closed_at)
                if _as_utc(closed_candle.closed_at) <= cutoff
                else None
            ),
        )
        mid = None if latest is None else (latest.bid + latest.ask) / 2
        spread = None if latest is None else latest.ask - latest.bid
        last = (
            None
            if latest is None
            else (
                latest.last_execution
                if latest.last_execution is not None
                else latest.mid
            )
        )
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
                None if latest is None else latest.market_timestamp
            ),
            'latest_market_received_at': (
                None if latest is None else latest.observed_at
            ),
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
            'bid': None if latest is None else _round(latest.bid),
            'ask': None if latest is None else _round(latest.ask),
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
            'quote_available': latest is not None,
            'last_is_broker_execution': (
                latest is not None and latest.last_execution is not None
            ),
            'quote_freshness_seconds': (
                None
                if latest is None
                else _round(
                    (cutoff - latest.market_timestamp).total_seconds()
                )
            ),
            'quote_receive_freshness_seconds': (
                None
                if latest is None
                else _round(
                    (cutoff - latest.observed_at).total_seconds()
                )
            ),
            'latest_candle_sample_count': closed_candle.sample_count,
            'latest_candle_carried_forward': (
                closed_candle.carried_forward
            ),
            'latest_candle_quality_degraded': (
                closed_candle.quality_degraded
            ),
            'latest_candle_source_price_age_seconds': (
                closed_candle.source_price_age_seconds
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
        written = self.journal.write(RESEARCH_EVENT_TYPE, record)
        if written is False:
            self.journal_failure_count += 1
            return False
        emitted_state_ids.add(state_id)
        return True

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
    if not decision.session_active or not decision.new_entries_allowed:
        return False
    if decision.session_key is None:
        return False
    remaining = _time_until_session_end_minutes(
        state_at,
        decision.session_end_time,
    )
    if remaining is None:
        remaining = decision.time_until_session_end_minutes
    return remaining is None or remaining > 60


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
