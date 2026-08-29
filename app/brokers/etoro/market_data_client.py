import json
import logging
import time
from pathlib import Path
from uuid import uuid4

import requests

from app.brokers.etoro.attempt_delay import delay_seconds_for_attempt
from app.brokers.etoro.endpoint_paths import (
    instrument_rates_path,
    instrument_search_path,
)
from app.brokers.etoro.get_rate_governor import (
    ETORO_GET_429_FALLBACK_SECONDS,
    EtoroGetRateGovernor,
)
from app.brokers.etoro.http_failure import raise_for_failed_response
from app.brokers.etoro.http_headers_builder import build_headers
from app.brokers.etoro.http_response_payload import response_payload
from app.brokers.etoro.http_retry_policy import (
    default_get_max_attempts,
    is_retryable_http_status,
)
from app.brokers.etoro.http_url_builder import build_http_url
from app.brokers.etoro.instrument_cache import remember_instrument_id
from app.brokers.etoro.instrument_search_parser import resolve_exact_instrument_id
from app.brokers.etoro.market_data_mapper import to_market_snapshots
from app.brokers.etoro.request_settings import default_request_timeout_seconds
from app.market.models import MarketSnapshot
from app.runtime.runtime_policy import (
    ETORO_INSTRUMENT_RESOLUTION_MIN_INTERVAL_SECONDS,
)

logger = logging.getLogger(__name__)


class EtoroRestMarketDataClient:
    """Read-only eToro REST companion for the canonical WebSocket feed."""

    api_base_url = 'https://public-api.etoro.com'
    resolution_min_interval_seconds = (
        ETORO_INSTRUMENT_RESOLUTION_MIN_INTERVAL_SECONDS
    )

    def __init__(
        self,
        *,
        api_key: str,
        user_key: str,
        instrument_id_cache_path: str,
        get_rate_governor: EtoroGetRateGovernor | None = None,
    ) -> None:
        self.api_key = api_key
        self.user_key = user_key
        self.instrument_id_cache_path = Path(instrument_id_cache_path)
        self.instrument_ids_by_symbol: dict[str, int] = {}
        self.symbol_by_instrument_id: dict[int, str] = {}
        self._last_resolution_started_at: float | None = None
        self._get_rate_governor = get_rate_governor or EtoroGetRateGovernor()
        self._load_instrument_id_cache()

    @property
    def headers(self) -> dict[str, str]:
        return build_headers(
            request_id=str(uuid4()),
            api_key=self.api_key,
            user_key=self.user_key,
        )

    def get_market_snapshots(
        self,
        symbols: list[str],
    ) -> dict[str, MarketSnapshot]:
        resolved = self.resolve_instrument_ids(symbols)
        instrument_ids = list(resolved.values())
        if not instrument_ids:
            return {}
        payload = self._get(instrument_rates_path(instrument_ids))
        return to_market_snapshots(
            rates_payload=payload,
            symbol_by_instrument_id=self.symbol_by_instrument_id,
        )

    def resolve_instrument_ids(self, symbols: list[str]) -> dict[str, int]:
        normalized = list(
            dict.fromkeys(
                symbol.strip().upper()
                for symbol in symbols
                if symbol.strip()
            )
        )
        resolved: dict[str, int] = {}
        for symbol in normalized:
            cached = self.instrument_ids_by_symbol.get(symbol)
            if cached is not None:
                resolved[symbol] = cached
                continue
            self._wait_for_resolution_slot()
            payload = self._get(
                instrument_search_path(),
                params={'internalSymbolFull': symbol},
            )
            instrument_id = resolve_exact_instrument_id(symbol, payload)
            self._last_resolution_started_at = time.monotonic()
            remember_instrument_id(
                instrument_ids_by_symbol=self.instrument_ids_by_symbol,
                symbol_by_instrument_id=self.symbol_by_instrument_id,
                symbol=symbol,
                instrument_id=instrument_id,
            )
            resolved[symbol] = instrument_id
            self._write_instrument_id_cache()
            logger.info(
                'Selected eToro market-data instrument | symbol=%s | '
                'instrument_id=%s',
                symbol,
                instrument_id,
            )
        return resolved

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = build_http_url(self.api_base_url, path)
        max_attempts = default_get_max_attempts()
        for attempt in range(1, max_attempts + 1):
            self._get_rate_governor.acquire()
            try:
                response = requests.get(
                    url,
                    headers=self.headers,
                    params=params,
                    timeout=default_request_timeout_seconds(),
                )
            except requests.RequestException as exc:
                logger.warning(
                    'eToro market-data GET failed | attempt=%s/%s | '
                    'url=%s | params=%s | error=%s',
                    attempt,
                    max_attempts,
                    url,
                    params,
                    exc,
                )
                if attempt == max_attempts:
                    raise
                time.sleep(delay_seconds_for_attempt(attempt))
                continue

            retry_after = _retry_after_seconds(response)
            if response.status_code == 429:
                self._get_rate_governor.defer(
                    retry_after
                    if retry_after is not None
                    else ETORO_GET_429_FALLBACK_SECONDS
                )
            if (
                is_retryable_http_status(response.status_code)
                and attempt < max_attempts
            ):
                logger.warning(
                    'eToro market-data GET retryable error | attempt=%s/%s | '
                    'status=%s | url=%s | params=%s',
                    attempt,
                    max_attempts,
                    response.status_code,
                    url,
                    params,
                )
                time.sleep(
                    retry_after
                    if retry_after is not None
                    else delay_seconds_for_attempt(attempt)
                )
                continue
            if not response.ok:
                logger.error(
                    'eToro market-data GET failed | status=%s | url=%s | '
                    'params=%s | response=%s',
                    response.status_code,
                    url,
                    params,
                    response.text,
                )
                raise_for_failed_response(response)
            return response_payload(response)
        raise RuntimeError(
            f'eToro market-data GET failed after retries | url={url}'
        )

    def _wait_for_resolution_slot(self) -> None:
        previous = self._last_resolution_started_at
        if previous is None:
            return
        remaining = self.resolution_min_interval_seconds - (
            time.monotonic() - previous
        )
        if remaining > 0:
            time.sleep(remaining)

    def _load_instrument_id_cache(self) -> None:
        path = self.instrument_id_cache_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            logger.warning(
                'Ignoring invalid eToro instrument cache | path=%s | error=%s',
                path,
                exc,
            )
            return
        if not isinstance(payload, dict):
            logger.warning(
                'Ignoring non-object eToro instrument cache | path=%s',
                path,
            )
            return
        loaded = 0
        for raw_symbol, raw_instrument_id in payload.items():
            symbol = str(raw_symbol).strip().upper()
            try:
                instrument_id = int(raw_instrument_id)
            except (TypeError, ValueError):
                continue
            if not symbol or instrument_id <= 0:
                continue
            remember_instrument_id(
                instrument_ids_by_symbol=self.instrument_ids_by_symbol,
                symbol_by_instrument_id=self.symbol_by_instrument_id,
                symbol=symbol,
                instrument_id=instrument_id,
            )
            loaded += 1
        logger.info(
            'Loaded eToro instrument cache | path=%s | instruments=%s',
            path,
            loaded,
        )

    def _write_instrument_id_cache(self) -> None:
        path = self.instrument_id_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(
            json.dumps(
                dict(sorted(self.instrument_ids_by_symbol.items())),
                ensure_ascii=False,
                indent=2,
            )
            + '\n',
            encoding='utf-8',
        )
        temporary.replace(path)


def _retry_after_seconds(response) -> float | None:
    value = response.headers.get('Retry-After')
    if value in (None, ''):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)
