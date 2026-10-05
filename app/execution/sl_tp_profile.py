from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import RiskProfile
from app.risk.structural_stop import calculate_structural_stop
from app.strategies.signals import Signal
from app.utils.commons import spread_percent

SlTpMode = Literal['fixed']
SlTpSource = str


@dataclass(frozen=True)
class EffectiveSlTp:
    stop_loss_percent: float
    take_profit_percent: float
    atr_percent: float | None
    mode: SlTpMode
    source: SlTpSource
    metadata: dict[str, Any] = field(default_factory=dict)


class EffectiveSlTpResolver:
    def resolve(
        self,
        *,
        candidate: TradeCandidate,
        risk_profile: RiskProfile,
    ) -> EffectiveSlTp:
        baseline = self.resolve_for_signal(
            signal=candidate.signal,
            risk_profile=risk_profile,
        )
        metadata = candidate.signal.metadata or {}
        if metadata.get('entry_origin') != 'pending_confirmation':
            return baseline

        raw_invalidation = metadata.get('structural_invalidation_price')
        try:
            invalidation_price = (
                float(raw_invalidation)
                if raw_invalidation is not None
                else None
            )
        except (TypeError, ValueError):
            invalidation_price = None
        confirmation = risk_profile.entry_confirmation
        structural = calculate_structural_stop(
            side=candidate.signal.action,
            entry_price=candidate.snapshot.last,
            invalidation_price=invalidation_price,
            atr_percent=baseline.atr_percent,
            atr_multiplier=confirmation.structural_stop_atr_multiplier,
            minimum_distance_percent=(
                confirmation.minimum_structural_stop_percent
            ),
            spread_percent=spread_percent(candidate.snapshot),
        )
        structural_metadata = {
            **baseline.metadata,
            'entry_origin': 'pending_confirmation',
            'structural_confirmation_satisfied': True,
            'structural_stop_valid': structural.valid,
            'structural_stop_reason': structural.reason,
            'structural_invalidation_price': structural.invalidation_price,
            'structural_distance_percent': (
                structural.structural_distance_percent
            ),
            'volatility_distance_percent': (
                structural.volatility_distance_percent
            ),
            'minimum_distance_percent': structural.minimum_distance_percent,
            'maximum_distance_percent': (
                confirmation.maximum_structural_stop_percent
            ),
            'constant_risk_baseline_stop_loss_percent': (
                baseline.stop_loss_percent
            ),
            'baseline_sl_tp_source': baseline.source,
        }
        return EffectiveSlTp(
            stop_loss_percent=(
                structural.effective_distance_percent
                if structural.valid
                else baseline.stop_loss_percent
            ),
            take_profit_percent=baseline.take_profit_percent,
            atr_percent=baseline.atr_percent,
            mode='fixed',
            source='pending_structural',
            metadata=structural_metadata,
        )

    def resolve_for_signal(
        self,
        *,
        signal: Signal,
        risk_profile: RiskProfile,
    ) -> EffectiveSlTp:
        atr_percent = self._atr_percent_from_signal(signal)
        directional_override = risk_profile.directional_override_for(
            signal.action
        )
        if directional_override is not None:
            return EffectiveSlTp(
                stop_loss_percent=directional_override.stop_loss_percent,
                take_profit_percent=directional_override.take_profit_percent,
                atr_percent=atr_percent,
                mode='fixed',
                source=directional_override.source,
                metadata={
                    'profile_key': directional_override.source,
                    'side': signal.action,
                },
            )
        return EffectiveSlTp(
            stop_loss_percent=risk_profile.stop_loss_percent,
            take_profit_percent=risk_profile.take_profit_percent,
            atr_percent=atr_percent,
            mode='fixed',
            source=risk_profile.profile_key,
            metadata={
                'profile_key': risk_profile.profile_key,
                'side': signal.action,
            },
        )

    def _atr_percent_from_signal(self, signal: Signal) -> float | None:
        raw_atr_percent = (signal.metadata or {}).get('atr_percent')
        if raw_atr_percent is None:
            return None
        try:
            atr_percent = float(raw_atr_percent)
        except (TypeError, ValueError):
            return None
        return atr_percent if atr_percent > 0 else None
