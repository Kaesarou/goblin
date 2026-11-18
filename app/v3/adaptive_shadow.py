"""Paired US-equity quote-window simulation. Never submits a broker order.

Both arms begin flat with the same simulated USD equity. Pending intents may
fill only at the next consecutive observed M1 decision quote, not on the
quote that generated the intent or on an unobserved intraminute excursion.
"""
from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import replace
from datetime import timedelta

from app.v3.adaptive_policy import (
    AdaptiveConfig, CapitalAllocationService, InventoryLifecycleService,
    Lifecycle, MarketRegimeService, Regime, SECTORS,
)
from app.v3.book import InventoryBook
from app.v3.forager import ForagerCandidate, NoVolumeForager
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent, PortfolioState

STARTING_EQUITY = 100_000.0
MIN_ENTRY_NOTIONAL = 10.0
MIN_REMAINDER_NOTIONAL = 10.0
WINDOW = timedelta(minutes=1)
MAX_BENCHMARK_AGE = timedelta(seconds=120)
BENCHMARK_MOMENTUM = timedelta(minutes=15)


def shadow_metadata(broker):
    return {
        "enabled": broker in ("etoro_demo", "alpaca_demo"),
        "authority": "none",
        "universe": "US_equities_only_USD_quote_proxy",
        "starting_equity": STARTING_EQUITY,
        "starting_state": "flat_independent_per_runtime_run",
        "fill_model": "next_consecutive_M1_quote_touch_bid_ask_no_intraminute_fill",
        "cost_model": "broker_research_schedule_per_request_not_confirmed_fees",
        "age_model": "observed_US_session_transitions_since_initial_fill",
        "policy": AdaptiveConfig().metadata(),
    }


def _valid(market):
    return (market.quality_ok and math.isfinite(market.bid) and math.isfinite(market.ask)
            and 0 < market.bid <= market.ask)


class _ShadowPortfolio:
    def __init__(self, *, name, planner, config, cost_schedule, emit, policy=None):
        self.name = name
        self.planner = planner
        self.config = config
        self.cost_schedule = cost_schedule
        self.emit = emit
        self.policy = policy
        self.lifecycle = InventoryLifecycleService(policy, config.risk.max_entry_fills) if policy else None
        self.allocation = CapitalAllocationService(policy) if policy else None
        self.book = InventoryBook()
        self.pending = ()
        self.marks = {}
        self.mark_times = {}
        self.opened_session = {}
        self.last_exit_session = {}
        self.partial_done = set()
        self.closed_by_risk = {}
        self.lifecycle_by_id = {}
        self.balance = STARTING_EQUITY  # realized P/L less fees, not available cash
        self.gross = self.fees = 0.
        self.peak = STARTING_EQUITY
        self.drawdown = 0.
        self.capital_minutes = 0.
        self.counts = Counter()
        self.last_at = None
        self.last_session = None
        self.session_index = 0

    def equity(self):
        return self.balance + sum(
            inv.total_units * (self.marks.get(inv.symbol, inv.average_entry_price)
                               - inv.average_entry_price)
            for inv in self.book.portfolio(equity=1.).inventories)

    def portfolio(self):
        return self.book.portfolio(equity=self.equity())

    def _fee(self, notional):
        return self.cost_schedule.fixed_fee_per_fill + (
            notional * self.cost_schedule.commission_pct_per_fill)

    def _can_buy(self, market):
        # Do not authorize new simulated exposure on stale held marks.
        return market.entry_allowed and all(
            self.mark_times.get(inv.symbol) == market.asof
            for inv in self.portfolio().inventories)

    def _buy(self, intent, market):
        portfolio = self.portfolio()
        inv = portfolio.inventory_for(intent.symbol)
        if portfolio.equity <= 0 or not self._can_buy(market):
            return False
        risk = self.config.risk
        if inv is None:
            if intent.purpose != IntentPurpose.INITIAL_ENTRY or portfolio.active_inventory_count >= risk.max_inventories:
                return False
        elif (intent.purpose != IntentPurpose.REENTRY or intent.inventory_id != inv.inventory_id
              or inv.entry_fill_count >= risk.max_entry_fills):
            return False

        # Recheck caps at fill including the immediate bid/ask mark loss and
        # the assumed commission. No pending plan can reserve nonexistent slots.
        price = market.ask
        requested = float(intent.metadata.get("requested_units", intent.notional / price)) * price
        fixed = self.cost_schedule.fixed_fee_per_fill
        loss_rate = (1 - market.bid / price) + self.cost_schedule.commission_pct_per_fill
        principal = sum(i.total_notional for i in portfolio.inventories)
        old = inv.total_notional if inv else 0.
        room = min(
            (risk.max_portfolio_exposure_pct * (portfolio.equity - fixed) - principal)
            / (1 + risk.max_portfolio_exposure_pct * loss_rate),
            (risk.max_symbol_exposure_pct * (portfolio.equity - fixed) - old)
            / (1 + risk.max_symbol_exposure_pct * loss_rate),
        )
        amount = min(requested, room)
        if not math.isfinite(amount) or amount < MIN_ENTRY_NOTIONAL:
            return False
        fee = self._fee(amount)
        iid = inv.inventory_id if inv else f"{self.name}:{market.symbol}:{market.asof.isoformat()}"
        self.book.apply_entry_fill(
            inventory_id=iid, symbol=market.symbol, position_id=intent.intent_id,
            units=amount / price, price=price, fee=fee, filled_at=market.asof)
        if inv is None:
            self.opened_session[iid] = self.session_index
        self.fees += fee
        self.balance -= fee
        self.counts["buy_fills"] += 1
        self._record_fill(intent, market, amount / price, price, fee, 0., iid)
        return True

    def _sell(self, intent, market):
        inv = self.book.active_for_symbol(intent.symbol)
        if inv is None or inv.inventory_id != intent.inventory_id or not intent.reduce_only:
            return False
        fraction = float(intent.metadata.get("close_fraction_of_units", 0.))
        if not 0 < fraction <= 1:
            return False
        # Match pro-rata geometry and the profit-exit dust proxy. A partial
        # risk reduction also collapses untradeable residuals rather than
        # inventing an independently investable fraction.
        price = market.bid
        if any(0 < leg.units * (1 - fraction) * price < MIN_REMAINDER_NOTIONAL
               for leg in inv.broker_legs):
            fraction = 1.
        fee = gross = units = 0.
        for leg in inv.broker_legs:
            quantity = leg.units * fraction
            leg_fee = self._fee(quantity * price)
            self.book.apply_exit_fill(position_id=leg.position_id, exit_price=price,
                                      fee=leg_fee, units=quantity, filled_at=market.asof)
            gross += quantity * (price - leg.entry_price)
            fee += leg_fee
            units += quantity
        self.balance += gross - fee
        self.gross += gross
        self.fees += fee
        self.counts["sell_fills"] += 1
        self.last_exit_session[inv.inventory_id] = self.session_index
        reason = intent.metadata.get("adaptive_reason")
        if reason:
            self.counts[str(reason)] += 1
            if reason == "stress_reduction":
                self.partial_done.add(inv.inventory_id)
        if self.book.active_for_symbol(intent.symbol) is None:
            self.counts["closed_inventories"] += 1
            if reason:
                self.closed_by_risk[intent.symbol] = self.session_index
        self._record_fill(intent, market, units, price, fee, gross, inv.inventory_id)
        return True

    def _record_fill(self, intent, market, units, price, fee, gross, iid):
        self.emit("adaptive_shadow_fill", {
            "arm": self.name, "asof": market.asof, "planned_at": intent.created_at,
            "intent_id": intent.intent_id, "symbol": intent.symbol, "inventory_id": iid,
            "side": intent.side, "purpose": intent.purpose.value,
            "adaptive_reason": intent.metadata.get("adaptive_reason"),
            "units": units, "price": price, "fee_assumption": fee,
            "gross_realized_pnl": gross, "equity": self.equity(),
        })

    def _age(self, inv):
        return self.session_index - self.opened_session[inv.inventory_id]

    def _initial_allowed(self, symbol, regime, inventories):
        if not self.policy:
            return True
        return (self.closed_by_risk.get(symbol) != self.session_index
                and self.allocation.entry_allowed(symbol=symbol, regime=regime,
                                                 inventories=inventories))

    def step(self, *, asof, session, markets, features, regime):
        if self.last_at is not None and asof <= self.last_at:
            return
        consecutive = self.last_at is not None and asof - self.last_at == WINDOW
        same_session = self.last_session == session
        if not same_session:
            self.session_index += 1
        self.last_at, self.last_session = asof, session
        markets = {s: m for s, m in markets.items() if _valid(m)}
        for symbol, market in markets.items():
            self.marks[symbol] = market.bid
            self.mark_times[symbol] = asof

        # Prior intents only; close before buy, then re-check limits on every
        # fill. A stale or missing quote expires the intent rather than bridging a gap.
        filled = set()
        previous, self.pending = self.pending, ()
        if consecutive and same_session:
            for intent in sorted(previous, key=lambda i: (i.side != "SELL", i.symbol)):
                market = markets.get(intent.symbol)
                if market is None or intent.symbol in filled:
                    continue
                if intent.side == "SELL":
                    if (intent.execution_style == ExecutionStyle.MARKET
                            or (intent.limit_price is not None and market.bid >= intent.limit_price)):
                        if self._sell(intent, market):
                            filled.add(intent.symbol)
                elif intent.limit_price is not None and market.ask <= intent.limit_price:
                    inv = self.book.active_for_symbol(intent.symbol)
                    if self.policy:
                        current = self.portfolio()
                        if inv is None:
                            if not self._initial_allowed(intent.symbol, regime, current.inventories):
                                self.counts["fill_gate_rejections"] += 1
                                continue
                        elif not self.allocation.reentry_allowed(
                            regime=regime, lifecycle=self.lifecycle.evaluate(
                                inventory=inv, age_sessions=self._age(inv))):
                            self.counts["fill_gate_rejections"] += 1
                            continue
                    if self._buy(intent, market):
                        filled.add(intent.symbol)
        for symbol, market in markets.items():
            if symbol not in filled:
                # Quote-only trajectory, identical in both arms. Never use a
                # full candle whose extrema might predate the simulated fill.
                self.book.observe_candle(symbol=symbol, high=market.midpoint,
                                         low=market.midpoint, close=market.midpoint)

        portfolio = self.portfolio()
        self.peak = max(self.peak, portfolio.equity)
        self.drawdown = max(self.drawdown, 1 - portfolio.equity / self.peak)
        self.counts["windows"] += 1
        if portfolio.active_inventory_count >= self.config.risk.max_inventories:
            self.counts["saturated_windows"] += 1
        self.capital_minutes += sum(i.total_notional for i in portfolio.inventories)
        if not all(self.mark_times.get(i.symbol) == asof for i in portfolio.inventories):
            self.counts["stale_mark_windows"] += 1

        candidates = [
            ForagerCandidate(m, features[s].forager_volatility, features[s].ema_readiness)
            for s, m in markets.items()
            if portfolio.inventory_for(s) is None and self._can_buy(m)
        ]
        ranked = NoVolumeForager().rank(candidates, limit=len(candidates))
        full = portfolio.active_inventory_count >= self.config.risk.max_inventories
        plans = []
        rotation_planned = False
        # Older inventories first, then deterministic symbol order.
        for inv in sorted(portfolio.inventories, key=lambda i: (-self._age(i), i.symbol)):
            market = markets.get(inv.symbol)
            if market is None:
                continue
            age = self._age(inv)
            state = self.lifecycle.evaluate(inventory=inv, age_sessions=age) if self.policy else None
            if state is not None and self.lifecycle_by_id.get(inv.inventory_id) != state:
                self.lifecycle_by_id[inv.inventory_id] = state
                self.emit("adaptive_shadow_lifecycle", {
                    "arm": self.name, "asof": asof, "symbol": inv.symbol,
                    "inventory_id": inv.inventory_id, "state": state.value,
                    "age_session_transitions": age,
                    "unrealized_return": market.bid / inv.average_entry_price - 1,
                })
            can_reenter = self._can_buy(market)
            if self.policy:
                allowed = self.allocation.reentry_allowed(regime=regime, lifecycle=state)
                if can_reenter and not allowed:
                    self.counts["reentry_gated_windows"] += 1
                can_reenter = can_reenter and allowed
            normal = self.planner.plan_existing_inventory(
                market=market, portfolio=portfolio, allow_new_risk=can_reenter)
            exits = [i for i in normal.intents if i.side == "SELL"]
            if exits and not (self.policy and self.policy.use_time_exit
                              and state == Lifecycle.EXIT_DUE):
                # Preserve normal profit exit precedence, except at the hard
                # research age limit. No opposing BUY alongside a planned sell.
                plans.extend(exits)
                continue
            reason = None
            if self.policy:
                remaining = [i for i in portfolio.inventories if i.inventory_id != inv.inventory_id]
                alternative = None
                if (state == Lifecycle.STALE and full and not rotation_planned
                        and self.policy.use_rotation and regime in (Regime.NORMAL, Regime.RECOVERY)):
                    exit_fee = sum(self._fee(leg.units * market.bid) for leg in inv.broker_legs)
                    post_equity = portfolio.equity - exit_fee
                    if post_equity > 0:
                        hypothetical = PortfolioState(post_equity, tuple(
                            replace(i, wallet_exposure_pct=i.total_notional / post_equity)
                            for i in remaining))
                        for candidate in ranked:
                            if (candidate.ema_readiness > 0 or not self._initial_allowed(
                                    candidate.market.symbol, regime, remaining)):
                                continue
                            preview = self.planner.plan_symbol(
                                market=candidate.market, portfolio=hypothetical)
                            if any(i.side == "BUY" and i.cost_estimate is not None
                                   and (i.expected_gross_capture or 0.) > i.cost_estimate.total + exit_fee
                                   for i in preview.intents):
                                alternative = candidate
                                break
                reason = self.allocation.risk_exit_reason(
                    lifecycle=state, regime=regime,
                    unrealized_return=market.bid / inv.average_entry_price - 1,
                    rotation_candidate=alternative is not None and not rotation_planned,
                    full=full, partial_done=inv.inventory_id in self.partial_done)
            if reason:
                fraction = .5 if reason == "stress_reduction" else 1.
                plans.append(OrderIntent(
                    f"{self.name}:{inv.inventory_id}:{reason}:{asof.isoformat()}",
                    IntentPurpose.RISK_REDUCTION, inv.symbol, "SELL",
                    inv.total_units * market.bid * fraction, asof,
                    ExecutionStyle.MARKET, inventory_id=inv.inventory_id, reduce_only=True,
                    metadata={"close_fraction_of_units": fraction, "adaptive_reason": reason},
                ))
                if reason == "rotation":
                    rotation_planned = True
                    self.emit("adaptive_shadow_rotation", {
                        "arm": self.name, "asof": asof, "from_symbol": inv.symbol,
                        "candidate_symbol": alternative.market.symbol,
                        "candidate_score": alternative.score,
                        "age_session_transitions": age,
                        "unrealized_return": market.bid / inv.average_entry_price - 1,
                    })
            else:
                plans.extend(i for i in normal.intents if i.side == "BUY")

        slots = max(0, self.config.risk.max_inventories - portfolio.active_inventory_count)
        selected = list(portfolio.inventories)
        for rank in ranked:
            if slots <= 0:
                break
            symbol = rank.market.symbol
            if not self._initial_allowed(symbol, regime, selected):
                self.counts["entry_gated_candidates"] += 1
                continue
            decision = self.planner.plan_symbol(market=rank.market, portfolio=portfolio)
            entries = [i for i in decision.intents if i.side == "BUY"]
            if entries:
                plans.extend(entries)
                # Preserve sector reservations when selecting several flat
                # candidates; financial caps are independently checked at fill.
                selected.append(rank.market)
                slots -= 1
        self.pending = tuple(plans)

    def metrics(self):
        p = self.portfolio()
        return {
            "arm": self.name, "equity": p.equity, "realized_net": self.balance - STARTING_EQUITY,
            "gross_realized": self.gross, "assumed_fees": self.fees,
            "unrealized_bid_pnl": p.equity - self.balance,
            "max_drawdown": self.drawdown, "capital_observed_window_minutes": self.capital_minutes,
            "counts": dict(self.counts), "pending_intents": len(self.pending),
            "principal": sum(i.total_notional for i in p.inventories),
            "inventories": [{
                "symbol": i.symbol, "inventory_id": i.inventory_id,
                "entries": i.entry_fill_count, "principal": i.total_notional,
                "age_session_transitions": self._age(i),
                "sessions_since_last_reduction": (
                    self.session_index - self.last_exit_session[i.inventory_id]
                    if i.inventory_id in self.last_exit_session else None),
                "unrealized_return": self.marks.get(i.symbol, i.average_entry_price) / i.average_entry_price - 1,
                "bid_observed_at": self.mark_times.get(i.symbol),
                "sector": SECTORS.get(i.symbol, "UNKNOWN"),
            } for i in p.inventories],
        }


class ShadowExperiment:
    """Research feed consumer with its own books; receives no execution adapter."""
    def __init__(self, *, planner, config, cost_schedule, journal, benchmark_symbols,
                 policy=None):
        self.policy = policy or AdaptiveConfig()
        self.journal = journal
        self.benchmark_symbols = tuple(benchmark_symbols)
        self.benchmarks = {s: deque(maxlen=2048) for s in self.benchmark_symbols}
        self.regime = MarketRegimeService(self.policy)
        self.arms = (
            _ShadowPortfolio(name="RR5_SHADOW_BASELINE", planner=planner, config=config,
                             cost_schedule=cost_schedule, emit=self._emit),
            _ShadowPortfolio(name=self.policy.name, planner=planner, config=config,
                             cost_schedule=cost_schedule, emit=self._emit, policy=self.policy),
        )
        self.last_at = self.session = self.last_regime = self.last_report_at = None
        self.benchmark_reference = {}
        self.symbol_reference = {}
        self.disabled_reason = None
        self.current_context = None

    def _emit(self, event, payload):
        self.journal.write(event, payload)

    def observe_benchmark(self, snapshot):
        history = self.benchmarks.get(snapshot.symbol)
        if history is not None and math.isfinite(snapshot.bid) and math.isfinite(snapshot.ask) and 0 < snapshot.bid <= snapshot.ask:
            history.append(snapshot)

    def _benchmark_context(self, asof, session_start):
        for symbol in self.benchmark_symbols:
            history = self.benchmarks[symbol]
            eligible = [q for q in history if session_start <= q.timestamp < asof
                        and q.received_at <= asof]
            if not eligible:
                continue
            last = eligible[-1]
            if asof - last.timestamp > MAX_BENCHMARK_AGE:
                continue
            price = (last.bid + last.ask) / 2
            initial = eligible[0]
            opened = self.benchmark_reference.setdefault(symbol, (initial.bid + initial.ask) / 2)
            target = asof - BENCHMARK_MOMENTUM
            previous = next((q for q in reversed(eligible) if q.timestamp <= target), None)
            momentum = None
            if previous and target - previous.timestamp <= MAX_BENCHMARK_AGE:
                momentum = price / ((previous.bid + previous.ask) / 2) - 1
            return price / opened - 1, momentum, symbol
        return None, None, None

    def step(self, *, asof, session, session_start, expected, markets, features):
        if self.disabled_reason is not None or (self.last_at is not None and asof <= self.last_at):
            return
        if session != self.session:
            self.session = session
            self.symbol_reference.clear()
            self.benchmark_reference.clear()
            self.regime.reset()
        elif self.last_at is not None and asof - self.last_at != WINDOW:
            self.regime.reset()
        self.last_at = asof
        markets = {s: m for s, m in markets.items() if s in expected and _valid(m)}
        returns = [m.midpoint / self.symbol_reference.setdefault(s, m.midpoint) - 1
                   for s, m in markets.items()]
        benchmark, momentum, symbol = self._benchmark_context(asof, session_start)
        context = self.regime.evaluate(benchmark_return=benchmark, benchmark_momentum=momentum,
                                       returns=returns, expected=len(expected))
        self.current_context = context
        if context.regime != self.last_regime:
            self.last_regime = context.regime
            self._emit("adaptive_shadow_regime", {
                "asof": asof, "session": session, "benchmark_symbol": symbol,
                "context": context,
            })
        for arm in self.arms:
            arm.step(asof=asof, session=session, markets=markets, features=features,
                     regime=context.regime)
        if self.last_report_at is None or asof - self.last_report_at >= timedelta(minutes=5):
            self.last_report_at = asof
            self._emit("adaptive_shadow_checkpoint", {"asof": asof, **self.metrics()})

    def metrics(self):
        return {
            "status": "disabled" if self.disabled_reason else "active",
            "disabled_reason": self.disabled_reason,
            "last_window": self.last_at, "context": self.current_context,
            "arms": [a.metrics() for a in self.arms],
            "journal_suppressed": self.journal.suppressed_count,
            "journal_failed": self.journal.failed_count,
        }
