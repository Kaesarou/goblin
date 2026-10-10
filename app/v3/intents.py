from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from app.market.models import MarketSnapshot
from app.v3.models import ExecutionStyle, OrderIntent


@dataclass
class _IntentStats:
    quote_count: int = 0
    max_bid: float | None = None
    min_ask: float | None = None
    first_quote_at: datetime | None = None
    last_quote_at: datetime | None = None
    first_crossed_at: datetime | None = None
    first_crossing_price: float | None = None
    crossing_reported: bool = False

    def observe(self, snapshot: MarketSnapshot) -> None:
        self.quote_count += 1
        self.max_bid = (
            snapshot.bid
            if self.max_bid is None
            else max(self.max_bid, snapshot.bid)
        )
        self.min_ask = (
            snapshot.ask
            if self.min_ask is None
            else min(self.min_ask, snapshot.ask)
        )
        if self.first_quote_at is None:
            self.first_quote_at = snapshot.timestamp
        self.last_quote_at = snapshot.timestamp


@dataclass(frozen=True)
class RestingIntentObservation:
    intent: OrderIntent
    quote_count: int
    max_bid: float | None
    min_ask: float | None
    first_quote_at: datetime | None
    last_quote_at: datetime | None
    first_crossed_at: datetime | None
    first_crossing_price: float | None
    closest_distance_bp: float | None


@dataclass(frozen=True)
class RestingIntentChange:
    added: tuple[OrderIntent, ...]
    removed: tuple[RestingIntentObservation, ...]


class RestingIntentBook:
    """Local emulation of the resting limits produced by the pure V3 planner.

    eToro's current Goblin adapter opens market positions. V3 therefore keeps the
    strategy's limit intent locally and only dispatches a market mutation when a
    fresh executable quote crosses the intended price. Intents are replaced on
    the next completed strategy candle, matching the Point-M one-candle ideal-set
    lifecycle rather than accumulating stale orders.

    A submitted BUY reserves its symbol until the broker action is resolved.
    Replacing a candle's intent must not release an in-flight broker mutation.
    SELL reduce-only intents are independent and remain eligible for dispatch.
    """

    def __init__(self) -> None:
        self._by_symbol: dict[str, dict[str, OrderIntent]] = defaultdict(dict)
        self._dispatched: set[str] = set()
        self._pending_buy_by_symbol: dict[str, str] = {}
        self._stats: dict[str, _IntentStats] = {}

    def replace_symbol(
        self,
        symbol: str,
        intents: tuple[OrderIntent, ...],
    ) -> RestingIntentChange:
        normalized = symbol.strip().upper()
        previous = self._by_symbol.get(normalized, {})
        retained = {
            intent.intent_id: intent
            for intent in intents
            if intent.symbol.strip().upper() == normalized
        }
        previous_ids = set(previous)
        retained_ids = set(retained)
        removed_ids = previous_ids - retained_ids
        added_ids = retained_ids - previous_ids
        removed = tuple(
            self._observation(previous[intent_id])
            for intent_id in sorted(removed_ids)
        )
        self._dispatched.difference_update(removed_ids)
        for intent_id in removed_ids:
            self._stats.pop(intent_id, None)
        for intent_id in added_ids:
            self._stats.setdefault(intent_id, _IntentStats())
        self._by_symbol[normalized] = retained
        return RestingIntentChange(
            added=tuple(retained[intent_id] for intent_id in sorted(added_ids)),
            removed=removed,
        )

    def cancel_symbol(self, symbol: str) -> RestingIntentChange:
        normalized = symbol.strip().upper()
        previous = self._by_symbol.pop(normalized, {})
        removed_ids = set(previous)
        removed = tuple(
            self._observation(previous[intent_id])
            for intent_id in sorted(removed_ids)
        )
        self._dispatched.difference_update(removed_ids)
        for intent_id in removed_ids:
            self._stats.pop(intent_id, None)
        # Cancellation only removes local intents. It cannot cancel or resolve
        # an already submitted broker BUY, so the symbol reservation remains.
        return RestingIntentChange(added=(), removed=removed)

    def triggered(self, snapshot: MarketSnapshot) -> tuple[OrderIntent, ...]:
        symbol = snapshot.symbol.strip().upper()
        result: list[OrderIntent] = []
        buy_selected = False
        for intent in self._by_symbol.get(symbol, {}).values():
            if intent.intent_id in self._dispatched:
                continue
            stats = self._stats.setdefault(intent.intent_id, _IntentStats())
            stats.observe(snapshot)
            is_buy = intent.side.upper() == "BUY"
            if is_buy and (buy_selected or symbol in self._pending_buy_by_symbol):
                continue
            crossed = False
            crossing_price: float | None = None
            if intent.execution_style == ExecutionStyle.MARKET:
                crossed = True
                crossing_price = snapshot.ask if is_buy else snapshot.bid
            elif intent.limit_price is not None:
                if is_buy and snapshot.ask <= intent.limit_price:
                    crossed = True
                    crossing_price = snapshot.ask
                elif (
                    intent.side.upper() == "SELL"
                    and snapshot.bid >= intent.limit_price
                ):
                    crossed = True
                    crossing_price = snapshot.bid
            if not crossed:
                continue
            if stats.first_crossed_at is None:
                stats.first_crossed_at = snapshot.timestamp
                stats.first_crossing_price = crossing_price
            result.append(intent)
            buy_selected = buy_selected or is_buy
        return tuple(result)

    def take_first_crossing(
        self,
        intent_id: str,
    ) -> RestingIntentObservation | None:
        intent = next(
            (
                intents[intent_id]
                for intents in self._by_symbol.values()
                if intent_id in intents
            ),
            None,
        )
        stats = self._stats.get(intent_id)
        if (
            intent is None
            or stats is None
            or stats.first_crossed_at is None
            or stats.crossing_reported
        ):
            return None
        stats.crossing_reported = True
        return self._observation(intent)

    def mark_dispatched(self, intent_id: str) -> None:
        for symbol, intents in self._by_symbol.items():
            intent = intents.get(intent_id)
            if intent is None or intent.side.upper() != "BUY":
                continue
            existing = self._pending_buy_by_symbol.get(symbol)
            if existing is not None and existing != intent_id:
                raise RuntimeError(
                    f"Concurrent BUY dispatch for {symbol}: {existing}"
                )
            self._pending_buy_by_symbol[symbol] = intent_id
            break
        self._dispatched.add(intent_id)

    def resolve(self, intent_id: str) -> None:
        self._dispatched.discard(intent_id)
        self._stats.pop(intent_id, None)
        for symbol, pending in tuple(self._pending_buy_by_symbol.items()):
            if pending == intent_id:
                self._pending_buy_by_symbol.pop(symbol, None)
        for symbol in list(self._by_symbol):
            if intent_id in self._by_symbol[symbol]:
                self._by_symbol[symbol].pop(intent_id, None)
            if not self._by_symbol[symbol]:
                self._by_symbol.pop(symbol, None)

    def snapshot(self) -> tuple[OrderIntent, ...]:
        return tuple(
            intent
            for symbol in sorted(self._by_symbol)
            for intent in self._by_symbol[symbol].values()
        )

    def _observation(self, intent: OrderIntent) -> RestingIntentObservation:
        stats = self._stats.get(intent.intent_id, _IntentStats())
        limit = intent.limit_price
        closest: float | None = None
        if limit is not None and limit > 0:
            if intent.side.upper() == "SELL" and stats.max_bid is not None:
                closest = (stats.max_bid / limit - 1.0) * 10_000
            elif intent.side.upper() == "BUY" and stats.min_ask is not None:
                closest = (limit / stats.min_ask - 1.0) * 10_000
        return RestingIntentObservation(
            intent=intent,
            quote_count=stats.quote_count,
            max_bid=stats.max_bid,
            min_ask=stats.min_ask,
            first_quote_at=stats.first_quote_at,
            last_quote_at=stats.last_quote_at,
            first_crossed_at=stats.first_crossed_at,
            first_crossing_price=stats.first_crossing_price,
            closest_distance_bp=closest,
        )
