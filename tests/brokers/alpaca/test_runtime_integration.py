"""Real bootstrap/factory/V3/SQLite/worker/stream wiring; HTTP and sockets are fake.

Finite scenarios drive the same runtime event/maintenance methods as run(). The
entry/exit planner, candle builder, feature state, journals and task lanes are
not replaced. Broker selection and bootstrap guards are exercised unchanged.
"""

import gzip
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import requests

from app import main
from app.brokers.alpaca import market_data
from app.brokers.alpaca import stream as stream_module
from app.runtime.storage_scope import RuntimeStorageScope
from app.v3.runtime import GoblinV3Runtime
from tests.brokers.alpaca.test_execution import TradingApi
from tests.brokers.alpaca.test_transport_and_data import response
from tests.runtime.test_storage_scope import storage_settings
from tests.v3.test_live_execution import _open_intent
from tests.v3.test_partial_close_execution import _close_intent

NOW = datetime(2026, 9, 28, 14, tzinfo=UTC)


def eventually(predicate, *, pump=lambda: None):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        pump()
        if predicate():
            return
        threading.Event().wait(0.005)
    raise AssertionError("Mocked runtime did not reach the expected state")


class Socket:
    def __init__(self, url):
        self.market = "/v2/" in url
        self.frames = queue.Queue()
        self.sent = []
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed.set()

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["action"] == "auth":
            self.frames.put({"T": "success", "msg": "authenticated"} if self.market else {
                "stream": "authorization", "data": {"status": "authorized"},
            })
        elif self.market:
            self.frames.put({"T": "subscription", "quotes": message["quotes"]})
        else:
            self.frames.put({"stream": "listening", "data": {"streams": ["trade_updates"]}})

    def recv(self, timeout):
        try:
            frame = self.frames.get(timeout=min(timeout, 0.01))
        except queue.Empty as exc:
            raise TimeoutError from exc
        if isinstance(frame, Exception):
            raise frame
        encoded = json.dumps([frame] if self.market else frame)
        return encoded if self.market else encoded.encode()


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, feed="iex"):
        self.now = NOW
        self.api = TradingApi()
        self.calls = []
        self.sockets = []
        self.runtimes = []
        self.asset_patches = {}
        self.quote_status = 200
        self.missing_quotes = set()
        self.no_quote_symbols = set()
        self.rest_bid = 100.0
        self.settings = storage_settings(
            tmp_path / "alpaca", ALPACA_DATA_FEED=feed,
            TRADING_SESSION_TIMEZONE="America/New_York", TRADING_SESSIONS_EQUITY_US="09:30-16:00",
        )
        self.monkeypatch = monkeypatch
        harness = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return harness.now if tz is not None else harness.now.replace(tzinfo=None)

        monkeypatch.setattr(main, "get_settings", lambda: self.settings)
        monkeypatch.setattr(main, "datetime", Clock)
        monkeypatch.setattr("app.v3.runtime.datetime", Clock)
        monkeypatch.setattr("app.market_data.coordinator.datetime", Clock)
        monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: self.now)
        monkeypatch.setattr(market_data, "datetime", Clock)
        monkeypatch.setattr("requests.request", self.http)
        monkeypatch.setattr(stream_module, "_connect", self.connect)

        def construct(**kwargs):
            runtime = GoblinV3Runtime(**kwargs)
            self.runtimes.append(runtime)
            return runtime

        monkeypatch.setattr(main, "GoblinV3Runtime", construct)

    def connect(self, url):
        socket = Socket(url)
        self.sockets.append(socket)
        return socket

    def http(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, kwargs.get("params")))
        if path.startswith("/v2/assets/"):
            identity = path.rsplit("/", 1)[1]
            assets = {"AAPL": str(UUID(int=1)), "SPY": str(UUID(int=2))}
            symbol = next((symbol for symbol, uid in assets.items() if identity in {symbol, uid}), None)
            if symbol is None:
                return response(404, {})
            return response(200, {
                "id": assets[symbol], "symbol": symbol, "class": "us_equity", "status": "active",
                "tradable": True, "fractionable": True, **self.asset_patches.get(symbol, {}),
            })
        if path == "/v2/stocks/quotes/latest":
            symbols = kwargs["params"]["symbols"].split(",")
            return response(self.quote_status, {"quotes": {
                symbol: self.quote(
                    symbol,
                    0 if symbol in self.no_quote_symbols else self.rest_bid,
                )
                for symbol in symbols if symbol not in self.missing_quotes
            }})
        return response(200, self.api.request(method, path, params=kwargs.get("params"), json=kwargs.get("json")))

    def quote(self, symbol="AAPL", bid=100):
        return {"T": "q", "S": symbol, "bp": bid, "ap": bid + 0.02, "t": self.now.isoformat()}

    def ready(self, runtime):
        eventually(lambda: runtime.live_market_data.connection_healthy()
                   and runtime.live_market_data._trade_stream.healthy())
        eventually(lambda: runtime._current_run_equity == 100_000, pump=runtime._drain_broker_tasks)

    def emit(self, runtime, *, seconds, bid=100, symbol="AAPL"):
        self.now = NOW + timedelta(seconds=seconds)
        socket = [s for s in self.sockets if s.market and not s.closed.is_set()][-1]
        socket.frames.put(self.quote(symbol, bid))
        event = runtime.live_market_data.next_event(1)
        assert event is not None
        runtime._refresh_sessions(self.now)
        runtime._handle_event(event, self.now)
        runtime._finalize_clocked_candles(self.now)
        runtime._flush_decision_windows(self.now)
        runtime._drain_broker_tasks()

    def run(self, scenario):
        self.monkeypatch.setattr(GoblinV3Runtime, "run", scenario)
        main.main()
        assert self.runtimes[-1]._stopped
        assert all(socket.closed.is_set() for socket in self.sockets)
        for stream in (self.runtimes[-1].live_market_data._stream,
                       self.runtimes[-1].live_market_data._trade_stream):
            assert not stream._thread.is_alive()

    def open_from_quotes(self, runtime):
        self.ready(runtime)
        for seconds in (1, 11, 21, 31, 41, 51, 61):
            self.emit(runtime, seconds=seconds)
        assert runtime.metrics["candles_closed"] >= 1
        assert runtime.metrics["decision_windows"] >= 1
        intents = runtime.intent_book.snapshot()
        assert len(intents) == 1 and intents[0].side == "BUY"
        self.emit(runtime, seconds=62, bid=intents[0].limit_price - 0.03)
        eventually(lambda: runtime.book.active_for_symbol("AAPL") is not None,
                   pump=runtime._drain_broker_tasks)
        return runtime.book.active_for_symbol("AAPL")


@pytest.mark.parametrize("feed", ["iex", "sip"])
def test_factory_runtime_prices_to_candles_to_real_planner_buy_and_restart(tmp_path, monkeypatch, feed):
    harness = Harness(tmp_path, monkeypatch, feed=feed)
    harness.run(lambda runtime: harness.open_from_quotes(runtime))
    first = harness.runtimes[-1]
    inventory = first.book.active_for_symbol("AAPL")
    assert len(harness.api.submissions) == 1
    assert harness.api.submissions[0]["symbol"] == "AAPL"
    assert inventory.total_units == pytest.approx(float(harness.api.submissions[0]["notional"]) / 100)
    assert inventory.entry_fill_count == 1
    manifest = json.loads(Path(harness.settings.run_manifest_path).read_text())
    assert manifest["broker"]["account_id"] == "paper-account"
    assert manifest["broker"]["data_feed"] == feed
    assert manifest["broker"]["universe_preflight"] == "passed"
    assert manifest["risk"]["live_authority"]["alpaca_demo_allowed"] is True
    assert manifest["risk"]["live_authority"]["alpaca_live_allowed"] is False
    assert manifest["runtime"]["account_equity"]["field"] == "equity"
    assert manifest["runtime"]["broker_execution"]["partial_close_request_field"] == "qty"
    assert "etoro_get_rate_governor" not in manifest["runtime"]["broker_execution"]
    assert "not Alpaca actual fees" in manifest["economics"]["note"]
    assert not list(Path(harness.settings.journal_path).parent.rglob("etoro_payload_schema.json"))
    requests = [params for method, path, params in harness.calls if path == "/v2/stocks/quotes/latest"]
    assert requests and all(params["feed"] == feed for params in requests)
    harness.now += timedelta(minutes=1)

    def restart(runtime):
        harness.ready(runtime)
        assert runtime.book.active_for_symbol("AAPL") == inventory
        assert runtime.executor.new_risk_allowed
        assert not runtime.intent_book.snapshot()

    harness.run(restart)
    assert len(harness.api.submissions) == 1


@pytest.mark.parametrize("patch", [
    {"class": "crypto"}, {"status": "inactive"}, {"tradable": False}, {"fractionable": False},
])
def test_ineligible_trading_asset_prevents_streams_and_orders(tmp_path, monkeypatch, patch):
    harness = Harness(tmp_path, monkeypatch)
    harness.asset_patches["AAPL"] = patch
    with pytest.raises(ValueError, match="active fractional US equity"):
        harness.run(lambda runtime: pytest.fail("Invalid universe started"))
    assert not harness.api.submissions
    assert not harness.sockets
    assert json.loads(Path(harness.settings.run_manifest_path).read_text())["status"] == "failed"


def test_unknown_etoro_benchmark_is_not_silently_replaced(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.settings = harness.settings.model_copy(update={"market_benchmark_equity_us": "SPX500"})
    with pytest.raises(requests.HTTPError):
        harness.run(lambda runtime: pytest.fail("Unknown benchmark started"))
    assert any(path == "/v2/assets/SPX500" for _, path, _ in harness.calls)
    assert not harness.api.submissions and not harness.sockets
    assert json.loads(Path(harness.settings.run_manifest_path).read_text())["status"] == "failed"


@pytest.mark.parametrize("missing", [False, True])
def test_missing_quote_or_feed_entitlement_prevents_runtime_start(tmp_path, monkeypatch, missing):
    harness = Harness(tmp_path, monkeypatch)
    if missing:
        harness.missing_quotes.add("SPY")
    else:
        harness.quote_status = 403
    with pytest.raises(KeyError if missing else requests.HTTPError):
        harness.run(lambda runtime: pytest.fail("Unusable data feed started"))
    assert not harness.api.submissions and not harness.sockets
    assert json.loads(Path(harness.settings.run_manifest_path).read_text())["status"] == "failed"


def test_closed_market_without_executable_rest_prices_can_bootstrap(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.no_quote_symbols.add("SPY")

    harness.run(lambda runtime: harness.ready(runtime))

    manifest = json.loads(Path(harness.settings.run_manifest_path).read_text())
    assert manifest["broker"]["universe_preflight"] == "passed"
    assert not harness.api.submissions


def test_benchmark_is_context_only_and_does_not_require_fractional_order_permission(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.asset_patches["SPY"] = {"fractionable": False, "tradable": False}
    harness.run(lambda runtime: harness.open_from_quotes(runtime))
    assert [order["symbol"] for order in harness.api.submissions] == ["AAPL"]


def test_startup_failure_closes_already_started_streams_and_releases_storage(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    original = GoblinV3Runtime.startup

    def fail_after_start(runtime, **kwargs):
        original(runtime, **kwargs)
        harness.ready(runtime)
        raise RuntimeError("Mock failure after starting both streams")

    monkeypatch.setattr(GoblinV3Runtime, "startup", fail_after_start)
    with pytest.raises(RuntimeError, match="after starting"):
        harness.run(lambda runtime: pytest.fail("Failed startup entered run loop"))
    assert all(socket.closed.is_set() for socket in harness.sockets)
    assert harness.runtimes[-1]._stopped
    with RuntimeStorageScope(harness.settings):
        pass
    assert not harness.api.submissions


def test_real_planner_trailing_exit_keeps_the_84_percent_allocation(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)

    def scenario(runtime):
        inventory = harness.open_from_quotes(runtime)
        initial_units = inventory.total_units
        for seconds in range(71, 362, 10):
            bid = 100.5 if seconds < 91 else 101 if seconds < 111 else 100.8
            harness.emit(runtime, seconds=seconds, bid=bid)
            if len(harness.api.submissions) == 2:
                break
        eventually(lambda: bool(runtime.executor._pending_close_confirmations),
                   pump=runtime._drain_broker_tasks)
        pending = next(iter(runtime.executor._pending_close_confirmations.values()))
        runtime._schedule_close_confirmation_checks(pending.next_attempt_monotonic + 1)
        eventually(lambda: runtime.book.active_for_symbol("AAPL").total_units < initial_units,
                   pump=runtime._drain_broker_tasks)
        assert runtime.book.active_for_symbol("AAPL").total_units == pytest.approx(initial_units * 0.16)
        assert float(harness.api.submissions[1]["qty"]) == pytest.approx(initial_units * 0.84)
        assert harness.api.submissions[1]["side"] == "sell"
        assert len(harness.api.submissions) == 2

    harness.run(scenario)


def test_quote_disconnect_rest_fallback_is_reduce_only_then_resubscribes(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)

    def scenario(runtime):
        inventory = harness.open_from_quotes(runtime)
        buy = _open_intent()
        close = _close_intent(inventory, 0.84)
        # Both intents would cross the REST quote; only the reduce-only SELL
        # may dispatch. The frozen planner is exercised separately above.
        runtime.intent_book.replace_symbol("AAPL", (buy, close))
        socket = [s for s in harness.sockets if s.market][-1]
        socket.frames.put(ConnectionError("Simulated disconnection"))
        eventually(lambda: not runtime.live_market_data.connection_healthy())
        eventually(lambda: runtime.live_market_data.diagnostics()["last_disconnect"] is not None)
        transport = runtime._heartbeat_metrics()["market_data_transport"]
        assert transport["last_disconnect"]["reason"] == "network_error"
        assert transport["trade_updates"]["healthy"] is True
        runtime.heartbeat.maybe_emit(
            journal=runtime.trade_journal, logger=logging.getLogger(__name__),
            metrics=runtime._heartbeat_metrics(), open_positions=1, active_symbols=1,
            now=runtime.heartbeat.last_emitted_at + timedelta(minutes=10),
        )
        # Block immediately, even before the per-symbol silence timeout.
        assert not runtime._operational_entry_allowed("AAPL")
        harness.now += timedelta(seconds=20)
        assert not runtime._operational_entry_allowed("AAPL")
        candles = runtime.metrics["candles_closed"]
        features = runtime.feature_engine.last_opened_at("AAPL")
        runtime._run_position_fallback_if_due(harness.now, time.monotonic() + 20)
        eventually(lambda: len(harness.api.submissions) == 2, pump=runtime._drain_broker_tasks)
        assert harness.api.submissions[-1]["side"] == "sell"
        assert runtime.metrics["candles_closed"] == candles
        assert runtime.feature_engine.last_opened_at("AAPL") == features
        assert not runtime._operational_entry_allowed("AAPL")
        eventually(lambda: runtime.live_market_data._stream.connections == 2
                   and runtime.live_market_data.connection_healthy())
        replacement = [s for s in harness.sockets if s.market][-1]
        assert replacement.sent[-1] == {"action": "subscribe", "quotes": ["AAPL", "SPY"]}
        assert not runtime._operational_entry_allowed("AAPL")  # transport alone is insufficient
        runtime.intent_book.cancel_symbol("AAPL")
        for seconds in (83, 84, 85):
            harness.emit(runtime, seconds=seconds)
        assert runtime._operational_entry_allowed("AAPL")
        assert len(harness.api.submissions) == 2

    harness.run(scenario)

    heartbeats = []
    for path in Path(harness.settings.run_manifest_path).parent.rglob("trades.jsonl.gz"):
        with gzip.open(path, "rt") as journal:
            heartbeats.extend(d for line in journal
                              if (d := json.loads(line))["event_type"] == "session_heartbeat")
    assert any(h["payload"]["market_data_transport"]["last_disconnect"]["reason"] == "network_error"
               for h in heartbeats)


def test_trade_stream_reconnect_and_duplicate_fill_do_not_rebook_inventory(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)

    def scenario(runtime):
        inventory = harness.open_from_quotes(runtime)
        order = harness.api.orders[harness.api.submissions[0]["client_order_id"]]
        socket = [s for s in harness.sockets if not s.market][-1]
        socket.frames.put(ConnectionError("Simulated trading stream disconnection"))
        eventually(lambda: runtime.live_market_data._trade_stream.connections == 2
                   and runtime.live_market_data._trade_stream.healthy())
        replacement = [s for s in harness.sockets if not s.market][-1]
        assert replacement.sent[-1] == {"action": "listen", "data": {"streams": ["trade_updates"]}}
        for _ in range(2):
            replacement.frames.put({"stream": "trade_updates", "data": {"event": "fill", "order": order}})
        eventually(lambda: replacement.frames.empty())
        runtime._drain_broker_tasks()
        assert runtime.book.active_for_symbol("AAPL") == inventory
        assert runtime.executor.verify_known_broker_legs() == ()
        assert len(harness.api.submissions) == 1

    harness.run(scenario)


@pytest.mark.parametrize("market", [True, False])
def test_real_run_loop_surfaces_fatal_stream_error_and_cleans_up(tmp_path, monkeypatch, market):
    harness = Harness(tmp_path, monkeypatch)
    original = GoblinV3Runtime.startup

    def startup(runtime, **kwargs):
        original(runtime, **kwargs)
        harness.ready(runtime)
        socket = next(s for s in harness.sockets if s.market == market)
        socket.frames.put(PermissionError("Simulated denied subscription"))
        stream = (runtime.live_market_data._stream if market
                  else runtime.live_market_data._trade_stream)
        eventually(lambda: stream.diagnostics()["fatal"])

    monkeypatch.setattr(GoblinV3Runtime, "startup", startup)
    # Unlike the finite event scenarios, run() is deliberately NOT replaced.
    with pytest.raises(RuntimeError, match="Alpaca stream failed"):
        main.main()
    assert harness.runtimes[-1].loop_id >= 1
    assert harness.runtimes[-1]._stopped
    assert all(s.closed.is_set() for s in harness.sockets)
    assert not harness.api.submissions
    with RuntimeStorageScope(harness.settings):
        pass


def test_shutdown_holds_storage_until_workers_finish_without_dispatching_late_fallback(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)

    def scenario(runtime):
        inventory = harness.open_from_quotes(runtime)
        runtime.intent_book.replace_symbol("AAPL", (_close_intent(inventory, 0.84),))
        started, release = threading.Event(), threading.Event()

        def fallback():
            started.set()
            assert release.wait(3), "Test did not release the in-flight REST request"
            return runtime.rest_market_data.get_market_snapshots(["AAPL"])

        runtime.maintenance_runner.submit(
            kind="v3_position_fallback", operation=fallback, context={"symbols": ["AAPL"]},
        )
        assert started.wait(1)

        def verify_lease_then_release():
            try:
                eventually(lambda: all(s.closed.is_set() for s in harness.sockets))
                assert not runtime._stopped
                with pytest.raises(RuntimeError, match="already in use"):
                    with RuntimeStorageScope(harness.settings):
                        pass
            finally:
                release.set()

        with ThreadPoolExecutor(max_workers=1) as controller:
            check = controller.submit(verify_lease_then_release)
            runtime.stop()
            check.result(timeout=3)
        assert runtime.maintenance_runner.pending_count() == 0
        assert runtime.maintenance_runner.drain() == []
        assert len(harness.api.submissions) == 1  # no SELL from the late REST quote
        assert runtime.book.active_for_symbol("AAPL") == inventory

    harness.run(scenario)
    with RuntimeStorageScope(harness.settings):
        pass
