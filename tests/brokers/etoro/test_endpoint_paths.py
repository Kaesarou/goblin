from app.brokers.etoro.endpoint_paths import (
    close_order_lookup_path,
    close_position_path,
    demo_portfolio_path,
    instrument_rates_path,
    instrument_search_path,
    open_order_path,
    order_lookup_path,
    real_portfolio_path,
)


def test_open_order_path_uses_demo_endpoint_when_env_is_demo():
    assert open_order_path('demo') == '/api/v2/trading/execution/demo/orders'


def test_open_order_path_uses_real_endpoint_when_env_is_real():
    assert open_order_path('live') == '/api/v2/trading/execution/orders'


def test_close_position_path_uses_demo_endpoint_when_env_is_demo():
    assert close_position_path('demo', '123') == (
        '/api/v1/trading/execution/demo/market-close-orders/positions/123'
    )


def test_close_position_path_uses_real_endpoint_when_env_is_real():
    assert close_position_path('live', '123') == (
        '/api/v1/trading/execution/market-close-orders/positions/123'
    )


def test_close_order_lookup_path_uses_demo_endpoint_when_env_is_demo():
    assert close_order_lookup_path('demo', 'close-123') == (
        '/api/v1/trading/info/demo/close-orders/close-123'
    )


def test_close_order_lookup_path_uses_real_endpoint_when_env_is_real():
    assert close_order_lookup_path('live', 'close-123') == (
        '/api/v1/trading/info/close-orders/close-123'
    )


def test_order_lookup_path_uses_demo_v2_endpoint_when_env_is_demo():
    assert order_lookup_path('demo') == '/api/v2/trading/info/demo/orders:lookup'


def test_order_lookup_path_uses_real_v2_endpoint_when_env_is_real():
    assert order_lookup_path('live') == '/api/v2/trading/info/orders:lookup'


def test_demo_portfolio_path():
    assert demo_portfolio_path() == '/api/v1/trading/info/demo/portfolio'


def test_real_portfolio_path():
    assert real_portfolio_path() == '/api/v1/trading/info/portfolio'


def test_instrument_search_path():
    assert instrument_search_path() == '/api/v1/market-data/search'


def test_instrument_rates_path_joins_instrument_ids():
    assert instrument_rates_path([1001, 1002, 1003]) == (
        '/api/v1/market-data/instruments/rates?instrumentIds=1001,1002,1003'
    )


def test_instrument_rates_path_accepts_empty_list():
    assert instrument_rates_path([]) == '/api/v1/market-data/instruments/rates?instrumentIds='
