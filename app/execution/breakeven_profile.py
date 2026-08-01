from dataclasses import dataclass
from enum import StrEnum

from app.instruments.models import AssetClass

BREAKEVEN_PROFILE_CONTRACT_VERSION = "breakeven_profiles_v1"


class BreakevenProfileName(StrEnum):
    CORRECTED_BASELINE_V1 = "corrected_baseline_v1"
    DELAYED_EQUITY_TRIGGER_V1 = "delayed_equity_trigger_v1"


@dataclass(frozen=True)
class BreakevenProfile:
    name: BreakevenProfileName
    trigger_percent_by_asset_class: dict[AssetClass, float]

    def trigger_percent_for(self, asset_class: AssetClass) -> float:
        try:
            return self.trigger_percent_by_asset_class[asset_class]
        except KeyError as exc:
            raise ValueError(f"Missing breakeven threshold for {asset_class.value}") from exc


BREAKEVEN_PROFILES: dict[BreakevenProfileName, BreakevenProfile] = {
    BreakevenProfileName.CORRECTED_BASELINE_V1: BreakevenProfile(
        name=BreakevenProfileName.CORRECTED_BASELINE_V1,
        trigger_percent_by_asset_class={
            AssetClass.CRYPTO: 0.20,
            AssetClass.EQUITY_EU: 0.55,
            AssetClass.EQUITY_US: 0.60,
        },
    ),
    BreakevenProfileName.DELAYED_EQUITY_TRIGGER_V1: BreakevenProfile(
        name=BreakevenProfileName.DELAYED_EQUITY_TRIGGER_V1,
        trigger_percent_by_asset_class={
            AssetClass.CRYPTO: 0.20,
            AssetClass.EQUITY_EU: 0.65,
            AssetClass.EQUITY_US: 0.70,
        },
    ),
}


def resolve_breakeven_profile(
    name: BreakevenProfileName,
) -> BreakevenProfile:
    return BREAKEVEN_PROFILES[name]
