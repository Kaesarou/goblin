from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrokerCapabilities:
    name: str
    supports_short: bool
    supports_fractional: bool
    supports_partial_close: bool
    supports_limit_open: bool
    supports_limit_close: bool
    supports_native_stop: bool
    supports_client_order_id: bool


ETORO_CURRENT_CAPABILITIES = BrokerCapabilities(
    name="etoro",
    supports_short=True,
    supports_fractional=True,
    supports_partial_close=True,
    supports_limit_open=False,
    supports_limit_close=False,
    supports_native_stop=True,
    supports_client_order_id=False,
)
