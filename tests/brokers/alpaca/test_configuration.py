import json
from pathlib import Path
from uuid import UUID

import pytest
import requests

from app.brokers.alpaca.client import AlpacaBrokerClient
from app.brokers.alpaca.environment import AlpacaEnvironment
from app.brokers.alpaca.http_client import AlpacaHttpClient
from app.brokers.alpaca.instrument_cache import AlpacaInstrumentCache
from app.brokers.alpaca.market_data import AlpacaMarketDataFeed, AlpacaRestMarketDataClient
from app.brokers.alpaca.order_store import AlpacaOrderStore
from app.brokers.etoro.market_data_client import EtoroRestMarketDataClient
from app.config.settings import Settings
from app.main import _assert_v3_execution_mode
from app.runtime.factories import build_runtime_clients
from app.v3.manifest import _sanitized_settings_snapshot

ASSET_ID = str(UUID(int=1))


def settings(tmp_path, mode="alpaca_demo", **overrides):
    return Settings(
        _env_file=None,
        **{
            "BROKER": mode,
            "ALPACA_API_KEY": "alpaca-key",
            "ALPACA_SECRET_KEY": "alpaca-secret",
            "ALPACA_INSTRUMENT_ID_CACHE_PATH": str(tmp_path / "alpaca.json"),
            "ETORO_API_KEY": "etoro-key",
            "ETORO_USER_KEY": "etoro-user",
            "ETORO_INSTRUMENT_ID_CACHE_PATH": str(tmp_path / "etoro.json"),
            "POSITION_STORE_PATH": str(tmp_path / "state.sqlite"),
            **overrides,
        },
    )


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("alpacademo", "alpaca_demo"),
        ("alpacalive", "alpaca_live"),
        ("alpaca_demo", "alpaca_demo"),
        ("alpaca_live", "alpaca_live"),
        (" eToro-live ", "etoro_live"),
        ("etorodemo", "etoro_demo"),
        ("paper", "paper"),
    ],
)
def test_broker_names_are_canonicalized(tmp_path, raw, canonical):
    assert settings(tmp_path, raw).broker == canonical


@pytest.mark.parametrize("mode,host", [("alpaca_demo", "paper-api"), ("alpaca_live", "api")])
def test_broker_selects_only_alpaca_keys_endpoints_cache_and_streams(
    tmp_path, monkeypatch, mode, host
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Factory must neither connect nor construct eToro clients")

    monkeypatch.setattr("app.runtime.factories.EtoroRestMarketDataClient", forbidden)
    monkeypatch.setattr(requests, "request", forbidden)
    # An invalid unused provider cache must not affect the selected provider.
    (tmp_path / "etoro.json").write_text("not JSON")
    config = settings(tmp_path, mode, ETORO_API_KEY="", ETORO_USER_KEY="", ALPACA_DATA_FEED="sip")
    clients = build_runtime_clients(config)
    execution = clients.execution_broker.delegate
    assert isinstance(execution, AlpacaBrokerClient)
    assert isinstance(clients.rest_market_data, AlpacaRestMarketDataClient)
    assert isinstance(clients.live_market_data, AlpacaMarketDataFeed)
    assert execution.http.base_url == f"https://{host}.alpaca.markets"
    assert execution.trade_stream.url == f"wss://{host}.alpaca.markets/stream"
    assert clients.rest_market_data.http.base_url == "https://data.alpaca.markets"
    assert clients.live_market_data._stream.url == "wss://stream.data.alpaca.markets/v2/sip"
    assert clients.live_market_data._trade_stream is execution.trade_stream
    assert execution.http._headers == {
        "APCA-API-KEY-ID": "alpaca-key",
        "APCA-API-SECRET-KEY": "alpaca-secret",
    }
    assert clients.rest_market_data.http._headers == execution.http._headers
    assert execution.trade_stream._auth["secret"] == "alpaca-secret"
    assert clients.live_market_data._stream._auth["key"] == "alpaca-key"
    assert execution.instrument_cache.path == tmp_path / "alpaca.json"
    assert execution.store.path == tmp_path / f"state.sqlite.{mode}.orders.sqlite"
    assert execution.trade_stream._thread is None


@pytest.mark.parametrize("mode", ["paper", "etoro_demo", "etoro_live"])
def test_etoro_modes_ignore_alpaca_credentials_and_cache(tmp_path, monkeypatch, mode):
    def forbidden(*args, **kwargs):
        raise AssertionError("Alpaca must not be constructed for an eToro mode")

    monkeypatch.setattr("app.runtime.factories.AlpacaHttpClient", forbidden)
    (tmp_path / "alpaca.json").write_text("not JSON")
    clients = build_runtime_clients(
        settings(tmp_path, mode, ALPACA_API_KEY="", ALPACA_SECRET_KEY="")
    )
    assert isinstance(clients.rest_market_data, EtoroRestMarketDataClient)
    assert clients.rest_market_data.api_key == "etoro-key"
    assert not list(tmp_path.glob("*.orders.sqlite"))


@pytest.mark.parametrize("missing", ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"])
def test_selected_alpaca_credentials_are_required_without_etoro_fallback(tmp_path, missing):
    with pytest.raises(ValueError, match="Alpaca API key and secret"):
        build_runtime_clients(settings(tmp_path, **{missing: " "}))
    assert not list(tmp_path.glob("*.orders.sqlite"))


def test_credentials_do_not_enter_manifest_or_settings_repr(tmp_path):
    config = settings(tmp_path)
    snapshot = _sanitized_settings_snapshot(config)
    for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ETORO_API_KEY", "ETORO_USER_KEY"):
        assert key not in snapshot
    assert "alpaca-key" not in repr(config)
    assert "alpaca-secret" not in repr(config)
    assert snapshot["BROKER"] == "alpaca_demo"


def test_same_account_id_cannot_cross_demo_live_journal(tmp_path):
    path = str(tmp_path / "shared.sqlite")
    AlpacaOrderStore(path, environment=AlpacaEnvironment.DEMO).bind_account("account")
    live = AlpacaOrderStore(path, environment=AlpacaEnvironment.LIVE)
    with pytest.raises(ValueError, match="another account"):
        live.bind_account("account")


def test_mixed_http_and_journal_environments_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="environments differ"):
        AlpacaBrokerClient(
            AlpacaHttpClient("key", "secret", environment=AlpacaEnvironment.LIVE),
            AlpacaOrderStore(str(tmp_path / "demo.sqlite")),
            api_key="key",
            secret_key="secret",
        )


def test_v3_live_guard_and_temporary_demo_integration_guard_are_explicit():
    for mode in ("alpaca_live", "etoro_live"):
        with pytest.raises(RuntimeError, match="not prospectively validated"):
            _assert_v3_execution_mode(mode)
    with pytest.raises(RuntimeError, match="universe and full-runtime"):
        _assert_v3_execution_mode("alpaca_demo")


def test_instrument_cache_is_used_after_restart_but_permissions_are_fresh(tmp_path):
    calls = []
    asset = {"id": ASSET_ID, "symbol": "AAPL", "tradable": True}

    class Http:
        def request(self, method, path):
            calls.append(path)
            return dict(asset)

    cache = AlpacaInstrumentCache(Http(), str(tmp_path / "ids.json"))
    assert cache.get_asset("AAPL")["tradable"] is True
    assert json.loads(cache.path.read_text()) == {"AAPL": ASSET_ID}
    asset["tradable"] = False
    restored = AlpacaInstrumentCache(Http(), str(cache.path))
    assert restored.get_asset("AAPL")["tradable"] is False
    assert calls == ["/v2/assets/AAPL", "/v2/assets/" + ASSET_ID]


def test_obsolete_cached_uuid_is_refreshed_only_after_404(tmp_path):
    class Http:
        def request(self, method, path):
            if path.endswith(ASSET_ID):
                response = requests.Response()
                response.status_code = 404
                raise requests.HTTPError(response=response)
            return {"id": str(UUID(int=2)), "symbol": "AAPL"}

    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"AAPL": ASSET_ID}))
    cache = AlpacaInstrumentCache(Http(), str(path))
    assert cache.get_asset("AAPL")["id"] == str(UUID(int=2))
    assert json.loads(path.read_text())["AAPL"] == str(UUID(int=2))


def test_cache_cannot_alias_a_different_symbol(tmp_path):
    class Http:
        def request(self, method, path):
            return {"id": ASSET_ID, "symbol": "MSFT"}

    cache = AlpacaInstrumentCache(Http(), str(tmp_path / "ids.json"))
    with pytest.raises(ValueError, match="requested symbol"):
        cache.get_asset("AAPL")
    assert not cache.path.exists()


def test_example_env_exposes_both_provider_configurations():
    config = Settings(_env_file=Path(".env.example"))
    assert config.alpaca_api_key == "replace_me"
    assert config.alpaca_secret_key == "replace_me"
    assert config.alpaca_instrument_id_cache_path == "data/alpaca_instrument_ids.json"


def test_selected_feed_owns_market_and_trade_stream_lifecycles(tmp_path, monkeypatch):
    clients = build_runtime_clients(settings(tmp_path))
    feed = clients.live_market_data
    calls = []
    for name, stream in (("quotes", feed._stream), ("orders", feed._trade_stream)):
        monkeypatch.setattr(
            stream, "start", lambda *args, label=name: calls.append((label, "start"))
        )
        monkeypatch.setattr(stream, "stop", lambda label=name: calls.append((label, "stop")))
    feed.start(["AAPL"])
    feed.stop()
    assert calls == [
        ("orders", "start"),
        ("quotes", "start"),
        ("quotes", "stop"),
        ("orders", "stop"),
    ]


def test_empty_cache_path_is_rejected_before_network_access():
    with pytest.raises(ValueError, match="must name a cache file"):
        AlpacaInstrumentCache(object(), "")
