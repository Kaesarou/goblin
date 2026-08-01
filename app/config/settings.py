from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.execution.breakeven_profile import BreakevenProfileName

BrokerMode = Literal['paper', 'etoro_demo', 'etoro_live']
JournalDetailLevel = Literal['minimal', 'normal', 'debug', 'full']


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='forbid',
    )

    app_log_path: str = Field(default='data/logs/goblin.log', alias='APP_LOG_PATH')
    position_store_path: str = Field(
        default='data/goblin.sqlite',
        alias='POSITION_STORE_PATH',
    )
    journal_path: str = Field(
        default='data/logs/trades.jsonl',
        alias='JOURNAL_PATH',
    )
    market_log_path: str = Field(
        default='data/logs/market.jsonl.gz',
        alias='MARKET_LOG_PATH',
    )
    candle_journal_path: str = Field(
        default='data/logs/candles.jsonl.gz',
        alias='CANDLE_JOURNAL_PATH',
    )
    errors_journal_path: str = Field(
        default='data/logs/errors.jsonl',
        alias='ERRORS_JOURNAL_PATH',
    )
    debug_decisions_journal_path: str = Field(
        default='data/logs/debug_decisions.jsonl.gz',
        alias='DEBUG_DECISIONS_JOURNAL_PATH',
    )
    daily_summary_path: str = Field(
        default='data/logs/daily_summary.json',
        alias='DAILY_SUMMARY_PATH',
    )
    partial_daily_summary_path: str = Field(
        default='data/logs/daily_summary.partial.json',
        alias='PARTIAL_DAILY_SUMMARY_PATH',
    )
    run_manifest_path: str = Field(
        default='data/logs/run_manifest.json',
        alias='RUN_MANIFEST_PATH',
    )
    journal_detail_level: JournalDetailLevel = Field(
        default='normal',
        alias='JOURNAL_DETAIL_LEVEL',
    )
    journal_max_runs: int = Field(default=30, alias='JOURNAL_MAX_RUNS')
    runtime_heartbeat_minutes: int = Field(
        default=5,
        alias='RUNTIME_HEARTBEAT_MINUTES',
    )

    broker: BrokerMode = Field(default='paper', alias='BROKER')
    log_level: str = Field(default='INFO', alias='LOG_LEVEL')

    etoro_api_key: str = Field(default='', alias='ETORO_API_KEY')
    etoro_user_key: str = Field(default='', alias='ETORO_USER_KEY')
    instrument_id_cache_path: str = Field(
        default='data/etoro_instrument_ids.json',
        alias='ETORO_INSTRUMENT_ID_CACHE_PATH',
    )

    watchlist: str = Field(default='', alias='WATCHLIST')
    base_currency: str = Field(default='USD', alias='BASE_CURRENCY')
    max_open_positions: int = Field(default=1, alias='MAX_OPEN_POSITIONS')
    max_open_positions_per_symbol: int = Field(
        default=1,
        alias='MAX_OPEN_POSITIONS_PER_SYMBOL',
    )
    max_trades_per_session: int = Field(
        default=3,
        alias='MAX_TRADES_PER_SESSION',
    )
    breakeven_profile: BreakevenProfileName = Field(
        default=BreakevenProfileName.CORRECTED_BASELINE_V1,
        alias='BREAKEVEN_PROFILE',
    )

    crypto_symbols: str = Field(default='', alias='CRYPTO_SYMBOLS')
    equity_us_symbols: str = Field(default='', alias='EQUITY_US_SYMBOLS')
    equity_eu_symbols: str = Field(default='', alias='EQUITY_EU_SYMBOLS')
    market_benchmark_crypto: str = Field(
        default='Crypto10',
        alias='MARKET_BENCHMARK_CRYPTO',
    )
    market_benchmark_equity_us: str = Field(
        default='SPX500',
        alias='MARKET_BENCHMARK_EQUITY_US',
    )
    market_benchmark_equity_eu: str = Field(
        default='FRA40',
        alias='MARKET_BENCHMARK_EQUITY_EU',
    )
    trading_session_timezone: str = Field(
        default='Europe/Paris',
        alias='TRADING_SESSION_TIMEZONE',
    )
    trading_sessions_crypto: str = Field(
        default='',
        alias='TRADING_SESSIONS_CRYPTO',
    )
    trading_sessions_equity_us: str = Field(
        default='',
        alias='TRADING_SESSIONS_EQUITY_US',
    )
    trading_sessions_equity_eu: str = Field(
        default='',
        alias='TRADING_SESSIONS_EQUITY_EU',
    )

    @field_validator('journal_max_runs')
    @classmethod
    def enforce_journal_run_floor(cls, value: int) -> int:
        return max(1, value)

    def watchlist_symbols(self) -> list[str]:
        symbols = self._parse_symbols(self.watchlist)
        if not symbols:
            raise ValueError('Watchlist cannot be empty.')
        return symbols

    def benchmark_symbols_by_asset_class(self):
        from app.instruments.models import AssetClass

        return {
            AssetClass.CRYPTO: tuple(
                self._parse_symbols(self.market_benchmark_crypto)
            ),
            AssetClass.EQUITY_US: tuple(
                self._parse_symbols(self.market_benchmark_equity_us)
            ),
            AssetClass.EQUITY_EU: tuple(
                self._parse_symbols(self.market_benchmark_equity_eu)
            ),
        }

    def _parse_symbols(self, raw_symbols: str) -> list[str]:
        symbols: list[str] = []
        seen_symbols: set[str] = set()
        for raw_symbol in raw_symbols.strip().split(','):
            symbol = raw_symbol.strip().upper()
            if not symbol or symbol in seen_symbols:
                continue
            symbols.append(symbol)
            seen_symbols.add(symbol)
        return symbols


def get_settings() -> Settings:
    return Settings()
