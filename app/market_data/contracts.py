from abc import ABC, abstractmethod
from typing import Protocol

from app.market.models import MarketSnapshot
from app.market_data.models import MarketDataEvent


class RestMarketDataClient(Protocol):
    def get_market_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]: ...


class LiveMarketDataFeed(ABC):
    @abstractmethod
    def start(self, symbols: list[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def update_symbols(self, symbols: list[str]) -> None:
        """Request replacement of the subscribed universe."""
        raise NotImplementedError

    @abstractmethod
    def subscribed_symbols(self) -> tuple[str, ...]:
        """Return the symbols whose subscription is currently applied."""
        raise NotImplementedError

    @abstractmethod
    def next_event(self, timeout_seconds: float) -> MarketDataEvent | None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    @property
    @abstractmethod
    def requires_websocket_health(self) -> bool:
        raise NotImplementedError

    def connection_healthy(self) -> bool:
        """Return whether the primary transport is currently usable.

        Polling feeds are healthy unless they surface a fatal error. WebSocket
        feeds override this with their actual authenticated connection state.
        """
        return True

    def diagnostics(self) -> dict[str, object]:
        return {}
