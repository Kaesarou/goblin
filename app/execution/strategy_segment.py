from __future__ import annotations

from enum import StrEnum

from app.instruments.models import AssetClass


class StrategySegment(StrEnum):
    EQUITY_EU_BUY = 'EQUITY_EU_BUY'
    EQUITY_EU_SELL = 'EQUITY_EU_SELL'
    EQUITY_US_BUY = 'EQUITY_US_BUY'
    EQUITY_US_SELL = 'EQUITY_US_SELL'
    CRYPTO_BUY = 'CRYPTO_BUY'
    CRYPTO_SELL = 'CRYPTO_SELL'

    @property
    def asset_class(self) -> AssetClass:
        if self.value.startswith('EQUITY_EU_'):
            return AssetClass.EQUITY_EU
        if self.value.startswith('EQUITY_US_'):
            return AssetClass.EQUITY_US
        return AssetClass.CRYPTO

    @property
    def side(self) -> str:
        return 'BUY' if self.value.endswith('_BUY') else 'SELL'

    @property
    def is_equity(self) -> bool:
        return self.asset_class in {
            AssetClass.EQUITY_EU,
            AssetClass.EQUITY_US,
        }

    @classmethod
    def from_asset_and_side(
        cls,
        asset_class: AssetClass | str,
        side: str,
    ) -> StrategySegment:
        asset = (
            asset_class.value
            if isinstance(asset_class, AssetClass)
            else str(asset_class).strip().upper()
        )
        normalized_side = side.strip().upper()
        if normalized_side not in {'BUY', 'SELL'}:
            raise ValueError(f'Unsupported strategy side: {side!r}.')
        try:
            return cls(f'{asset}_{normalized_side}')
        except ValueError as exc:
            raise ValueError(
                f'Unsupported strategy segment: {asset}_{normalized_side}.'
            ) from exc


EQUITY_STRATEGY_SEGMENTS = (
    StrategySegment.EQUITY_EU_BUY,
    StrategySegment.EQUITY_EU_SELL,
    StrategySegment.EQUITY_US_BUY,
    StrategySegment.EQUITY_US_SELL,
)

SUPPORTED_STRATEGY_SEGMENTS = (
    *EQUITY_STRATEGY_SEGMENTS,
    StrategySegment.CRYPTO_BUY,
    StrategySegment.CRYPTO_SELL,
)
