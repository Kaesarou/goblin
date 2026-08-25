from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from app.market.candle_builder import CandleBuilder
from app.market.models import MarketSnapshot
from app.market_data.models import (
    CandleBuildResult,
    CandleQuality,
    MarketDataEvent,
    MarketDataSource,
)


class QualityAwareCandleBuilder:
    def __init__(
        self,
        *,
        ordering_drop_degrade_count: int = 3,
        ordering_drop_degrade_ratio: float = 0.10,
    ) -> None:
        self._builder = CandleBuilder()
        self._bucket: datetime | None = None
        self._messages = 0
        self._price_events = 0
        self._fallback_events = 0
        self._out_of_order_drops = 0
        self._sources: Counter[str] = Counter()
        self._pending_event: MarketDataEvent | None = None
        self._last_closed_result: CandleBuildResult | None = None
        self._last_bucket_snapshot: MarketSnapshot | None = None
        self.ordering_drop_degrade_count = max(
            1,
            ordering_drop_degrade_count,
        )
        self.ordering_drop_degrade_ratio = max(
            0.0,
            ordering_drop_degrade_ratio,
        )

    def reset(self) -> None:
        self._builder.reset()
        self._bucket = None
        self._pending_event = None
        self._last_closed_result = None
        self._last_bucket_snapshot = None
        self._reset_quality()

    def prepare_event(self, event: MarketDataEvent) -> None:
        self._pending_event = event

    def take_last_closed_result(self) -> CandleBuildResult | None:
        result = self._last_closed_result
        self._last_closed_result = None
        return result

    def record_out_of_order_drop(self) -> None:
        self._out_of_order_drops += 1

    def on_snapshot(self, snapshot: MarketSnapshot):
        event = self._pending_event
        self._pending_event = None
        if event is None:
            event = MarketDataEvent(
                symbol=snapshot.symbol,
                source=MarketDataSource.WEBSOCKET,
                received_at=snapshot.received_at or snapshot.timestamp,
                snapshot=snapshot,
                price_changed=True,
            )
        result = self.on_event(event)
        self._last_closed_result = result
        return result.candle if result is not None else None

    def on_event(self, event: MarketDataEvent) -> CandleBuildResult | None:
        snapshot = event.snapshot
        if snapshot is None:
            return None
        bucket = snapshot.timestamp.astimezone(timezone.utc).replace(
            second=0,
            microsecond=0,
        )
        if self._bucket is None:
            self._bucket = bucket
            self._last_bucket_snapshot = snapshot
            self._record_event(event, forwarded=True)
            self._builder.on_snapshot(snapshot)
            return None

        if bucket < self._bucket:
            self.record_out_of_order_drop()
            return None

        if bucket == self._bucket:
            # A complete quote may update bid/ask even when the canonical last
            # price is unchanged. Preserve it for causal execution economics,
            # while OHLC still only consumes price_changed events.
            self._last_bucket_snapshot = snapshot
            self._record_event(event, forwarded=event.price_changed)
            if event.price_changed:
                self._builder.on_snapshot(snapshot)
            return None

        closed_decision_snapshot = self._last_bucket_snapshot
        closed_quality_base = replace(
            self._quality(
                carried_forward=False,
                last_price_age_seconds=None,
                max_carry_forward_age_seconds=None,
            ),
            decision_snapshot=closed_decision_snapshot,
        )
        closed = self._builder.on_snapshot(snapshot)
        carried, price_age = self._builder.take_last_closed_metadata()
        self._bucket = bucket
        self._last_bucket_snapshot = snapshot
        self._reset_quality()
        self._record_event(event, forwarded=True)
        if closed is None:
            return None
        return CandleBuildResult(
            candle=closed,
            quality=self._merge_clock_metadata(
                closed_quality_base,
                carried_forward=carried,
                last_price_age_seconds=price_age,
                max_carry_forward_age_seconds=None,
            ),
            decision_snapshot=closed_decision_snapshot,
        )

    def finalize_until(
        self,
        now: datetime,
        *,
        grace_seconds: float,
        max_carry_forward_age_seconds: float,
    ) -> list[CandleBuildResult]:
        closed_bucket_snapshot = self._last_bucket_snapshot
        finalized = self._builder.finalize_until(
            now,
            grace_seconds=grace_seconds,
        )
        if not finalized:
            return []

        results: list[CandleBuildResult] = []
        for index, (candle, carried, price_age) in enumerate(finalized):
            decision_snapshot = (
                closed_bucket_snapshot
                if index == 0
                and closed_bucket_snapshot is not None
                and candle.opened_at <= closed_bucket_snapshot.timestamp < candle.closed_at
                else None
            )
            quality = replace(
                self._quality(
                    carried_forward=carried,
                    last_price_age_seconds=price_age,
                    max_carry_forward_age_seconds=max_carry_forward_age_seconds,
                ),
                decision_snapshot=decision_snapshot,
            )
            results.append(
                CandleBuildResult(
                    candle=candle,
                    quality=quality,
                    decision_snapshot=decision_snapshot,
                )
            )
            self._bucket = candle.closed_at
            self._last_bucket_snapshot = None
            self._reset_quality()
            if index + 1 < len(finalized):
                self._bucket = candle.closed_at
        return results

    def _record_event(self, event: MarketDataEvent, *, forwarded: bool) -> None:
        self._messages += 1
        self._sources[event.source.value] += 1
        if forwarded:
            self._price_events += 1
        if event.source == MarketDataSource.REST_FALLBACK:
            self._fallback_events += 1

    def _reset_quality(self) -> None:
        self._messages = 0
        self._price_events = 0
        self._fallback_events = 0
        self._out_of_order_drops = 0
        self._sources = Counter()

    def _quality(
        self,
        *,
        carried_forward: bool,
        last_price_age_seconds: float | None,
        max_carry_forward_age_seconds: float | None,
    ) -> CandleQuality:
        source = (
            next(iter(self._sources))
            if len(self._sources) == 1
            else ('mixed' if self._sources else 'carried_forward')
        )
        ordering_denominator = max(
            1,
            self._messages + self._out_of_order_drops,
        )
        ordering_ratio = self._out_of_order_drops / ordering_denominator
        reasons: list[str] = []
        if self._fallback_events > 0:
            reasons.append('rest_fallback')
        if source == 'mixed':
            reasons.append('mixed_sources')
        if (
            self._out_of_order_drops >= self.ordering_drop_degrade_count
            and ordering_ratio >= self.ordering_drop_degrade_ratio
        ):
            reasons.append('ordering_drop_rate')
        if (
            carried_forward
            and max_carry_forward_age_seconds is not None
            and last_price_age_seconds is not None
            and last_price_age_seconds > max_carry_forward_age_seconds
        ):
            reasons.append('stale_carried_forward_price')
        return CandleQuality(
            source=source,
            message_count=self._messages,
            price_event_count=self._price_events,
            fallback_event_count=self._fallback_events,
            out_of_order_drop_count=self._out_of_order_drops,
            degraded=bool(reasons),
            carried_forward=carried_forward,
            last_price_age_seconds=last_price_age_seconds,
            ordering_drop_ratio=round(ordering_ratio, 6),
            degraded_reasons=tuple(reasons),
        )

    def _merge_clock_metadata(
        self,
        quality: CandleQuality,
        *,
        carried_forward: bool,
        last_price_age_seconds: float | None,
        max_carry_forward_age_seconds: float | None,
    ) -> CandleQuality:
        reasons = list(quality.degraded_reasons)
        if (
            carried_forward
            and max_carry_forward_age_seconds is not None
            and last_price_age_seconds is not None
            and last_price_age_seconds > max_carry_forward_age_seconds
            and 'stale_carried_forward_price' not in reasons
        ):
            reasons.append('stale_carried_forward_price')
        return CandleQuality(
            source=quality.source,
            message_count=quality.message_count,
            price_event_count=quality.price_event_count,
            fallback_event_count=quality.fallback_event_count,
            out_of_order_drop_count=quality.out_of_order_drop_count,
            degraded=bool(reasons),
            carried_forward=carried_forward,
            last_price_age_seconds=last_price_age_seconds,
            ordering_drop_ratio=quality.ordering_drop_ratio,
            degraded_reasons=tuple(reasons),
            decision_snapshot=quality.decision_snapshot,
        )