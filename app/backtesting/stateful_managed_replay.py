from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from app.config.settings import Settings
from app.execution.candidate_economics import (
    CandidateEconomicsEstimator,
    EvaluatedTradeCandidate,
)
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_economics import calculate_position_pnl
from app.execution.position_models import ClosedPosition, PositionCloseSignal
from app.execution.position_tracker import PositionTracker
from app.execution.scoring.managed_outcome import (
    CandidateManagedOutcomeEvaluator,
)
from app.execution.scoring.outcome_probability import (
    CandidateOutcomeProbabilityEvaluator,
)
from app.execution.scoring.tp_feasibility import (
    CandidateTpFeasibilityEvaluator,
)
from app.execution.trade_candidate import TradeCandidate
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.market.models import MarketSnapshot
from app.risk.position_sizing import FixedPercentPositionSizing
from app.risk.risk_manager import RiskManager
from app.risk.trade_cooldown import (
    ClosedTradeMemoryEntry,
    build_closed_trade_memory_entry,
)
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.runtime.candidate_flow import (
    attach_entry_decisions,
    select_evaluated_trade_candidates_with_strategy_profile,
)
from app.runtime.trading_session_window import (
    TradingSessionService,
    trading_session_service_from_settings,
)
from app.strategies.balanced_strategy_config import BalancedStrategyConfig
from app.utils.commons import spread_percent

STATEFUL_MANAGED_REPLAY_CONTRACT_VERSION = "stateful_managed_replay_v1"


@dataclass(frozen=True)
class ReplayCandidateBatch:
    occurred_at: datetime
    account_equity: float
    candidates: tuple[TradeCandidate, ...]


@dataclass(frozen=True)
class ReplayTrade:
    candidate_id: str
    origin_candidate_id: str
    pending_entry_id: str | None
    position_id: str
    symbol: str
    asset_class: str
    side: str
    session_key: str
    opened_at: datetime
    closed_at: datetime
    close_reason: str
    amount: float
    signal_price: float
    pnl_entry_price: float
    pnl_exit_price: float
    gross_pnl: float
    explicit_costs_deducted: float
    net_pnl: float
    gross_pnl_percent: float
    net_pnl_percent: float
    duration_seconds: float
    mfe_percent: float
    mae_percent: float
    managed_edge_at_entry: float
    managed_time_expected_return_contribution: float


@dataclass
class _OpenTradeMetadata:
    candidate: TradeCandidate
    asset_class: AssetClass
    managed_edge: float
    managed_time_expected_return_contribution: float


@dataclass
class _ProtectedExitCounterfactual:
    candidate_id: str
    symbol: str
    side: str
    amount: float
    pnl_entry_price: float
    explicit_cost: float
    initial_stop_loss: float
    take_profit: float
    protected_reason: PositionCloseReason
    actual_exit_at: datetime
    actual_net_pnl: float
    horizon_at: datetime
    resolved_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_price: float | None = None
    counterfactual_net_pnl: float | None = None


class _ReplayClosedTradeMemory:
    def __init__(self) -> None:
        self.entries: list[ClosedTradeMemoryEntry] = []

    def save_or_replace(self, entry: ClosedTradeMemoryEntry) -> None:
        self.entries = [
            item
            for item in self.entries
            if not (item.position_id == entry.position_id and item.symbol == entry.symbol)
        ]
        self.entries.append(entry)

    def find_latest_initial_stop(
        self,
        *,
        symbol: str,
    ) -> ClosedTradeMemoryEntry | None:
        return self._latest(
            item
            for item in self.entries
            if item.symbol == symbol and item.close_reason is PositionCloseReason.INITIAL_STOP
        )

    def find_active_cooldown(
        self,
        *,
        symbol: str,
        side: str,
        now: datetime,
    ) -> ClosedTradeMemoryEntry | None:
        return self._latest(
            item
            for item in self.entries
            if item.symbol == symbol and item.side == side and item.cooldown_expires_at > now
        )

    def find_recent_take_profit(
        self,
        *,
        symbol: str,
        side: str,
        now: datetime,
        lookback_minutes: int,
    ) -> ClosedTradeMemoryEntry | None:
        cutoff = now - timedelta(minutes=lookback_minutes)
        return self._latest(
            item
            for item in self.entries
            if item.symbol == symbol
            and item.side == side
            and item.close_reason is PositionCloseReason.TAKE_PROFIT
            and cutoff <= item.closed_at <= now
        )

    @staticmethod
    def _latest(values) -> ClosedTradeMemoryEntry | None:
        items = list(values)
        return max(items, key=lambda item: item.closed_at) if items else None


class StatefulManagedReplay:
    """Chronological MANAGED_EDGE_V1 portfolio replay using live contracts."""

    def __init__(
        self,
        *,
        settings: Settings,
        strategy_profile: BalancedStrategyConfig,
        scenario_name: str,
    ) -> None:
        self.settings = settings
        self.strategy_profile = strategy_profile
        self.scenario_name = scenario_name
        self.instrument_registry = InstrumentRegistry(
            settings,
            instrument_configs=strategy_profile.instrument_configs,
        )
        self.risk_manager = RiskManager(
            settings,
            FixedPercentPositionSizing(),
            self.instrument_registry,
        )
        self.economics = CandidateEconomicsEstimator(
            FixedPercentPositionSizing(),
            self.instrument_registry,
        )
        self.tp_feasibility = CandidateTpFeasibilityEvaluator()
        self.outcome_probability = CandidateOutcomeProbabilityEvaluator()
        self.managed_outcome = CandidateManagedOutcomeEvaluator()
        self.position_tracker = PositionTracker()
        self.cooldown_store = _ReplayClosedTradeMemory()
        self.cooldown_guard = TradeCooldownGuard(self.cooldown_store)
        self.session_service: TradingSessionService = trading_session_service_from_settings(
            settings
        )
        self.open_metadata: dict[str, _OpenTradeMetadata] = {}
        self.trades: list[ReplayTrade] = []
        self.latest_snapshot_by_symbol: dict[str, MarketSnapshot] = {}
        self.counterfactuals: list[_ProtectedExitCounterfactual] = []
        self.counts: Counter[str] = Counter()
        self.model_rejections: Counter[str] = Counter()
        self.risk_rejections: Counter[str] = Counter()
        self.cooldown_rejections: Counter[str] = Counter()
        self.first_event_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.peak_open_positions = 0
        self._intraday_equity_peak = 0.0
        self.max_intraday_equity_drawdown = 0.0

    def on_candidate_batch(self, batch: ReplayCandidateBatch) -> None:
        self._observe_time(batch.occurred_at)
        candidates = list(batch.candidates)
        self.counts["candidate_batches"] += 1
        self.counts["candidates"] += len(candidates)
        self.counts["pending_candidates"] += sum(
            candidate.pending_entry_id is not None for candidate in candidates
        )

        cooldown = self.cooldown_guard.filter_candidates(
            candidates=candidates,
            risk_manager=self.risk_manager,
            now=batch.occurred_at,
        )
        for rejected in cooldown.rejected_candidates:
            reason = rejected.decision.reason or "cooldown_active"
            self.cooldown_rejections[reason] += 1
            self.counts["prevented_by_cooldown"] += 1

        evaluated = [
            self._evaluate_candidate(candidate, batch.account_equity)
            for candidate in cooldown.selected_candidates
        ]
        selection = select_evaluated_trade_candidates_with_strategy_profile(
            evaluated,
            self.risk_manager,
            self.strategy_profile,
        )
        for rejected in selection.rejected_candidates:
            self.model_rejections[rejected.reason] += 1
        self.counts["selected_after_top_n"] += len(selection.selected_candidates)

        for evaluated_candidate in selection.selected_candidates:
            candidate = evaluated_candidate.candidate
            plan = self.risk_manager.evaluate(
                signal=candidate.signal,
                snapshot=candidate.snapshot,
                account_equity=batch.account_equity,
                session_key=candidate.session_key,
                effective_sl_tp=evaluated_candidate.effective_sl_tp,
            )
            if not plan.approved:
                self.risk_rejections[plan.reason] += 1
                if plan.reason in {
                    "max_open_positions_reached",
                    "max_open_positions_per_symbol_reached",
                }:
                    self.counts["prevented_by_capacity"] += 1
                continue

            entry_estimate = candidate.snapshot.executable_entry_price(candidate.signal.action)
            adjusted_plan = self.risk_manager.adjust_trade_plan_to_entry_price(
                trade_plan=plan,
                entry_price=entry_estimate,
            )
            position_id = self._position_id(candidate, batch.occurred_at)
            self.position_tracker.record_open_position(
                position_id=position_id,
                trade_plan=adjusted_plan,
                signal_price=candidate.snapshot.last,
                executable_entry_estimate=entry_estimate,
                broker_entry_fill_price=None,
                opened_at=batch.occurred_at,
            )
            self.risk_manager.record_open_position(
                candidate.symbol,
                candidate.session_key,
            )
            time_adjustment = candidate.managed_outcome_metadata.get("time_adjustment") or {}
            self.open_metadata[position_id] = _OpenTradeMetadata(
                candidate=candidate,
                asset_class=self.instrument_registry.resolve(candidate.symbol).asset_class,
                managed_edge=float(candidate.managed_edge or 0.0),
                managed_time_expected_return_contribution=float(
                    time_adjustment.get("expected_return_contribution") or 0.0
                ),
            )
            self.latest_snapshot_by_symbol[candidate.symbol] = candidate.snapshot
            self.counts["trades_opened"] += 1
            if candidate.pending_entry_id is not None:
                self.counts["pending_trades_opened"] += 1
            self.peak_open_positions = max(
                self.peak_open_positions,
                len(self.position_tracker.open_positions_snapshot()),
            )
        self._update_intraday_equity_drawdown()

    def on_market_snapshot(
        self,
        *,
        snapshot: MarketSnapshot,
        occurred_at: datetime,
    ) -> None:
        self._observe_time(occurred_at)
        self.latest_snapshot_by_symbol[snapshot.symbol] = snapshot
        self._advance_counterfactuals(snapshot, occurred_at=occurred_at)
        if not any(
            position.symbol == snapshot.symbol
            for position in self.position_tracker.open_positions_snapshot()
        ):
            return
        asset_class = self.instrument_registry.resolve(snapshot.symbol).asset_class
        session_decision = self.session_service.evaluate(
            asset_class=asset_class,
            now=occurred_at,
        )
        close_signals = self.position_tracker.evaluate_snapshot(
            snapshot,
            force_close=session_decision.force_close_required,
            force_close_metadata={
                "session_decision": session_decision.reason,
                "time_until_session_end_minutes": (session_decision.time_until_session_end_minutes),
            },
        )
        self.counts["managed_stop_updates"] += len(
            self.position_tracker.consume_managed_stop_updates()
        )
        for signal in close_signals:
            self._close_position(signal)
        self._update_intraday_equity_drawdown()

    def result(self) -> dict[str, Any]:
        mark_to_market = self._mark_to_market()
        realized_net = sum(item.net_pnl for item in self.trades)
        realized_gross = sum(item.gross_pnl for item in self.trades)
        explicit_costs = sum(item.explicit_costs_deducted for item in self.trades)
        durations = [item.duration_seconds for item in self.trades]
        coverage_seconds = (
            max(
                0.0,
                (self.last_event_at - self.first_event_at).total_seconds(),
            )
            if self.first_event_at is not None and self.last_event_at is not None
            else 0.0
        )
        capital_minutes = sum(
            item.amount * item.duration_seconds / 60 for item in self.trades
        ) + sum(
            item["amount"] * item["duration_seconds"] / 60 for item in mark_to_market["positions"]
        )
        return {
            "contract_version": STATEFUL_MANAGED_REPLAY_CONTRACT_VERSION,
            "scenario": self.scenario_name,
            "breakeven_profile": self.strategy_profile.breakeven_profile_name,
            "breakeven_trigger_percent": {
                asset.value: config.risk.breakeven_trigger_percent
                for asset, config in self.strategy_profile.instrument_configs.items()
            },
            "coverage": {
                "first_event_at": self.first_event_at,
                "last_event_at": self.last_event_at,
                "seconds": round(coverage_seconds, 3),
            },
            "pnl": {
                "realized_gross": round(realized_gross, 4),
                "explicit_costs_deducted": round(explicit_costs, 4),
                "realized_net": round(realized_net, 4),
                "mark_to_market_net": mark_to_market["net_total"],
                "realized_plus_mark_to_market_net": round(
                    realized_net + mark_to_market["net_total"],
                    4,
                ),
                "max_realized_drawdown": self._max_drawdown(),
                "max_intraday_equity_drawdown": round(
                    self.max_intraday_equity_drawdown,
                    4,
                ),
            },
            "trades": {
                "count": len(self.trades),
                "by_close_reason": dict(Counter(item.close_reason for item in self.trades)),
                "by_asset_class": self._pnl_breakdown("asset_class"),
                "by_side": self._pnl_breakdown("side"),
                "average_duration_seconds": (round(mean(durations), 3) if durations else None),
                "average_mfe_percent": self._average_trade_field("mfe_percent"),
                "average_mae_percent": self._average_trade_field("mae_percent"),
                "details": [asdict(item) for item in self.trades],
            },
            "portfolio": {
                "peak_simultaneous_positions": self.peak_open_positions,
                "capital_immobilized_usd_minutes": round(capital_minutes, 4),
                "average_reserved_capital_usd": (
                    round(capital_minutes / (coverage_seconds / 60), 4)
                    if coverage_seconds > 0
                    else 0.0
                ),
                "open_positions_at_last_tick": len(mark_to_market["positions"]),
            },
            "constraints": {
                **dict(self.counts),
                "model_rejections": dict(self.model_rejections),
                "risk_rejections": dict(self.risk_rejections),
                "cooldown_rejections": dict(self.cooldown_rejections),
            },
            "protected_exit_counterfactuals": self._counterfactual_summary(),
            "mark_to_market": mark_to_market,
        }

    def _evaluate_candidate(
        self,
        candidate: TradeCandidate,
        account_equity: float,
    ) -> EvaluatedTradeCandidate:
        evaluated = self.economics.evaluate(candidate, account_equity)
        risk_profile = self.risk_manager.risk_profile_for(candidate.symbol)
        evaluated = self.tp_feasibility.evaluate(
            evaluated_candidate=evaluated,
            risk_profile=risk_profile,
        )
        evaluated = attach_entry_decisions([evaluated])[0]
        evaluated = self.outcome_probability.evaluate(
            evaluated_candidate=evaluated,
            risk_profile=risk_profile,
        )
        return self.managed_outcome.evaluate(evaluated_candidate=evaluated)

    def _close_position(self, signal: PositionCloseSignal) -> None:
        tracked = next(
            position
            for position in self.position_tracker.open_positions_snapshot()
            if position.position_id == signal.position_id
        )
        closed = self.position_tracker.record_closed_position(
            signal,
            confirmed_at=signal.detected_at,
        )
        if closed is None:
            raise RuntimeError(f"Position disappeared before close: {signal}")
        metadata = self.open_metadata.pop(closed.position_id)
        self.risk_manager.record_close_position(closed.symbol)
        self._register_cooldown(closed, metadata)
        self.trades.append(self._replay_trade(closed, metadata))
        self.counts["trades_closed"] += 1
        if signal.reason in {
            PositionCloseReason.PROTECTED_BREAKEVEN,
            PositionCloseReason.PROTECTED_TRAILING,
        }:
            self.counterfactuals.append(
                _ProtectedExitCounterfactual(
                    candidate_id=metadata.candidate.candidate_id,
                    symbol=closed.symbol,
                    side=closed.side,
                    amount=closed.amount,
                    pnl_entry_price=closed.pnl_entry_price,
                    explicit_cost=closed.explicit_costs_deducted,
                    initial_stop_loss=float(tracked.initial_stop_loss or tracked.stop_loss),
                    take_profit=closed.take_profit,
                    protected_reason=signal.reason,
                    actual_exit_at=closed.closed_at,
                    actual_net_pnl=closed.net_pnl,
                    horizon_at=closed.opened_at
                    + timedelta(minutes=tracked.stale_position_max_age_minutes),
                )
            )

    def _register_cooldown(
        self,
        closed: ClosedPosition,
        metadata: _OpenTradeMetadata,
    ) -> None:
        config = self.risk_manager.risk_profile_for(closed.symbol).trade_cooldown
        entry = build_closed_trade_memory_entry(
            symbol=closed.symbol,
            side=closed.side,
            config=config,
            close_reason=closed.close_reason,
            closed_at=closed.closed_at,
            position_id=closed.position_id,
            opened_at=closed.opened_at,
            signal_price=closed.signal_price,
            executable_entry_estimate=closed.executable_entry_estimate,
            broker_entry_fill_price=closed.broker_entry_fill_price,
            pnl_entry_price=closed.pnl_entry_price,
            pnl_exit_price=closed.pnl_exit_price,
            exit_price_source=closed.exit_price_source,
            stop_loss=closed.stop_loss,
            take_profit=closed.take_profit,
            highest_executable_price=closed.highest_executable_price,
            lowest_executable_price=closed.lowest_executable_price,
            gross_pnl=closed.gross_pnl,
            gross_pnl_percent=closed.gross_pnl_percent,
            explicit_costs_deducted=closed.explicit_costs_deducted,
            net_pnl=closed.net_pnl,
            net_pnl_percent=closed.net_pnl_percent,
            created_at=closed.closed_at,
            session_key=metadata.candidate.session_key,
        )
        self.cooldown_store.save_or_replace(entry)

    def _advance_counterfactuals(
        self,
        snapshot: MarketSnapshot,
        *,
        occurred_at: datetime,
    ) -> None:
        for item in self.counterfactuals:
            if item.resolved_reason is not None or item.symbol != snapshot.symbol:
                continue
            if snapshot.timestamp <= item.actual_exit_at:
                continue
            executable = snapshot.executable_exit_price(item.side)
            take_profit_hit = (
                executable >= item.take_profit
                if item.side == "BUY"
                else executable <= item.take_profit
            )
            initial_stop_hit = (
                executable <= item.initial_stop_loss
                if item.side == "BUY"
                else executable >= item.initial_stop_loss
            )
            asset_class = self.instrument_registry.resolve(item.symbol).asset_class
            force_close = self.session_service.evaluate(
                asset_class=asset_class,
                now=occurred_at,
            ).force_close_required
            if take_profit_hit:
                self._resolve_counterfactual(item, "take_profit", snapshot)
            elif initial_stop_hit:
                self._resolve_counterfactual(item, "initial_stop", snapshot)
            elif snapshot.timestamp >= item.horizon_at:
                self._resolve_counterfactual(item, "stale_horizon", snapshot)
            elif force_close:
                self._resolve_counterfactual(item, "session_force_close", snapshot)

    def _resolve_counterfactual(
        self,
        item: _ProtectedExitCounterfactual,
        reason: str,
        snapshot: MarketSnapshot,
    ) -> None:
        price = snapshot.executable_exit_price(item.side)
        pnl = calculate_position_pnl(
            side=item.side,
            amount=item.amount,
            entry_price=item.pnl_entry_price,
            exit_price=price,
            explicit_cost=item.explicit_cost,
            explicit_cost_percent=(
                item.explicit_cost / item.amount * 100 if item.amount > 0 else 0.0
            ),
        )
        item.resolved_reason = reason
        item.resolved_at = snapshot.timestamp
        item.resolved_price = price
        item.counterfactual_net_pnl = pnl.net_pnl

    def _counterfactual_summary(self) -> dict[str, Any]:
        resolved = [item for item in self.counterfactuals if item.resolved_reason is not None]
        breakeven = [
            item
            for item in self.counterfactuals
            if item.protected_reason is PositionCloseReason.PROTECTED_BREAKEVEN
        ]
        return {
            "count": len(self.counterfactuals),
            "resolved": len(resolved),
            "unresolved_at_last_tick": len(self.counterfactuals) - len(resolved),
            "tp_after_breakeven": sum(item.resolved_reason == "take_profit" for item in breakeven),
            "sl_after_breakeven": sum(item.resolved_reason == "initial_stop" for item in breakeven),
            "incremental_net_if_held": round(
                sum(
                    float(item.counterfactual_net_pnl) - item.actual_net_pnl
                    for item in resolved
                    if item.counterfactual_net_pnl is not None
                ),
                4,
            ),
            "details": [asdict(item) for item in self.counterfactuals],
        }

    def _mark_to_market(self) -> dict[str, Any]:
        positions: list[dict[str, Any]] = []
        for tracked in self.position_tracker.open_positions_snapshot():
            snapshot = self.latest_snapshot_by_symbol.get(tracked.symbol)
            if snapshot is None or self.last_event_at is None:
                continue
            signal = PositionCloseSignal(
                position_id=tracked.position_id,
                symbol=tracked.symbol,
                side=tracked.side,
                reason=PositionCloseReason.UNKNOWN_CONFIRMED_CLOSE,
                detected_at=snapshot.timestamp,
                last_execution_price=snapshot.last,
                executable_estimate=snapshot.executable_exit_price(tracked.side),
                bid_at_detection=snapshot.bid,
                ask_at_detection=snapshot.ask,
                observed_spread_percent=spread_percent(snapshot),
            )
            temporary = PositionTracker()
            temporary.restore_open_position(tracked)
            marked = temporary.record_closed_position(signal)
            if marked is None:
                continue
            positions.append(
                {
                    "position_id": tracked.position_id,
                    "symbol": tracked.symbol,
                    "side": tracked.side,
                    "amount": tracked.amount,
                    "pnl_entry_price": tracked.pnl_entry_price,
                    "executable_mark_price": marked.pnl_exit_price,
                    "gross_pnl": marked.gross_pnl,
                    "explicit_costs_deducted": (marked.explicit_costs_deducted),
                    "net_pnl": marked.net_pnl,
                    "mfe_percent": marked.mfe_percent,
                    "mae_percent": marked.mae_percent,
                    "duration_seconds": max(
                        0.0,
                        (self.last_event_at - tracked.opened_at).total_seconds(),
                    ),
                    "price_source": "executable_estimate",
                }
            )
        return {
            "net_total": round(sum(item["net_pnl"] for item in positions), 4),
            "positions": positions,
        }

    def _replay_trade(
        self,
        closed: ClosedPosition,
        metadata: _OpenTradeMetadata,
    ) -> ReplayTrade:
        candidate = metadata.candidate
        return ReplayTrade(
            candidate_id=candidate.candidate_id,
            origin_candidate_id=candidate.origin_candidate_id,
            pending_entry_id=candidate.pending_entry_id,
            position_id=closed.position_id,
            symbol=closed.symbol,
            asset_class=metadata.asset_class.value,
            side=closed.side,
            session_key=candidate.session_key,
            opened_at=closed.opened_at,
            closed_at=closed.closed_at,
            close_reason=closed.close_reason.value,
            amount=closed.amount,
            signal_price=closed.signal_price,
            pnl_entry_price=closed.pnl_entry_price,
            pnl_exit_price=closed.pnl_exit_price,
            gross_pnl=closed.gross_pnl,
            explicit_costs_deducted=closed.explicit_costs_deducted,
            net_pnl=closed.net_pnl,
            gross_pnl_percent=closed.gross_pnl_percent,
            net_pnl_percent=closed.net_pnl_percent,
            duration_seconds=closed.duration_seconds,
            mfe_percent=closed.mfe_percent,
            mae_percent=closed.mae_percent,
            managed_edge_at_entry=metadata.managed_edge,
            managed_time_expected_return_contribution=(
                metadata.managed_time_expected_return_contribution
            ),
        )

    def _pnl_breakdown(self, attribute: str) -> dict[str, dict[str, float | int]]:
        keys = sorted({str(getattr(item, attribute)) for item in self.trades})
        return {
            key: {
                "count": len(items),
                "net_pnl": round(sum(item.net_pnl for item in items), 4),
            }
            for key in keys
            for items in [[item for item in self.trades if str(getattr(item, attribute)) == key]]
        }

    def _average_trade_field(self, attribute: str) -> float | None:
        values = [float(getattr(item, attribute)) for item in self.trades]
        return round(mean(values), 4) if values else None

    def _max_drawdown(self) -> float:
        cumulative = 0.0
        peak = 0.0
        maximum = 0.0
        for trade in sorted(self.trades, key=lambda item: item.closed_at):
            cumulative += trade.net_pnl
            peak = max(peak, cumulative)
            maximum = max(maximum, peak - cumulative)
        return round(maximum, 4)

    def _update_intraday_equity_drawdown(self) -> None:
        equity_pnl = sum(item.net_pnl for item in self.trades)
        for position in self.position_tracker.open_positions_snapshot():
            snapshot = self.latest_snapshot_by_symbol.get(position.symbol)
            if snapshot is None:
                continue
            marked = calculate_position_pnl(
                side=position.side,
                amount=position.amount,
                entry_price=position.pnl_entry_price,
                exit_price=snapshot.executable_exit_price(position.side),
                explicit_cost=position.estimated_explicit_cost,
                explicit_cost_percent=(
                    position.estimated_explicit_cost_percent
                ),
            )
            equity_pnl += marked.net_pnl
        self._intraday_equity_peak = max(
            self._intraday_equity_peak,
            equity_pnl,
        )
        self.max_intraday_equity_drawdown = max(
            self.max_intraday_equity_drawdown,
            self._intraday_equity_peak - equity_pnl,
        )

    def _observe_time(self, value: datetime) -> None:
        self.first_event_at = (
            value if self.first_event_at is None else min(self.first_event_at, value)
        )
        self.last_event_at = value if self.last_event_at is None else max(self.last_event_at, value)

    def _position_id(self, candidate: TradeCandidate, at: datetime) -> str:
        base = candidate.candidate_id or f"{candidate.symbol}:{candidate.signal.action}"
        return f"replay:{self.scenario_name}:{base}:{at.isoformat()}"
