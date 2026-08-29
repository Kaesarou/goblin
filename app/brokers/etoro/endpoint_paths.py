def open_order_path(env: str) -> str:
    if env == 'demo':
        return '/api/v2/trading/execution/demo/orders'

    return '/api/v2/trading/execution/orders'


def close_position_path(env: str, position_id: str) -> str:
    if env == 'demo':
        return f'/api/v1/trading/execution/demo/market-close-orders/positions/{position_id}'

    return f'/api/v1/trading/execution/market-close-orders/positions/{position_id}'


def order_lookup_path(env: str) -> str:
    if env == 'demo':
        return '/api/v2/trading/info/demo/orders:lookup'

    return '/api/v2/trading/info/orders:lookup'


def close_order_lookup_path(env: str, close_order_id: str) -> str:
    if env == 'demo':
        return f'/api/v1/trading/info/demo/close-orders/{close_order_id}'

    return f'/api/v1/trading/info/close-orders/{close_order_id}'


def pnl_path(env: str) -> str:
    if env == 'demo':
        return '/api/v1/trading/info/demo/pnl'

    return '/api/v1/trading/info/real/pnl'


def demo_portfolio_path() -> str:
    return '/api/v1/trading/info/demo/portfolio'


def real_portfolio_path() -> str:
    return '/api/v1/trading/info/portfolio'


def instrument_search_path() -> str:
    return '/api/v1/market-data/search'


def instrument_rates_path(instrument_ids: list[int]) -> str:
    joined_instrument_ids = ','.join(
        str(instrument_id)
        for instrument_id in instrument_ids
    )

    return f'/api/v1/market-data/instruments/rates?instrumentIds={joined_instrument_ids}'
