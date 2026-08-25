from __future__ import annotations

from collections import defaultdict

from app.market.models import MarketSnapshot
from app.v3.models import ExecutionStyle, OrderIntent


class RestingIntentBook:
    """Local emulation of the resting limits produced by the pure V3 planner.

    eToro's current Goblin adapter opens market positions. V3 therefore keeps the
    strategy's limit intent locally and only dispatches a market mutation when a
    fresh executable quote crosses the intended price. Intents are replaced on
    the next completed strategy candle, matching the Point-M one-candle ideal-set
    lifecycle rather than accumulating stale orders.
    """

    def __init__(self) -> None:
        self._by_symbol: dict[str, dict[str, OrderIntent]] = defaultdict(dict)
        self._dispatched: set[str] = set()

    def replace_symbol(self, symbol: str, intents: tuple[OrderIntent, ...]) -> None:
        normalized = symbol.strip().upper()
        retained = {
            intent.intent_id: intent
            for intent in intents
            if intent.symbol.strip().upper() == normalized
        }
        previous_ids = set(self._by_symbol.get(normalized, {}))
        removed = previous_ids - set(retained)
        self._dispatched.difference_update(removed)
        self._by_symbol[normalized] = retained

    def cancel_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        removed = set(self._by_symbol.pop(normalized, {}))
        self._dispatched.difference_update(removed)

    def triggered(self, snapshot: MarketSnapshot) -> tuple[OrderIntent, ...]:
        symbol = snapshot.symbol.strip().upper()
        result: list[OrderIntent] = []
        for intent in self._by_symbol.get(symbol, {}).values():
            if intent.intent_id in self._dispatched:
                continue
            if intent.execution_style == ExecutionStyle.MARKET:
                result.append(intent)
                continue
            if intent.limit_price is None:
                continue
            if intent.side.upper() == "BUY" and snapshot.ask <= intent.limit_price:
                result.append(intent)
            elif intent.side.upper() == "SELL" and snapshot.bid >= intent.limit_price:
                result.append(intent)
        return tuple(result)

    def mark_dispatched(self, intent_id: str) -> None:
        self._dispatched.add(intent_id)

    def resolve(self, intent_id: str) -> None:
        self._dispatched.discard(intent_id)
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
