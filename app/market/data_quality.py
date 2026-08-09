import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping

from app.market.models import MarketSnapshot

_LIVE_CLOCK_WINDOW_SECONDS = 300
QUOTE_QUALITY_CONTRACT_VERSION = 'quote_quality_v2'
SUSPECT_REFERENCE_OBSERVATIONS = 128
SUSPECT_MINIMUM_OBSERVATIONS = 20
SUSPECT_MINIMUM_JUMP_PERCENT = 0.75
SUSPECT_MEDIAN_MULTIPLIER = 20.0


class MarketDataStatus(StrEnum):
    ACCEPTED = 'accepted'
    QUARANTINED = 'quarantined'
    REJECTED = 'rejected'


@dataclass(frozen=True)
class MarketDataQualityConfig:
    max_snapshot_age_seconds: int = 120
    max_future_skew_seconds: int = 10
    max_out_of_order_seconds: int = 2
    max_data_spread_percent: float = 5.0
    max_last_quote_deviation_percent: float = 2.0
    max_jump_percent: float = 5.0
    jump_confirmation_tolerance_percent: float = 0.35
    reset_baseline_after_seconds: int = 900


@dataclass(frozen=True)
class MarketDataValidationResult:
    symbol: str
    status: MarketDataStatus
    snapshot: MarketSnapshot | None
    reasons: tuple[str, ...] = ()
    spread_percent: float | None = None
    price_change_percent: float | None = None
    previous_snapshot_timestamp: datetime | None = None
    quality_contract_version: str = QUOTE_QUALITY_CONTRACT_VERSION
    suspect_quote: bool = False
    suspicion_threshold_percent: float | None = None
    reference_median_abs_change_percent: float | None = None
    reference_observations: int = 0
    quarantined_snapshot_timestamp: datetime | None = None


@dataclass(frozen=True)
class ValidatedMarketBatch:
    loop_id: int
    as_of: datetime
    requested_symbols: tuple[str, ...]
    accepted: dict[str, MarketSnapshot] = field(default_factory=dict)
    quarantined: dict[str, MarketDataValidationResult] = field(
        default_factory=dict
    )
    rejected: dict[str, MarketDataValidationResult] = field(
        default_factory=dict
    )
    missing_symbols: tuple[str, ...] = ()
    results: dict[str, MarketDataValidationResult] = field(
        default_factory=dict
    )


class MarketDataValidator:
    def __init__(self) -> None:
        self._last_accepted: dict[str, MarketSnapshot] = {}
        self._quarantined: dict[str, MarketSnapshot] = {}
        self._accepted_abs_changes: dict[str, deque[float]] = {}

    def reset_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        self._last_accepted.pop(normalized, None)
        self._quarantined.pop(normalized, None)
        self._accepted_abs_changes.pop(normalized, None)

    def observe_accepted(self, snapshot: MarketSnapshot) -> None:
        """Mirror a snapshot already accepted by another source gate.

        Position REST fallback has a separate validator so its timestamps can
        never advance the canonical WebSocket validator. Mirroring accepted
        WebSocket quotes keeps the fallback gate warm without revalidating or
        forwarding the quote a second time.
        """

        normalized_symbol = snapshot.symbol.strip().upper()
        previous = self._last_accepted.get(normalized_symbol)
        self._quarantined.pop(normalized_symbol, None)
        self._accept_snapshot(normalized_symbol, previous, snapshot)

    def validate(
        self,
        snapshot: MarketSnapshot,
        config: MarketDataQualityConfig,
        *,
        now: datetime | None = None,
    ) -> MarketDataValidationResult:
        actual_now = _validation_time(now)
        normalized_symbol = snapshot.symbol.strip().upper()
        timestamp = _as_utc(snapshot.timestamp)
        previous = self._last_accepted.get(normalized_symbol)
        spread = _spread_percent(snapshot)
        reasons = self._basic_rejection_reasons(
            snapshot=snapshot,
            timestamp=timestamp,
            now=actual_now,
            spread=spread,
            previous=previous,
            config=config,
        )
        change = _quote_change_percent(previous, snapshot) if previous else None
        if reasons:
            return MarketDataValidationResult(
                symbol=normalized_symbol,
                status=MarketDataStatus.REJECTED,
                snapshot=snapshot,
                reasons=tuple(reasons),
                spread_percent=_round_optional(spread),
                price_change_percent=_round_optional(change),
                previous_snapshot_timestamp=(
                    previous.timestamp if previous else None
                ),
            )

        if (
            previous is not None
            and self._baseline_is_stale(previous, timestamp, config)
        ):
            previous = None
            change = None
            self._quarantined.pop(normalized_symbol, None)
            self._accepted_abs_changes.pop(normalized_symbol, None)

        pending = self._quarantined.get(normalized_symbol)
        if (
            previous is not None
            and pending is not None
            and self._confirms_quarantined_level(
                snapshot=snapshot,
                quarantined=pending,
                config=config,
            )
        ):
            self._quarantined.pop(normalized_symbol, None)
            self._accept_snapshot(normalized_symbol, previous, snapshot)
            diagnostics = self._suspect_diagnostics(normalized_symbol)
            return MarketDataValidationResult(
                symbol=normalized_symbol,
                status=MarketDataStatus.ACCEPTED,
                snapshot=snapshot,
                reasons=('suspect_quote_level_confirmed',),
                spread_percent=_round_optional(spread),
                price_change_percent=_round_optional(change),
                previous_snapshot_timestamp=previous.timestamp,
                suspect_quote=True,
                quarantined_snapshot_timestamp=pending.timestamp,
                **diagnostics,
            )

        if (
            previous is not None
            and pending is not None
            and self._returns_to_baseline(
                snapshot=snapshot,
                baseline=previous,
                config=config,
            )
        ):
            self._quarantined.pop(normalized_symbol, None)
            self._accept_snapshot(normalized_symbol, previous, snapshot)
            diagnostics = self._suspect_diagnostics(normalized_symbol)
            return MarketDataValidationResult(
                symbol=normalized_symbol,
                status=MarketDataStatus.ACCEPTED,
                snapshot=snapshot,
                reasons=('isolated_suspect_quote_rejected',),
                spread_percent=_round_optional(spread),
                price_change_percent=_round_optional(change),
                previous_snapshot_timestamp=previous.timestamp,
                suspect_quote=False,
                quarantined_snapshot_timestamp=pending.timestamp,
                **diagnostics,
            )

        if previous is not None and pending is not None:
            self._quarantined[normalized_symbol] = snapshot
            diagnostics = self._suspect_diagnostics(normalized_symbol)
            return MarketDataValidationResult(
                symbol=normalized_symbol,
                status=MarketDataStatus.QUARANTINED,
                snapshot=snapshot,
                reasons=('suspect_quote_resolution_pending',),
                spread_percent=_round_optional(spread),
                price_change_percent=_round_optional(change),
                previous_snapshot_timestamp=previous.timestamp,
                suspect_quote=True,
                quarantined_snapshot_timestamp=snapshot.timestamp,
                **diagnostics,
            )

        suspect, diagnostics = self._is_suspect_change(
            normalized_symbol,
            change,
            config,
        )
        if previous is not None and suspect:
            self._quarantined[normalized_symbol] = snapshot
            return MarketDataValidationResult(
                symbol=normalized_symbol,
                status=MarketDataStatus.QUARANTINED,
                snapshot=snapshot,
                reasons=('suspect_quote_requires_confirmation',),
                spread_percent=_round_optional(spread),
                price_change_percent=_round_optional(change),
                previous_snapshot_timestamp=previous.timestamp,
                suspect_quote=True,
                quarantined_snapshot_timestamp=snapshot.timestamp,
                **diagnostics,
            )

        self._quarantined.pop(normalized_symbol, None)
        self._accept_snapshot(normalized_symbol, previous, snapshot)
        diagnostics = self._suspect_diagnostics(normalized_symbol)
        return MarketDataValidationResult(
            symbol=normalized_symbol,
            status=MarketDataStatus.ACCEPTED,
            snapshot=snapshot,
            spread_percent=_round_optional(spread),
            price_change_percent=_round_optional(change),
            previous_snapshot_timestamp=(
                previous.timestamp if previous else None
            ),
            **diagnostics,
        )

    def _accept_snapshot(
        self,
        symbol: str,
        previous: MarketSnapshot | None,
        snapshot: MarketSnapshot,
    ) -> None:
        if previous is not None:
            change = _quote_change_percent(previous, snapshot)
            if change is not None:
                history = self._accepted_abs_changes.setdefault(
                    symbol,
                    deque(maxlen=SUSPECT_REFERENCE_OBSERVATIONS),
                )
                history.append(abs(change))
        self._last_accepted[symbol] = snapshot

    def _is_suspect_change(
        self,
        symbol: str,
        change: float | None,
        config: MarketDataQualityConfig,
    ) -> tuple[bool, dict[str, float | int | None]]:
        diagnostics = self._suspect_diagnostics(symbol)
        if change is None:
            return False, diagnostics
        threshold = diagnostics['suspicion_threshold_percent']
        fixed_jump = abs(change) > config.max_jump_percent
        adaptive_jump = (
            threshold is not None and abs(change) > float(threshold)
        )
        return fixed_jump or adaptive_jump, diagnostics

    def _suspect_diagnostics(
        self,
        symbol: str,
    ) -> dict[str, float | int | None]:
        history = self._accepted_abs_changes.get(symbol, ())
        observations = len(history)
        median_change = _median(tuple(history))
        threshold = (
            None
            if observations < SUSPECT_MINIMUM_OBSERVATIONS
            or median_change is None
            else max(
                SUSPECT_MINIMUM_JUMP_PERCENT,
                median_change * SUSPECT_MEDIAN_MULTIPLIER,
            )
        )
        return {
            'suspicion_threshold_percent': _round_optional(threshold),
            'reference_median_abs_change_percent': _round_optional(
                median_change
            ),
            'reference_observations': observations,
        }

    def validate_batch(
        self,
        *,
        loop_id: int,
        requested_symbols: list[str],
        snapshots: Mapping[str, MarketSnapshot],
        configs: Mapping[str, MarketDataQualityConfig],
        now: datetime | None = None,
    ) -> ValidatedMarketBatch:
        actual_now = _validation_time(now)
        accepted: dict[str, MarketSnapshot] = {}
        quarantined: dict[str, MarketDataValidationResult] = {}
        rejected: dict[str, MarketDataValidationResult] = {}
        results: dict[str, MarketDataValidationResult] = {}
        missing: list[str] = []

        for raw_symbol in requested_symbols:
            symbol = raw_symbol.strip().upper()
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                missing.append(symbol)
                result = MarketDataValidationResult(
                    symbol=symbol,
                    status=MarketDataStatus.REJECTED,
                    snapshot=None,
                    reasons=('missing_snapshot',),
                )
                rejected[symbol] = result
                results[symbol] = result
                continue
            result = self.validate(
                snapshot,
                configs[symbol],
                now=actual_now,
            )
            results[symbol] = result
            if result.status == MarketDataStatus.ACCEPTED:
                accepted[symbol] = snapshot
            elif result.status == MarketDataStatus.QUARANTINED:
                quarantined[symbol] = result
            else:
                rejected[symbol] = result

        return ValidatedMarketBatch(
            loop_id=loop_id,
            as_of=actual_now,
            requested_symbols=tuple(
                symbol.strip().upper()
                for symbol in requested_symbols
            ),
            accepted=accepted,
            quarantined=quarantined,
            rejected=rejected,
            missing_symbols=tuple(missing),
            results=results,
        )

    def _basic_rejection_reasons(
        self,
        *,
        snapshot: MarketSnapshot,
        timestamp: datetime,
        now: datetime,
        spread: float | None,
        previous: MarketSnapshot | None,
        config: MarketDataQualityConfig,
    ) -> list[str]:
        reasons: list[str] = []
        for name, value in (
            ('bid', snapshot.bid),
            ('ask', snapshot.ask),
            ('last', snapshot.last),
        ):
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                reasons.append(f'non_finite_{name}')
            elif float(value) <= 0:
                reasons.append(f'non_positive_{name}')
        if reasons:
            return reasons
        if snapshot.ask < snapshot.bid:
            reasons.append('inverted_quote')
        if (
            spread is None
            or spread < 0
            or spread > config.max_data_spread_percent
        ):
            reasons.append('data_spread_abnormal')
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds > config.max_snapshot_age_seconds:
            reasons.append('snapshot_too_old')
        if age_seconds < -config.max_future_skew_seconds:
            reasons.append('snapshot_from_future')
        if previous is not None:
            previous_timestamp = _as_utc(previous.timestamp)
            if (
                previous_timestamp - timestamp
            ).total_seconds() > config.max_out_of_order_seconds:
                reasons.append('snapshot_out_of_order')
        if (
            _last_quote_deviation_percent(snapshot)
            > config.max_last_quote_deviation_percent
        ):
            reasons.append('last_too_far_from_quote')
        return reasons

    def _baseline_is_stale(
        self,
        previous: MarketSnapshot,
        timestamp: datetime,
        config: MarketDataQualityConfig,
    ) -> bool:
        return (
            timestamp - _as_utc(previous.timestamp)
        ).total_seconds() > config.reset_baseline_after_seconds

    def _confirms_quarantined_level(
        self,
        *,
        snapshot: MarketSnapshot,
        quarantined: MarketSnapshot,
        config: MarketDataQualityConfig,
    ) -> bool:
        confirmation_distance = _quote_distance_percent(
            quarantined,
            snapshot,
        )
        return (
            confirmation_distance
            <= config.jump_confirmation_tolerance_percent
        )

    def _returns_to_baseline(
        self,
        *,
        snapshot: MarketSnapshot,
        baseline: MarketSnapshot,
        config: MarketDataQualityConfig,
    ) -> bool:
        distance = _quote_distance_percent(baseline, snapshot)
        return distance <= config.jump_confirmation_tolerance_percent


def quote_quality_contract_metadata() -> dict[str, float | int | str]:
    return {
        'version': QUOTE_QUALITY_CONTRACT_VERSION,
        'reference_observations': SUSPECT_REFERENCE_OBSERVATIONS,
        'minimum_observations': SUSPECT_MINIMUM_OBSERVATIONS,
        'minimum_jump_percent': SUSPECT_MINIMUM_JUMP_PERCENT,
        'median_multiplier': SUSPECT_MEDIAN_MULTIPLIER,
        'change_basis': 'max_abs_percent_change_across_bid_ask_last',
        'confirmation_basis': 'max_abs_percent_distance_across_bid_ask_last',
        'ambiguous_follow_up': (
            'quarantine_and_rebase_pending_until_level_or_baseline_confirmation'
        ),
        'confirmation_tolerance_source': (
            'instrument_market_data_quality.jump_confirmation_tolerance_percent'
        ),
    }


def _validation_time(request_started_at: datetime | None) -> datetime:
    wall_clock = datetime.now(timezone.utc)
    if request_started_at is None:
        return wall_clock
    explicit = _as_utc(request_started_at)
    if (
        abs((wall_clock - explicit).total_seconds())
        <= _LIVE_CLOCK_WINDOW_SECONDS
    ):
        return wall_clock
    return explicit


def _spread_percent(snapshot: MarketSnapshot) -> float | None:
    midpoint = (snapshot.bid + snapshot.ask) / 2
    if midpoint <= 0:
        return None
    return ((snapshot.ask - snapshot.bid) / midpoint) * 100


def _last_quote_deviation_percent(snapshot: MarketSnapshot) -> float:
    if snapshot.bid <= snapshot.last <= snapshot.ask:
        return 0.0
    midpoint = (snapshot.bid + snapshot.ask) / 2
    if midpoint <= 0:
        return float('inf')
    nearest = (
        snapshot.bid if snapshot.last < snapshot.bid else snapshot.ask
    )
    return abs(snapshot.last - nearest) / midpoint * 100


def _price_change_percent(
    previous: float,
    current: float,
) -> float | None:
    if previous <= 0:
        return None
    return ((current - previous) / previous) * 100


def _quote_change_percent(
    previous: MarketSnapshot,
    current: MarketSnapshot,
) -> float | None:
    changes = [
        change
        for before, after in (
            (previous.bid, current.bid),
            (previous.ask, current.ask),
            (previous.last, current.last),
        )
        for change in [_price_change_percent(before, after)]
        if change is not None
    ]
    return max(changes, key=abs) if changes else None


def _quote_distance_percent(
    reference: MarketSnapshot,
    current: MarketSnapshot,
) -> float:
    return abs(_quote_change_percent(reference, current) or 0.0)


def _median(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
