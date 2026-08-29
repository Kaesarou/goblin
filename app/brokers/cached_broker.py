import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import cast

from app.brokers.base import (
    BrokerClient,
    BrokerCloseExecution,
    ClosePositionSubmission,
)

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    expires_at: float
    value: object


@dataclass
class CachedBrokerClient(BrokerClient):
    delegate: BrokerClient
    account_equity_ttl_seconds: float = 30.0
    position_status_ttl_seconds: float = 5.0
    logging_enabled: bool = False
    account_equity_cache: CacheEntry | None = None
    position_status_cache: dict[str, CacheEntry] = field(default_factory=dict)

    def get_account_equity(self) -> float:
        now = self._now()
        if (
            self.account_equity_ttl_seconds > 0
            and self.account_equity_cache is not None
            and self.account_equity_cache.expires_at > now
        ):
            self._log_cache_hit('account_equity', 'account')
            return cast(float, self.account_equity_cache.value)
        self._log_cache_miss('account_equity', 'account')
        equity = self.delegate.get_account_equity()
        self.account_equity_cache = (
            self._build_entry(equity, self.account_equity_ttl_seconds)
            if self.account_equity_ttl_seconds > 0
            else None
        )
        return equity

    def open_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_loss: float,
        take_profit: float,
    ):
        result = self.delegate.open_position(
            symbol,
            side,
            amount,
            stop_loss,
            take_profit,
        )
        self.invalidate_account_and_positions()
        return result

    def close_position(
        self,
        position_id: str,
        units_to_deduct: float | None = None,
    ) -> ClosePositionSubmission:
        submission = self.delegate.close_position(
            position_id,
            units_to_deduct=units_to_deduct,
        )
        self.invalidate_account_and_positions()
        return submission

    def is_position_open(self, position_id: str) -> bool:
        cached_status = self._get_cache_entry(
            self.position_status_cache,
            position_id,
            'position_status',
        )
        if cached_status is not None:
            return bool(cached_status)
        is_open = self.delegate.is_position_open(position_id)
        self._put_cache_entry(
            self.position_status_cache,
            position_id,
            is_open,
            self.position_status_ttl_seconds,
        )
        return is_open

    def get_open_position_units(
        self,
        position_ids: Iterable[str],
    ) -> dict[str, float | None]:
        # Reconciliation is safety-critical and should observe one coherent broker
        # portfolio snapshot rather than compose per-position TTL cache entries.
        return self.delegate.get_open_position_units(position_ids)

    def get_rate_limit_metrics(self) -> dict[str, object]:
        return self.delegate.get_rate_limit_metrics()

    def get_close_execution(
        self,
        close_order_id: str,
        position_id: str,
    ) -> BrokerCloseExecution | None:
        return self.delegate.get_close_execution(close_order_id, position_id)

    def remember_position_instrument(self, position_id: str, symbol: str) -> None:
        self.delegate.remember_position_instrument(position_id, symbol)
        self.position_status_cache.pop(position_id, None)

    def forget_position_instrument(self, position_id: str) -> None:
        self.delegate.forget_position_instrument(position_id)
        self.position_status_cache.pop(position_id, None)

    def invalidate_account_and_positions(self) -> None:
        self.account_equity_cache = None
        self.position_status_cache.clear()

    def _get_cache_entry(
        self,
        cache: dict[str, CacheEntry],
        key: str,
        cache_name: str,
    ) -> object | None:
        entry = cache.get(key)
        now = self._now()
        if entry is None:
            self._log_cache_miss(cache_name, key)
            return None
        if entry.expires_at <= now:
            cache.pop(key, None)
            self._log_cache_miss(cache_name, key)
            return None
        self._log_cache_hit(cache_name, key)
        return entry.value

    def _put_cache_entry(
        self,
        cache: dict[str, CacheEntry],
        key: str,
        value: object,
        ttl_seconds: float,
    ) -> None:
        if ttl_seconds > 0:
            cache[key] = self._build_entry(value, ttl_seconds)

    def _build_entry(self, value: object, ttl_seconds: float) -> CacheEntry:
        return CacheEntry(expires_at=self._now() + ttl_seconds, value=value)

    def _log_cache_hit(self, cache_name: str, key: str) -> None:
        if self.logging_enabled:
            logger.info('Broker cache hit | cache=%s | key=%s', cache_name, key)

    def _log_cache_miss(self, cache_name: str, key: str) -> None:
        if self.logging_enabled:
            logger.info('Broker cache miss | cache=%s | key=%s', cache_name, key)

    def _now(self) -> float:
        return time.monotonic()
