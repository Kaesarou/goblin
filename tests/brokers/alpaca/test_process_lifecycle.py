"""Real OS signals/process death; only broker transports and time are simulated.

The parent owns the mock broker's orders, so killing Goblin cannot erase broker
evidence. Children use main(), the real continuous run loop, threads and SQLite.
Broker selection and execution-mode guards are not bypassed.
"""

import json
import multiprocessing
import os
import queue
import signal
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

import pytest

from app import main
from app.runtime.storage_scope import RuntimeStorageScope
from app.v3.book import InventoryBook
from app.v3.persistence import InventoryEventStore
from app.v3.runtime import GoblinV3Runtime
from app.v3.state_store import V3RuntimeStateStore
from tests.brokers.alpaca.test_execution import TradingApi
from tests.brokers.alpaca.test_runtime_integration import NOW, Harness, eventually
from tests.runtime.test_storage_scope import storage_settings


class RemoteApi:
    def __init__(self, connection):
        self.connection = connection
        self.lock = threading.Lock()

    def request(self, method, path, **kwargs):
        with self.lock:
            self.connection.send((method, path, kwargs))
            if not self.connection.poll(15):
                raise TimeoutError("Mock broker controller did not answer")
            ok, result = self.connection.recv()
        if not ok:
            raise AssertionError(result)
        return result


def _child(root, connection, messages, scenario, offset):
    """Spawn target must be importable; no inherited threads or signal handlers."""
    with pytest.MonkeyPatch.context() as patch:
        harness = Harness(Path(root), patch)
        harness.api = RemoteApi(connection)
        harness.now = NOW + timedelta(seconds=offset)
        original_run = GoblinV3Runtime.run
        original_startup = GoblinV3Runtime.startup
        if scenario == "recover":
            original_delete_retry = V3RuntimeStateStore.delete_close_retry

            def slow_retry_cleanup(store, action_id):
                # Pending-close removal precedes durable retry cleanup and halt
                # retirement. Make the driver's cross-thread observation window
                # deterministic instead of depending on CI SQLite/CPU timing.
                threading.Event().wait(0.05)
                original_delete_retry(store, action_id)

            patch.setattr(V3RuntimeStateStore, "delete_close_retry", slow_retry_cleanup)

        def send_quote(runtime, seconds, bid=100):
            before = runtime.coordinator.metrics["accepted_events"]
            loop = runtime.loop_id
            harness.now = NOW + timedelta(seconds=seconds)
            socket = next(s for s in harness.sockets if s.market and not s.closed.is_set())
            socket.frames.put(harness.quote(bid=bid))
            eventually(lambda: runtime._stop_requested or (
                runtime.coordinator.metrics["accepted_events"] > before
                and runtime.loop_id > loop + 1))

        def drive(runtime, done):
            try:
                eventually(lambda: runtime.live_market_data.connection_healthy()
                           and runtime.live_market_data._trade_stream.healthy()
                           and runtime._current_run_equity == 100_000)
                messages.put(("ready", os.getpid()))
                if scenario in {"buy", "exit", "normal"}:
                    for seconds in (1, 11, 21, 31, 41, 51, 61):
                        send_quote(runtime, seconds)
                    eventually(lambda: bool(runtime.intent_book.snapshot()))
                    intent = runtime.intent_book.snapshot()[0]
                    assert intent.side == "BUY"
                    send_quote(runtime, 62, bid=intent.limit_price - 0.03)
                    if scenario in {"normal", "exit"}:
                        eventually(lambda: runtime.book.active_for_symbol("AAPL") is not None)
                        inventory = runtime.book.active_for_symbol("AAPL")
                        messages.put(("filled", inventory.total_units))
                    if scenario == "normal":
                        runtime.request_stop()
                    elif scenario == "exit":
                        for seconds in range(71, 362, 10):
                            bid = 100.5 if seconds < 91 else 101 if seconds < 111 else 100.8
                            send_quote(runtime, seconds, bid)
                            if runtime.metrics["orders_submitted"] >= 2:
                                break
                        assert runtime.metrics["orders_submitted"] == 2
                elif scenario == "recover":
                    eventually(lambda: runtime.book.active_for_symbol("AAPL") is not None
                               and not runtime.executor._pending_open_confirmations
                               and not runtime.executor._pending_close_confirmations
                               and runtime.executor.new_risk_allowed)
                    inventory = runtime.book.active_for_symbol("AAPL")
                    messages.put(("recovered", {
                        "units": inventory.total_units, "entries": inventory.entry_fill_count,
                        "new_risk_allowed": runtime.executor.new_risk_allowed,
                    }))
                # Observe the signal request while main waits for in-flight work.
                while not done.wait(0.005):
                    if runtime._stop_requested:
                        eventually(lambda: all(s.closed.is_set() for s in harness.sockets))
                        messages.put(("stopping", runtime.stop_reason))
                        return
            except BaseException:
                messages.put(("driver_error", traceback.format_exc()))
                runtime.request_stop()

        def run(runtime):
            if runtime._stop_requested:
                return original_run(runtime, timeout_seconds=0.005)
            done = threading.Event()
            driver = threading.Thread(target=drive, args=(runtime, done), daemon=True)
            driver.start()
            try:
                return original_run(runtime, timeout_seconds=0.005)
            finally:
                done.set()
                driver.join(timeout=4)
                assert not driver.is_alive()

        def startup(runtime, **kwargs):
            original_startup(runtime, **kwargs)
            if scenario == "startup":
                messages.put(("startup", os.getpid()))
                eventually(lambda: runtime._stop_requested)

        patch.setattr(GoblinV3Runtime, "run", run)
        patch.setattr(GoblinV3Runtime, "startup", startup)
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        main.main()
        assert all(signal.getsignal(s) == handler for s, handler in previous.items())
        assert all(s.closed.is_set() for s in harness.sockets)
        runtime = harness.runtimes[-1]
        assert runtime._stopped
        assert runtime.mutation_runner.pending_count() == 0
        assert runtime.maintenance_runner.pending_count() == 0
        messages.put(("stopped", runtime.stop_reason))


class ProcessRun:
    def __init__(self, root, api, scenario, *, block_side=None, offset=0):
        self.api = api
        self.block_side = block_side
        self.post_seen = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.errors = []
        self.calls = []
        ctx = multiprocessing.get_context("spawn")
        self.messages = ctx.Queue()
        self.parent, child = ctx.Pipe()
        self.process = ctx.Process(target=_child, args=(str(root), child, self.messages, scenario, offset))
        self.server = threading.Thread(target=self.serve, daemon=True)
        self.process.start()
        child.close()
        self.server.start()

    def serve(self):
        try:
            while not self.closed.is_set():
                if not self.parent.poll(0.05):
                    continue
                method, path, kwargs = self.parent.recv()
                self.calls.append((method, path))
                value = self.api.request(method, path, **kwargs)
                if method == "POST" and kwargs["json"]["side"] == self.block_side:
                    self.post_seen.set()  # broker filled; response not delivered yet
                    if not self.release.wait(15):
                        raise TimeoutError("Test did not release broker response")
                self.parent.send((True, value))
        except (EOFError, BrokenPipeError, ConnectionResetError):
            pass  # Expected when the child is killed while awaiting HTTP.
        except BaseException:
            self.errors.append(traceback.format_exc())

    def expect(self, event, *, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                kind, value = self.messages.get(timeout=0.1)
            except queue.Empty:
                if not self.process.is_alive():
                    pytest.fail(f"Child exited {self.process.exitcode} before {event}; {self.errors}")
                continue
            if kind == "driver_error":
                pytest.fail(value)
            if kind == event:
                return value
        pytest.fail(f"Child did not report {event}; {self.errors}")

    def signal(self, signum):
        os.kill(self.process.pid, signum)

    def join(self, expected=0):
        self.process.join(timeout=8)
        assert not self.process.is_alive()
        assert self.process.exitcode == expected
        assert not self.errors

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release.set()
        if self.process.is_alive():
            self.process.kill()
        self.process.join(timeout=5)
        self.closed.set()
        self.server.join(timeout=2)
        self.parent.close()
        self.messages.close()


def stored_inventory(root):
    settings = storage_settings(root / "alpaca")
    with RuntimeStorageScope(settings):
        book = InventoryBook.from_events(InventoryEventStore(settings.position_store_path).events())
        manifest = json.loads(Path(settings.run_manifest_path).read_text())
    return book.active_for_symbol("AAPL"), manifest


def test_continuous_loop_requested_stop_checkpoints_one_planner_buy(tmp_path):
    api = TradingApi()
    with ProcessRun(tmp_path, api, "normal") as run:
        units = run.expect("filled")
        assert run.expect("stopped") == "requested"
        run.join()
    inventory, manifest = stored_inventory(tmp_path)
    assert inventory.total_units == units
    assert inventory.entry_fill_count == 1
    assert manifest["status"] == "completed"
    assert len(api.submissions) == 1


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_process_signal_during_order_drains_fill_before_unlocking(tmp_path, signum):
    api = TradingApi()
    with ProcessRun(tmp_path, api, "buy", block_side="buy") as run:
        run.expect("ready")
        assert run.post_seen.wait(8)
        run.signal(signum)
        assert run.expect("stopping") == "interrupted"
        with pytest.raises(RuntimeError, match="already in use"):
            with RuntimeStorageScope(storage_settings(tmp_path / "alpaca")):
                pass
        run.signal(signum)  # Repeated Docker stop/interrupt must not abort cleanup.
        assert run.process.is_alive()
        run.release.set()
        assert run.expect("stopped") == "interrupted"
        run.join()
    inventory, manifest = stored_inventory(tmp_path)
    assert inventory.entry_fill_count == 1
    assert manifest["status"] == "interrupted"
    assert len(api.submissions) == 1


def test_sigterm_after_stream_start_preserves_stop_request_before_run(tmp_path):
    api = TradingApi()
    with ProcessRun(tmp_path, api, "startup") as run:
        run.expect("startup")
        run.signal(signal.SIGTERM)
        assert run.expect("stopped") == "interrupted"
        run.join()
    inventory, manifest = stored_inventory(tmp_path)
    assert inventory is None
    assert manifest["status"] == "interrupted"
    assert not api.submissions


@pytest.mark.parametrize("side,scenario", [("buy", "buy"), ("sell", "exit")])
def test_sigkill_after_broker_fill_recovers_exactly_once_without_reposting(tmp_path, side, scenario):
    api = TradingApi()
    with ProcessRun(tmp_path, api, scenario, block_side=side) as run:
        run.expect("ready")
        assert run.post_seen.wait(8)
        run.signal(signal.SIGKILL)
        run.join(expected=-signal.SIGKILL)
    count = len(api.submissions)
    expected = float(api.request("GET", "/v2/positions")[0]["qty"])
    with ProcessRun(tmp_path, api, "recover", offset=600) as restart:
        result = restart.expect("recovered")
        assert result == {"units": pytest.approx(expected), "entries": 1, "new_risk_allowed": True}
        restart.signal(signal.SIGTERM)
        restart.expect("stopped")
        restart.join()
        assert all(method == "GET" for method, _ in restart.calls)
    assert len(api.submissions) == count == (1 if side == "buy" else 2)
    inventory, manifest = stored_inventory(tmp_path)
    assert inventory.total_units == pytest.approx(expected)
    assert inventory.entry_fill_count == 1
    assert manifest["status"] == "interrupted"
