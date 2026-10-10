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

    def set_data_expected(self, expected: bool) -> None:
        """Tell the transport whether executable live data is currently expected.

        Session-aware feeds may suppress silence recovery while their market is
        closed. This never grants trading authority: session and per-symbol
        freshness checks remain independent runtime gates.
        """
        return None

    def executable_data_expected(self) -> bool:
        """Return whether REST/WS executable quotes are expected right now."""
        return True

    def connection_healthy(self) -> bool:
        """Return whether the primary transport is currently usable.

        Polling feeds are healthy unless they surface a fatal error. WebSocket
        feeds override this with their actual authenticated connection state.
        """
        return True

    def diagnostics(self) -> dict[str, object]:
        return {}
