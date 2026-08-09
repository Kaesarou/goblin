from datetime import UTC, datetime

from app.instruments.models import AssetClass
from app.market.timeframes import BarCompleteness
from app.runtime.pending_entry_flow import write_pending_events
from app.runtime.runtime_policy import POSITION_RECONCILIATION_INTERVAL_SECONDS
from app.runtime.session_runtime import filter_symbols_by_trading_session
from app.runtime.trading_session_window import TradingSessionDecision
from app.strategies.strategy import TrendStrategy


class MarketDataSessionFlow:
    def _refresh_sessions_if_due(self, now, monotonic_now: float) -> None:
        if monotonic_now - self._last_session_refresh < 1.0:
            return
        self._last_session_refresh = monotonic_now
        (
            active_symbols,
            session_decisions,
            started_symbols,
            completed_session_keys,
        ) = filter_symbols_by_trading_session(
            symbols=self.symbols,
            instrument_registry=self.instrument_registry,
            trading_session_service=self.trading_session_service,
            trading_session_state=self.trading_session_state,
            now=now,
        )
        self.active_symbols = active_symbols
        self.session_decisions = session_decisions
        self.context_asset_classes = self._context_symbols_for_active_assets(
            active_symbols
        )
        for symbol, decision in session_decisions.items():
            self.trade_journal.write(
                'session_state',
                {
                    'symbol': symbol,
                    'session_decision': decision,
                    'loop_id': self.loop_id,
                },
            )

        for session_key in completed_session_keys:
            self.risk_manager.reset_session_trades(session_key)
            self.market_context_service.reset_session(session_key)
            write_pending_events(
                self.trade_journal,
                self.pending_entry_manager.invalidate_session(session_key),
            )
            self.trade_journal.write(
                'session_trades_reset',
                {'session_key': session_key, 'loop_id': self.loop_id},
            )
        for symbol in started_symbols:
            self.market_data_validator.reset_symbol(symbol)
            reset_fallback_quality = getattr(
                self.broker_operations,
                'reset_market_data_quality_symbol',
                None,
            )
            if reset_fallback_quality is not None:
                reset_fallback_quality(symbol)
            self.coordinator.reset_symbol(symbol, now=now)
            self.decision_windows.reset_symbol(symbol)
            self._write_partial_timeframe_bars(
                symbol,
                self.multi_timeframe_service.reset_symbol(symbol),
            )
            self.candle_builders[symbol].reset()
            self.strategies[symbol] = TrendStrategy(
                self.instrument_registry.config_for(symbol).trend
            )
            self._last_bucket_by_symbol.pop(symbol, None)
            self._degraded_buckets = {
                item for item in self._degraded_buckets if item[0] != symbol
            }
            self.trade_journal.write(
                'session_started',
                {
                    'symbol': symbol,
                    'session_decision': session_decisions[symbol],
                    'loop_id': self.loop_id,
                },
            )

        self._synchronize_market_data_subscription()

    def _desired_market_data_symbols(self) -> list[str]:
        open_position_symbols = [
            position.symbol.strip().upper()
            for position in self.position_tracker.open_positions_snapshot()
        ]
        return list(
            dict.fromkeys(
                [
                    *self.active_symbols,
                    *self.context_asset_classes,
                    *open_position_symbols,
                ]
            )
        )

    def _synchronize_market_data_subscription(self) -> None:
        if not self._feed_started:
            return
        desired = tuple(self._desired_market_data_symbols())
        if desired == self._subscribed_symbols:
            return
        previous = set(self._subscribed_symbols)
        current = set(desired)
        added = sorted(current - previous)
        removed = sorted(previous - current)

        self.live_market_data.update_symbols(list(desired))
        self._subscribed_symbols = desired
        self.trade_journal.write(
            'market_data_subscription_requested',
            {
                'added_symbols': added,
                'removed_symbols': removed,
                'requested_symbols': list(desired),
                'requested_at': datetime.now(UTC),
                'loop_id': self.loop_id,
            },
        )

    def _refresh_applied_market_data_subscription(self, now: datetime) -> None:
        applied = tuple(self.live_market_data.subscribed_symbols())
        if applied == self._applied_feed_symbols:
            return
        previous = set(self._applied_feed_symbols)
        current = set(applied)
        added = sorted(current - previous)
        removed = sorted(previous - current)
        for symbol in added:
            self.coordinator.reset_symbol(symbol, now=now)
        self._applied_feed_symbols = applied
        self.trade_journal.write(
            'market_data_subscription_applied',
            {
                'added_symbols': added,
                'removed_symbols': removed,
                'subscribed_symbols': list(applied),
                'applied_at': now,
                'loop_id': self.loop_id,
            },
        )

    def _reconcile_positions_if_due(self, now, monotonic_now: float) -> None:
        if (
            monotonic_now - self._last_position_reconciliation
            < POSITION_RECONCILIATION_INTERVAL_SECONDS
        ):
            return
        self._last_position_reconciliation = monotonic_now
        self.cooldown_store.delete_expired(now)
        self.broker_operations.schedule_reconciliation(now=now)

    def _context_symbols_for_active_assets(
        self,
        active_symbols: list[str],
    ) -> dict[str, AssetClass]:
        active_assets = {
            self.instrument_registry.resolve(symbol).asset_class
            for symbol in active_symbols
        }
        result: dict[str, AssetClass] = {}
        for asset_class, symbols in (
            self.settings.benchmark_symbols_by_asset_class().items()
        ):
            if asset_class not in active_assets:
                continue
            for symbol in symbols:
                if symbol not in active_symbols:
                    result[symbol] = asset_class
        return result

    def _session_decision_for_market_data_symbol(
        self,
        symbol: str,
    ) -> TradingSessionDecision | None:
        normalized = symbol.strip().upper()
        if normalized in self.active_symbols:
            return self.session_decisions.get(normalized)

        asset_class = self.context_asset_classes.get(normalized)
        if asset_class is None:
            return None
        for active_symbol in self.active_symbols:
            decision = self.session_decisions.get(active_symbol)
            if (
                decision is not None
                and decision.collect_snapshots
                and decision.asset_class == asset_class
            ):
                return decision
        return None

    def _write_partial_timeframe_bars(self, symbol: str, bars) -> None:
        for bar in bars:
            event_type = (
                'timeframe_bar_partial'
                if bar.completeness == BarCompleteness.PARTIAL
                else 'timeframe_bar_incomplete'
            )
            self.candle_journal.write(
                event_type,
                {
                    'symbol': symbol,
                    'timeframe': bar.timeframe.name.lower(),
                    'timeframe_bar': bar,
                    'loop_id': self.loop_id,
                },
            )
