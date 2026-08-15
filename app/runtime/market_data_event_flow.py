from datetime import datetime

from app.market.data_quality import MarketDataStatus
from app.market_data.models import MarketDataEvent
from app.runtime.pending_entry_flow import write_pending_events
from app.runtime.session_runtime import session_timestamp_rejection_reason


class MarketDataEventFlow:
    def _handle_event(self, event: MarketDataEvent, now: datetime) -> None:
        symbol = event.symbol.strip().upper()
        precheck = self.coordinator.precheck(event)
        self.trade_journal.write(
            'market_data_event_received',
            {
                'symbol': symbol,
                'source': event.source.value,
                'message_id': event.message_id,
                'price_rate_id': event.price_rate_id,
                'connection_id': event.connection_id,
                'price_changed': event.price_changed,
                'state_reconstructed': event.state_reconstructed,
                'received_at': event.received_at,
                'snapshot': event.snapshot,
                'precheck': precheck.reason,
                'loop_id': self.loop_id,
            },
        )
        research_pipeline = getattr(self, 'research_pipeline', None)
        if research_pipeline is not None:
            research_pipeline.observe_payload_schema(event)
        if not precheck.accepted:
            self._record_precheck_rejection(symbol, event, precheck.reason)
            return

        snapshot = event.snapshot
        if snapshot is None:
            return
        config = self._market_data_quality_config(symbol)
        validation = self.market_data_validator.validate(
            snapshot,
            config,
            now=now,
        )
        if validation.status != MarketDataStatus.ACCEPTED:
            event_type = (
                'market_data_quarantined'
                if validation.status == MarketDataStatus.QUARANTINED
                else 'market_data_rejected'
            )
            self.trade_journal.write(
                event_type,
                {
                    'symbol': symbol,
                    'validation': validation,
                    'source': event.source.value,
                    'loop_id': self.loop_id,
                },
            )
            return

        if validation.reasons:
            self.trade_journal.write(
                'market_data_quality_resolved',
                {
                    'symbol': symbol,
                    'validation': validation,
                    'source': event.source.value,
                    'loop_id': self.loop_id,
                },
            )

        self.coordinator.mark_accepted(event)
        self.latest_snapshots[symbol] = snapshot
        self.market_context_service.observe_accepted_snapshot(snapshot)
        if event.price_changed:
            self.market_journal.write(
                'market_price_changed',
                {
                    'symbol': symbol,
                    'snapshot': snapshot,
                    'source': event.source.value,
                    'message_id': event.message_id,
                    'connection_id': event.connection_id,
                    'loop_id': self.loop_id,
                },
            )
        if research_pipeline is not None:
            research_pipeline.observe_accepted_snapshot(snapshot)

        position_session_decision = self.session_decisions.get(symbol)
        self.broker_operations.on_snapshot(
            snapshot=snapshot,
            session_decision=position_session_decision,
            source=event.source.value,
        )

        session_decision = self._session_decision_for_market_data_symbol(symbol)
        if session_decision is None:
            return
        rejection_reason = session_timestamp_rejection_reason(
            decision=session_decision,
            timestamp=snapshot.timestamp,
        )
        if rejection_reason is not None:
            self.trade_journal.write(
                'market_data_event_ignored',
                {
                    'symbol': symbol,
                    'source': event.source.value,
                    'reason': rejection_reason,
                    'message_id': event.message_id,
                    'snapshot_timestamp': snapshot.timestamp,
                    'session_start_time': (
                        session_decision.session_start_time
                    ),
                    'session_end_time': session_decision.session_end_time,
                    'session_key': session_decision.session_key,
                    'position_management_forwarded': True,
                    'loop_id': self.loop_id,
                },
            )
            return

        if symbol not in self.active_symbols:
            return
        self.strategies[symbol].on_snapshot(snapshot)
        self._invalidate_pending_after_symbol_lock(symbol, snapshot.timestamp)

        builder = self.candle_builders[symbol]
        builder.prepare_event(event)
        builder.on_snapshot(snapshot)
        closed_result = builder.take_last_closed_result()
        if closed_result is None:
            return
        self._process_candle_result(
            symbol=symbol,
            result=closed_result,
            latest_snapshot=snapshot,
            session_decision=session_decision,
            now=now,
            source='event',
        )

    def _invalidate_pending_after_symbol_lock(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> None:
        latest_stop_loss = self.cooldown_store.find_latest_initial_stop(
            symbol=symbol
        )
        if latest_stop_loss is None:
            return
        config = self.risk_manager.risk_profile_for(symbol).trade_cooldown
        if (
            latest_stop_loss.symbol_lock_remaining_seconds(
                config=config,
                now=observed_at,
            )
            <= 0
        ):
            return
        write_pending_events(
            self.trade_journal,
            self.pending_entry_manager.invalidate_symbol(
                symbol,
                'initial_stop_symbol_lock_registered',
            ),
        )

    def _record_precheck_rejection(
        self,
        symbol: str,
        event: MarketDataEvent,
        reason: str,
    ) -> None:
        if reason == 'strict_out_of_order_timestamp' and symbol in self.candle_builders:
            self.candle_builders[symbol].record_out_of_order_drop()
        self.trade_journal.write(
            'market_data_event_ignored',
            {
                'symbol': symbol,
                'source': event.source.value,
                'reason': reason,
                'message_id': event.message_id,
                'loop_id': self.loop_id,
            },
        )

    def _market_data_quality_config(self, symbol: str):
        if symbol in self.symbols:
            return self.instrument_registry.config_for(symbol).market_data_quality
        asset_class = self.context_asset_classes.get(symbol)
        if asset_class is None:
            for candidate_asset, configured_symbols in (
                self.settings.benchmark_symbols_by_asset_class().items()
            ):
                if symbol in configured_symbols:
                    asset_class = candidate_asset
                    break
        if asset_class is None:
            asset_class = self.instrument_registry.resolve(symbol).asset_class
        return self.strategy_profile.instrument_config_for_asset_class(
            asset_class
        ).market_data_quality
