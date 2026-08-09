from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.brokers.base import BrokerCloseExecution
from app.execution.trade_candidate import TradeCandidate

REPLAY_PRICING_CONTRACT_VERSION = 'replay_pricing_v2'


class ReplayMode(StrEnum):
    LIVE_VALIDATION = 'live_validation'
    COUNTERFACTUAL = 'counterfactual'


class ReplayPriceProvenance(StrEnum):
    BROKER_HISTORICAL_FILL = 'broker_historical_fill'
    EXECUTABLE_ESTIMATE = 'executable_estimate'
    VALIDATION_CONTRACTUAL_FALLBACK = 'validation_contractual_fallback'


@dataclass(frozen=True)
class HistoricalReplayFill:
    candidate_id: str
    position_id: str
    entry_price: float
    entry_at: datetime
    exit_price: float | None = None
    exit_at: datetime | None = None
    close_order_id: str | None = None
    close_reason: str | None = None


@dataclass(frozen=True)
class ReplayEntryPrice:
    price: float
    provenance: ReplayPriceProvenance
    historical_position_id: str | None
    historical_entry_at: datetime | None


@dataclass(frozen=True)
class ReplayFillExtraction:
    fills: tuple[HistoricalReplayFill, ...]
    position_open_events: int
    broker_entry_fills: int
    broker_exit_fills: int
    missing_candidate_ids: int
    ambiguous_legacy_entry_prices: int

    def ledger(self) -> ReplayFillLedger:
        return ReplayFillLedger(self.fills)


class ReplayFillLedger:
    def __init__(self, fills: tuple[HistoricalReplayFill, ...] = ()) -> None:
        self._by_candidate: dict[str, HistoricalReplayFill] = {}
        self._by_position: dict[str, HistoricalReplayFill] = {}
        for fill in fills:
            if not fill.candidate_id or not fill.position_id:
                raise ValueError(
                    'Historical replay fills require candidate and position IDs.'
                )
            if not math.isfinite(fill.entry_price) or fill.entry_price <= 0:
                raise ValueError('Historical replay entry fill must be positive.')
            if fill.exit_price is not None and (
                not math.isfinite(fill.exit_price) or fill.exit_price <= 0
            ):
                raise ValueError('Historical replay exit fill must be positive.')
            if (fill.exit_price is None) != (fill.exit_at is None):
                raise ValueError(
                    'Historical replay exit price and timestamp must be paired.'
                )
            if fill.exit_at is not None and fill.exit_at < fill.entry_at:
                raise ValueError(
                    'Historical replay exit cannot precede its entry.'
                )
            if fill.candidate_id in self._by_candidate:
                raise ValueError(
                    f'Duplicate historical candidate fill: {fill.candidate_id}.'
                )
            if fill.position_id in self._by_position:
                raise ValueError(
                    f'Duplicate historical position fill: {fill.position_id}.'
                )
            self._by_candidate[fill.candidate_id] = fill
            self._by_position[fill.position_id] = fill

    def historical_fill_for_position(
        self,
        historical_position_id: str | None,
        mode: ReplayMode,
    ) -> HistoricalReplayFill | None:
        if (
            mode is not ReplayMode.LIVE_VALIDATION
            or historical_position_id is None
        ):
            return None
        return self._by_position.get(historical_position_id)

    def entry_for(
        self,
        candidate: TradeCandidate,
        mode: ReplayMode,
    ) -> ReplayEntryPrice:
        estimate = candidate.snapshot.executable_entry_price(
            candidate.signal.action
        )
        if mode is ReplayMode.COUNTERFACTUAL:
            return ReplayEntryPrice(
                price=estimate,
                provenance=ReplayPriceProvenance.EXECUTABLE_ESTIMATE,
                historical_position_id=None,
                historical_entry_at=None,
            )
        fill = self._by_candidate.get(candidate.candidate_id)
        if fill is not None:
            return ReplayEntryPrice(
                price=fill.entry_price,
                provenance=ReplayPriceProvenance.BROKER_HISTORICAL_FILL,
                historical_position_id=fill.position_id,
                historical_entry_at=fill.entry_at,
            )
        return ReplayEntryPrice(
            price=estimate,
            provenance=ReplayPriceProvenance.VALIDATION_CONTRACTUAL_FALLBACK,
            historical_position_id=None,
            historical_entry_at=None,
        )

    def close_execution_for(
        self,
        *,
        historical_position_id: str | None,
        replay_position_id: str,
        mode: ReplayMode,
    ) -> tuple[BrokerCloseExecution | None, ReplayPriceProvenance]:
        if mode is ReplayMode.COUNTERFACTUAL:
            return None, ReplayPriceProvenance.EXECUTABLE_ESTIMATE
        fill = (
            None
            if historical_position_id is None
            else self._by_position.get(historical_position_id)
        )
        if fill is None or fill.exit_price is None:
            return (
                None,
                ReplayPriceProvenance.VALIDATION_CONTRACTUAL_FALLBACK,
            )
        return (
            BrokerCloseExecution(
                position_id=replay_position_id,
                close_order_id=fill.close_order_id or 'historical-replay-fill',
                executed_exit_price=fill.exit_price,
                executed_at=fill.exit_at,
                units=None,
                conversion_rate=None,
                amount=None,
                broker_response={
                    'source': ReplayPriceProvenance.BROKER_HISTORICAL_FILL.value,
                    'historical_position_id': fill.position_id,
                },
            ),
            ReplayPriceProvenance.BROKER_HISTORICAL_FILL,
        )


def historical_fills_from_journal_records(
    records: Iterable[Mapping[str, object]],
) -> ReplayFillExtraction:
    """Extract only semantically explicit broker fills from canonical events.

    Legacy ``entry_price``/``exit_price`` fields are intentionally not treated
    as broker fills because their historical meaning is ambiguous.
    """

    opened: dict[str, dict[str, object]] = {}
    exits: dict[str, dict[str, object]] = {}
    open_events = 0
    missing_candidate_ids = 0
    ambiguous_legacy_entries = 0
    broker_exit_fills = 0
    for record in records:
        event_type = str(record.get('event_type') or '')
        payload = _mapping(record.get('payload'))
        if event_type == 'position_opened':
            open_events += 1
            position = _mapping(payload.get('position'))
            candidate = _mapping(payload.get('candidate'))
            position_id = str(
                payload.get('position_id')
                or position.get('position_id')
                or ''
            )
            candidate_id = str(candidate.get('candidate_id') or '')
            if not candidate_id:
                missing_candidate_ids += 1
            entry_price = _positive_float(
                payload.get('broker_entry_fill_price')
            )
            if entry_price is None:
                entry_price = _positive_float(
                    position.get('broker_entry_fill_price')
                )
            if entry_price is None and (
                payload.get('entry_price_source') == 'broker_fill'
                or position.get('entry_price_source') == 'broker_fill'
            ):
                entry_price = _positive_float(
                    payload.get('pnl_entry_price')
                    or position.get('pnl_entry_price')
                )
            if entry_price is None and (
                position.get('entry_price') is not None
                or payload.get('pnl_entry_price') is not None
            ):
                ambiguous_legacy_entries += 1
            if position_id:
                opened[position_id] = {
                    'candidate_id': candidate_id,
                    'entry_price': entry_price,
                    'entry_at': _datetime_or_none(
                        position.get('opened_at') or record.get('timestamp')
                    ),
                }
            continue
        if event_type not in {
            'broker_close_fill_resolved',
            'position_close_confirmed',
        }:
            continue
        closed = _mapping(payload.get('closed_position'))
        broker_execution = _mapping(payload.get('broker_execution'))
        position_id = str(
            payload.get('position_id')
            or closed.get('position_id')
            or broker_execution.get('position_id')
            or ''
        )
        exit_price = _positive_float(payload.get('broker_exit_fill_price'))
        if exit_price is None:
            exit_price = _positive_float(closed.get('broker_exit_fill_price'))
        if exit_price is None:
            exit_price = _positive_float(
                broker_execution.get('executed_exit_price')
            )
        if exit_price is None:
            continue
        if position_id not in exits:
            broker_exit_fills += 1
        exits[position_id] = {
            'exit_price': exit_price,
            'exit_at': _datetime_or_none(
                payload.get('broker_executed_at')
                or broker_execution.get('executed_at')
                or closed.get('closed_at')
                or payload.get('confirmed_at')
                or record.get('timestamp')
            ),
            'close_order_id': (
                str(
                    payload.get('close_order_id')
                    or broker_execution.get('close_order_id')
                )
                if (
                    payload.get('close_order_id')
                    or broker_execution.get('close_order_id')
                )
                else None
            ),
            'close_reason': (
                str(
                    payload.get('close_reason')
                    or closed.get('close_reason')
                )
                if payload.get('close_reason') or closed.get('close_reason')
                else None
            ),
        }

    fills: list[HistoricalReplayFill] = []
    for position_id, entry in opened.items():
        candidate_id = str(entry['candidate_id'])
        entry_price = entry['entry_price']
        entry_at = entry['entry_at']
        if not candidate_id or entry_price is None or entry_at is None:
            continue
        exit_fill = exits.get(position_id, {})
        fills.append(
            HistoricalReplayFill(
                candidate_id=candidate_id,
                position_id=position_id,
                entry_price=float(entry_price),
                entry_at=entry_at,
                exit_price=_positive_float(exit_fill.get('exit_price')),
                exit_at=_datetime_or_none(exit_fill.get('exit_at')),
                close_order_id=(
                    None
                    if exit_fill.get('close_order_id') is None
                    else str(exit_fill['close_order_id'])
                ),
                close_reason=(
                    None
                    if exit_fill.get('close_reason') is None
                    else str(exit_fill['close_reason'])
                ),
            )
        )
    return ReplayFillExtraction(
        fills=tuple(fills),
        position_open_events=open_events,
        broker_entry_fills=len(fills),
        broker_exit_fills=broker_exit_fills,
        missing_candidate_ids=missing_candidate_ids,
        ambiguous_legacy_entry_prices=ambiguous_legacy_entries,
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _datetime_or_none(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
