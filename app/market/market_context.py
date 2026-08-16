from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass, MarketContextConfig
from app.instruments.sector_map import DEFAULT_SECTOR_BY_SYMBOL
from app.market.models import MarketSnapshot
from app.market.relative_spread import (
    SPREAD_REFERENCE_MAX_OBSERVATIONS,
    SpreadContext,
    build_relative_spread_context,
)
from app.market.session_rules import TradingSessionDecision

MARKET_CONTEXT_VERSION = 'market_context_v3'
SIDE_NEUTRAL_RESEARCH_MARKET_CONTEXT_VERSION = (
    'side_neutral_research_market_context_v1'
)
MARKET_CONTEXT_HISTORY_RETENTION_SECONDS = 24 * 60 * 60


class MarketDirection(StrEnum):
    BULLISH = 'bullish'
    BEARISH = 'bearish'
    NEUTRAL = 'neutral'
    UNKNOWN = 'unknown'


class MarketRegime(StrEnum):
    RISK_ON = 'risk_on'
    RISK_OFF = 'risk_off'
    MIXED = 'mixed'
    UNKNOWN = 'unknown'


class ContextAlignment(StrEnum):
    ALIGNED = 'aligned'
    NEUTRAL = 'neutral'
    OPPOSED = 'opposed'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class BenchmarkContext:
    symbol: str | None
    available: bool
    direction: MarketDirection
    session_return_percent: float | None
    momentum_percent: float | None
    spread_percent: float | None
    snapshot_age_seconds: float | None


@dataclass(frozen=True)
class BreadthContext:
    available: bool
    direction: MarketDirection
    eligible_symbols: int
    valid_symbols: int
    coverage_ratio: float
    advancing_count: int
    declining_count: int
    unchanged_count: int
    advancing_ratio: float
    median_session_return_percent: float | None


@dataclass(frozen=True)
class SectorContext:
    sector: str | None
    available: bool
    direction: MarketDirection
    member_count: int
    valid_member_count: int
    advancing_ratio: float | None
    median_session_return_percent: float | None
    benchmark_symbol: str | None = None
    benchmark_return_percent: float | None = None


@dataclass(frozen=True)
class CandidateMarketContext:
    version: str
    as_of: datetime
    asset_class: AssetClass
    regime: MarketRegime
    alignment: ContextAlignment
    benchmark: BenchmarkContext
    breadth: BreadthContext
    sector: SectorContext
    symbol_session_return_percent: float | None
    symbol_relative_strength_percent: float | None
    reasons: tuple[str, ...]
    spread: SpreadContext | None = None


@dataclass(frozen=True)
class SideNeutralResearchMarketContext:
    version: str
    as_of: datetime
    asset_class: AssetClass
    regime: MarketRegime
    benchmark: BenchmarkContext
    breadth: BreadthContext
    sector: SectorContext
    symbol_session_return_percent: float | None
    symbol_relative_strength_percent: float | None
    spread: SpreadContext
    latest_symbol_timestamp: datetime | None


@dataclass(frozen=True)
class _HistoricalSnapshot:
    session_key: str | None
    snapshot: MarketSnapshot


class MarketContextService:
    def __init__(
        self,
        *,
        instrument_registry: InstrumentRegistry,
        benchmark_symbols: Mapping[AssetClass, tuple[str, ...]] | None = None,
        sector_by_symbol: Mapping[str, str] | None = None,
    ) -> None:
        self.instrument_registry = instrument_registry
        self.benchmark_symbols = {
            asset_class: tuple(symbol.strip().upper() for symbol in symbols if symbol.strip())
            for asset_class, symbols in (benchmark_symbols or {}).items()
        }
        self.sector_by_symbol = {
            symbol.strip().upper(): sector
            for symbol, sector in (sector_by_symbol or DEFAULT_SECTOR_BY_SYMBOL).items()
        }
        self._latest: dict[str, MarketSnapshot] = {}
        self._history: dict[str, deque[_HistoricalSnapshot]] = {}
        self._spread_latest: dict[str, MarketSnapshot] = {}
        self._spread_history: dict[str, deque[MarketSnapshot]] = {}
        self._asset_class_by_symbol: dict[str, AssetClass] = {}
        self._session_key_by_symbol: dict[str, str] = {}
        self._session_open: dict[tuple[str, str], float] = {}
        self._active_trading_symbols: dict[AssetClass, set[str]] = {
            asset_class: set() for asset_class in AssetClass
        }

    def reset_session(self, session_key: str) -> None:
        completed_symbols = {
            symbol
            for symbol, key in self._session_key_by_symbol.items()
            if key == session_key
        }
        for symbol in completed_symbols:
            self._spread_latest.pop(symbol, None)
            self._spread_history.pop(symbol, None)
        self._session_open = {
            key: value for key, value in self._session_open.items() if key[0] != session_key
        }
        self._session_key_by_symbol = {
            symbol: key
            for symbol, key in self._session_key_by_symbol.items()
            if key != session_key
        }
        self._history = {
            symbol: deque(item for item in history if item.session_key != session_key)
            for symbol, history in self._history.items()
        }

    def observe_accepted_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Retain every accepted quote for prior-only spread context."""

        self._observe_spread_snapshot(snapshot.symbol, snapshot)

    def update(
        self,
        *,
        snapshots: Mapping[str, MarketSnapshot],
        session_decisions: Mapping[str, TradingSessionDecision],
        context_asset_classes: Mapping[str, AssetClass] | None = None,
    ) -> None:
        context_assets = {
            symbol.strip().upper(): asset_class
            for symbol, asset_class in (context_asset_classes or {}).items()
        }
        active_session_by_asset: dict[AssetClass, str] = {}
        self._active_trading_symbols = {asset_class: set() for asset_class in AssetClass}

        for raw_symbol, decision in session_decisions.items():
            symbol = raw_symbol.strip().upper()
            try:
                asset_class = self.instrument_registry.resolve(symbol).asset_class
            except ValueError:
                continue
            if decision.collect_snapshots:
                self._active_trading_symbols[asset_class].add(symbol)
            if decision.session_key:
                active_session_by_asset.setdefault(asset_class, decision.session_key)
                self._session_key_by_symbol[symbol] = decision.session_key

        for raw_symbol, snapshot in snapshots.items():
            symbol = raw_symbol.strip().upper()
            try:
                asset_class = self.instrument_registry.resolve(symbol).asset_class
            except ValueError:
                asset_class = context_assets.get(symbol)
            if asset_class is None:
                continue

            self._observe_spread_snapshot(symbol, snapshot)
            self._latest[symbol] = snapshot
            self._asset_class_by_symbol[symbol] = asset_class
            session_key = self._session_key_by_symbol.get(symbol) or active_session_by_asset.get(asset_class)
            if session_key:
                self._session_key_by_symbol[symbol] = session_key
                self._session_open.setdefault((session_key, symbol), snapshot.last)
            self._append_history(symbol=symbol, snapshot=snapshot, session_key=session_key)

    def build_candidate_context(
        self,
        *,
        symbol: str,
        side: str,
        as_of: datetime,
    ) -> CandidateMarketContext:
        normalized_symbol = symbol.strip().upper()
        asset_class = self.instrument_registry.resolve(normalized_symbol).asset_class
        config = self.instrument_registry.config_for(normalized_symbol).market_context
        actual_as_of = _as_utc(as_of)
        session_key = self._session_key_by_symbol.get(normalized_symbol)
        symbol_return = self._session_return(normalized_symbol, session_key)
        benchmark = self._benchmark_context(
            asset_class=asset_class,
            session_key=session_key,
            config=config,
            as_of=actual_as_of,
        )
        breadth = self._breadth_context(
            asset_class=asset_class,
            session_key=session_key,
            config=config,
            as_of=actual_as_of,
        )
        sector = self._sector_context(
            symbol=normalized_symbol,
            asset_class=asset_class,
            session_key=session_key,
            config=config,
            as_of=actual_as_of,
        )
        regime = _market_regime(benchmark.direction, breadth.direction)
        alignment = _context_alignment(side, regime, sector.direction)
        benchmark_return = benchmark.session_return_percent
        relative_strength = (
            symbol_return - benchmark_return
            if symbol_return is not None and benchmark_return is not None
            else None
        )
        reasons = _context_reasons(
            benchmark=benchmark,
            breadth=breadth,
            sector=sector,
            regime=regime,
            alignment=alignment,
        )
        spread = self._spread_context(normalized_symbol)
        return CandidateMarketContext(
            version=MARKET_CONTEXT_VERSION,
            as_of=actual_as_of,
            asset_class=asset_class,
            regime=regime,
            alignment=alignment,
            benchmark=benchmark,
            breadth=breadth,
            sector=sector,
            symbol_session_return_percent=_round_optional(symbol_return),
            symbol_relative_strength_percent=_round_optional(relative_strength),
            reasons=reasons,
            spread=spread,
        )

    def build_side_neutral_research_context(
        self,
        *,
        symbol: str,
        as_of: datetime,
    ) -> SideNeutralResearchMarketContext:
        """Build strict-cutoff context without using a BUY/SELL side.

        This method is research-only and does not mutate any live context state.
        Both broker and receive timestamps must be strictly earlier than the
        requested cutoff.
        """

        normalized_symbol = symbol.strip().upper()
        asset_class = self.instrument_registry.resolve(
            normalized_symbol
        ).asset_class
        config = self.instrument_registry.config_for(
            normalized_symbol
        ).market_context
        actual_as_of = _as_utc(as_of)
        session_key = self._session_key_by_symbol.get(normalized_symbol)
        symbol_snapshot = self._snapshot_before(
            normalized_symbol,
            actual_as_of,
        )
        symbol_return = self._session_return_before(
            normalized_symbol,
            session_key,
            actual_as_of,
        )
        benchmark = self._research_benchmark_context(
            asset_class=asset_class,
            session_key=session_key,
            config=config,
            as_of=actual_as_of,
        )
        breadth = self._research_breadth_context(
            asset_class=asset_class,
            session_key=session_key,
            config=config,
            as_of=actual_as_of,
        )
        sector = self._research_sector_context(
            symbol=normalized_symbol,
            asset_class=asset_class,
            session_key=session_key,
            config=config,
            as_of=actual_as_of,
        )
        benchmark_return = benchmark.session_return_percent
        relative_strength = (
            symbol_return - benchmark_return
            if symbol_return is not None and benchmark_return is not None
            else None
        )
        return SideNeutralResearchMarketContext(
            version=SIDE_NEUTRAL_RESEARCH_MARKET_CONTEXT_VERSION,
            as_of=actual_as_of,
            asset_class=asset_class,
            regime=_market_regime(
                benchmark.direction,
                breadth.direction,
            ),
            benchmark=benchmark,
            breadth=breadth,
            sector=sector,
            symbol_session_return_percent=_round_optional(symbol_return),
            symbol_relative_strength_percent=_round_optional(
                relative_strength
            ),
            spread=self._spread_context_before(
                normalized_symbol,
                actual_as_of,
            ),
            latest_symbol_timestamp=(
                None
                if symbol_snapshot is None
                else _as_utc(symbol_snapshot.timestamp)
            ),
        )

    def _snapshot_before(
        self,
        symbol: str,
        as_of: datetime,
    ) -> MarketSnapshot | None:
        for item in reversed(self._history.get(symbol, ())):
            snapshot = item.snapshot
            if _snapshot_is_strictly_before(snapshot, as_of):
                return snapshot
        return None

    def _session_return_before(
        self,
        symbol: str,
        session_key: str | None,
        as_of: datetime,
    ) -> float | None:
        if session_key is None:
            return None
        history = self._history.get(symbol, ())
        opening_snapshot = next(
            (
                item.snapshot
                for item in history
                if item.session_key == session_key
                and _snapshot_is_strictly_before(item.snapshot, as_of)
            ),
            None,
        )
        current_snapshot = next(
            (
                item.snapshot
                for item in reversed(history)
                if item.session_key == session_key
                and _snapshot_is_strictly_before(item.snapshot, as_of)
            ),
            None,
        )
        if opening_snapshot is None or current_snapshot is None:
            return None
        opening = opening_snapshot.last
        current = current_snapshot.last
        if opening <= 0:
            return None
        return ((current - opening) / opening) * 100

    def _research_benchmark_context(
        self,
        *,
        asset_class: AssetClass,
        session_key: str | None,
        config: MarketContextConfig,
        as_of: datetime,
    ) -> BenchmarkContext:
        configured_symbols = self.benchmark_symbols.get(asset_class, ())
        for symbol in configured_symbols:
            snapshot = self._snapshot_before(symbol, as_of)
            if snapshot is None or not self._research_snapshot_is_fresh(
                snapshot,
                as_of,
                config,
            ):
                continue
            session_return = self._session_return_before(
                symbol,
                session_key,
                as_of,
            )
            momentum = self._momentum_percent_before(
                symbol=symbol,
                session_key=session_key,
                as_of=as_of,
                window_seconds=config.momentum_window_seconds,
                maximum_reference_lag_seconds=(
                    config.maximum_context_age_seconds
                ),
            )
            return BenchmarkContext(
                symbol=symbol,
                available=True,
                direction=_direction_from_return(
                    session_return,
                    config.minimum_benchmark_move_percent,
                ),
                session_return_percent=_round_optional(session_return),
                momentum_percent=_round_optional(momentum),
                spread_percent=_round_optional(_spread_percent(snapshot)),
                snapshot_age_seconds=round(
                    max(
                        0.0,
                        (
                            as_of - _as_utc(snapshot.timestamp)
                        ).total_seconds(),
                    ),
                    3,
                ),
            )
        return BenchmarkContext(
            symbol=configured_symbols[0] if configured_symbols else None,
            available=False,
            direction=MarketDirection.UNKNOWN,
            session_return_percent=None,
            momentum_percent=None,
            spread_percent=None,
            snapshot_age_seconds=None,
        )

    def _research_breadth_context(
        self,
        *,
        asset_class: AssetClass,
        session_key: str | None,
        config: MarketContextConfig,
        as_of: datetime,
    ) -> BreadthContext:
        eligible = sorted(
            self._active_trading_symbols.get(asset_class, set())
        )
        returns = [
            value
            for symbol in eligible
            if self._research_symbol_is_fresh(
                symbol,
                as_of,
                config,
            )
            for value in [
                self._session_return_before(symbol, session_key, as_of)
            ]
            if value is not None
        ]
        valid = len(returns)
        coverage = valid / len(eligible) if eligible else 0.0
        advancing = sum(
            value > config.unchanged_band_percent for value in returns
        )
        declining = sum(
            value < -config.unchanged_band_percent for value in returns
        )
        unchanged = valid - advancing - declining
        advancing_ratio = advancing / valid if valid else 0.0
        available = (
            valid >= config.minimum_breadth_sample_size
            and coverage >= config.minimum_breadth_coverage_ratio
        )
        return BreadthContext(
            available=available,
            direction=(
                _breadth_direction(advancing_ratio, config)
                if available
                else MarketDirection.UNKNOWN
            ),
            eligible_symbols=len(eligible),
            valid_symbols=valid,
            coverage_ratio=round(coverage, 4),
            advancing_count=advancing,
            declining_count=declining,
            unchanged_count=unchanged,
            advancing_ratio=round(advancing_ratio, 4),
            median_session_return_percent=_round_optional(
                _median(returns)
            ),
        )

    def _research_sector_context(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        session_key: str | None,
        config: MarketContextConfig,
        as_of: datetime,
    ) -> SectorContext:
        sector = self.sector_by_symbol.get(symbol)
        if sector is None:
            return SectorContext(
                None,
                False,
                MarketDirection.UNKNOWN,
                0,
                0,
                None,
                None,
            )
        members = sorted(
            candidate
            for candidate in self._active_trading_symbols.get(
                asset_class,
                set(),
            )
            if self.sector_by_symbol.get(candidate) == sector
        )
        returns = [
            value
            for member in members
            if self._research_symbol_is_fresh(
                member,
                as_of,
                config,
            )
            for value in [
                self._session_return_before(member, session_key, as_of)
            ]
            if value is not None
        ]
        valid = len(returns)
        advancing_ratio = (
            sum(
                value > config.unchanged_band_percent
                for value in returns
            )
            / valid
            if valid
            else None
        )
        available = valid >= config.minimum_sector_sample_size
        return SectorContext(
            sector=sector,
            available=available,
            direction=(
                _breadth_direction(advancing_ratio or 0.0, config)
                if available
                else MarketDirection.UNKNOWN
            ),
            member_count=len(members),
            valid_member_count=valid,
            advancing_ratio=_round_optional(advancing_ratio),
            median_session_return_percent=_round_optional(
                _median(returns)
            ),
        )

    def _momentum_percent_before(
        self,
        *,
        symbol: str,
        session_key: str | None,
        as_of: datetime,
        window_seconds: int,
        maximum_reference_lag_seconds: int,
    ) -> float | None:
        current = self._snapshot_before(symbol, as_of)
        if current is None or session_key is None or window_seconds <= 0:
            return None
        target_time = _as_utc(current.timestamp) - timedelta(
            seconds=window_seconds
        )
        reference: MarketSnapshot | None = None
        for item in reversed(self._history.get(symbol, ())):
            if item.session_key != session_key:
                continue
            snapshot = item.snapshot
            if not _snapshot_is_strictly_before(snapshot, as_of):
                continue
            if _as_utc(snapshot.timestamp) <= target_time:
                reference = snapshot
                break
        if reference is None or reference.last <= 0:
            return None
        reference_lag = (
            target_time - _as_utc(reference.timestamp)
        ).total_seconds()
        if reference_lag > maximum_reference_lag_seconds:
            return None
        return ((current.last - reference.last) / reference.last) * 100

    def _research_symbol_is_fresh(
        self,
        symbol: str,
        as_of: datetime,
        config: MarketContextConfig,
    ) -> bool:
        snapshot = self._snapshot_before(symbol, as_of)
        return (
            snapshot is not None
            and self._research_snapshot_is_fresh(
                snapshot,
                as_of,
                config,
            )
        )

    @staticmethod
    def _research_snapshot_is_fresh(
        snapshot: MarketSnapshot,
        as_of: datetime,
        config: MarketContextConfig,
    ) -> bool:
        age = (as_of - _as_utc(snapshot.timestamp)).total_seconds()
        receive_age = (
            as_of - _as_utc(snapshot.received_at or snapshot.timestamp)
        ).total_seconds()
        return (
            0 < age <= config.maximum_context_age_seconds
            and 0 < receive_age <= config.maximum_context_age_seconds
        )

    def _spread_context_before(
        self,
        symbol: str,
        as_of: datetime,
    ) -> SpreadContext:
        eligible = [
            snapshot
            for snapshot in self._spread_history.get(symbol, ())
            if _snapshot_is_strictly_before(snapshot, as_of)
        ]
        current = eligible[-1] if eligible else None
        current_timestamp = (
            None if current is None else _as_utc(current.timestamp)
        )
        prior = [
            snapshot
            for snapshot in eligible
            if current_timestamp is not None
            and _as_utc(snapshot.timestamp) < current_timestamp
        ]
        return build_relative_spread_context(
            current=current,
            prior_snapshots=prior,
        )

    def _spread_context(self, symbol: str) -> SpreadContext:
        current = self._spread_latest.get(symbol)
        current_timestamp = (
            None if current is None else _as_utc(current.timestamp)
        )
        prior = [
            item
            for item in self._spread_history.get(symbol, ())
            if current_timestamp is not None
            and _as_utc(item.timestamp) < current_timestamp
        ]
        return build_relative_spread_context(
            current=current,
            prior_snapshots=prior,
        )

    def _observe_spread_snapshot(
        self,
        raw_symbol: str,
        snapshot: MarketSnapshot,
    ) -> None:
        symbol = raw_symbol.strip().upper()
        previous = self._spread_latest.get(symbol)
        self._spread_latest[symbol] = snapshot
        if previous is not None and _same_quote(previous, snapshot):
            return
        history = self._spread_history.setdefault(
            symbol,
            deque(maxlen=SPREAD_REFERENCE_MAX_OBSERVATIONS + 1),
        )
        history.append(snapshot)

    def _benchmark_context(
        self,
        *,
        asset_class: AssetClass,
        session_key: str | None,
        config: MarketContextConfig,
        as_of: datetime,
    ) -> BenchmarkContext:
        configured_symbols = self.benchmark_symbols.get(asset_class, ())
        for symbol in configured_symbols:
            snapshot = self._latest.get(symbol)
            if snapshot is None or not self._is_fresh(snapshot, as_of, config):
                continue
            session_return = self._session_return(symbol, session_key)
            momentum = self._momentum_percent(
                symbol=symbol,
                session_key=session_key,
                window_seconds=config.momentum_window_seconds,
                maximum_reference_lag_seconds=config.maximum_context_age_seconds,
            )
            return BenchmarkContext(
                symbol=symbol,
                available=True,
                direction=_direction_from_return(
                    session_return,
                    config.minimum_benchmark_move_percent,
                ),
                session_return_percent=_round_optional(session_return),
                momentum_percent=_round_optional(momentum),
                spread_percent=_round_optional(_spread_percent(snapshot)),
                snapshot_age_seconds=round(
                    max(0.0, (as_of - _as_utc(snapshot.timestamp)).total_seconds()),
                    3,
                ),
            )
        return BenchmarkContext(
            symbol=configured_symbols[0] if configured_symbols else None,
            available=False,
            direction=MarketDirection.UNKNOWN,
            session_return_percent=None,
            momentum_percent=None,
            spread_percent=None,
            snapshot_age_seconds=None,
        )

    def _breadth_context(
        self,
        *,
        asset_class: AssetClass,
        session_key: str | None,
        config: MarketContextConfig,
        as_of: datetime,
    ) -> BreadthContext:
        eligible = sorted(self._active_trading_symbols.get(asset_class, set()))
        returns = [
            value
            for symbol in eligible
            if self._is_symbol_fresh(symbol, as_of, config)
            for value in [self._session_return(symbol, session_key)]
            if value is not None
        ]
        valid = len(returns)
        coverage = valid / len(eligible) if eligible else 0.0
        advancing = sum(value > config.unchanged_band_percent for value in returns)
        declining = sum(value < -config.unchanged_band_percent for value in returns)
        unchanged = valid - advancing - declining
        advancing_ratio = advancing / valid if valid else 0.0
        available = (
            valid >= config.minimum_breadth_sample_size
            and coverage >= config.minimum_breadth_coverage_ratio
        )
        direction = (
            _breadth_direction(advancing_ratio, config)
            if available
            else MarketDirection.UNKNOWN
        )
        return BreadthContext(
            available=available,
            direction=direction,
            eligible_symbols=len(eligible),
            valid_symbols=valid,
            coverage_ratio=round(coverage, 4),
            advancing_count=advancing,
            declining_count=declining,
            unchanged_count=unchanged,
            advancing_ratio=round(advancing_ratio, 4),
            median_session_return_percent=_round_optional(_median(returns)),
        )

    def _sector_context(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        session_key: str | None,
        config: MarketContextConfig,
        as_of: datetime,
    ) -> SectorContext:
        sector = self.sector_by_symbol.get(symbol)
        if sector is None:
            return SectorContext(None, False, MarketDirection.UNKNOWN, 0, 0, None, None)
        members = sorted(
            candidate
            for candidate in self._active_trading_symbols.get(asset_class, set())
            if self.sector_by_symbol.get(candidate) == sector
        )
        returns = [
            value
            for member in members
            if self._is_symbol_fresh(member, as_of, config)
            for value in [self._session_return(member, session_key)]
            if value is not None
        ]
        valid = len(returns)
        advancing_ratio = (
            sum(value > config.unchanged_band_percent for value in returns) / valid
            if valid
            else None
        )
        available = valid >= config.minimum_sector_sample_size
        direction = (
            _breadth_direction(advancing_ratio or 0.0, config)
            if available
            else MarketDirection.UNKNOWN
        )
        return SectorContext(
            sector=sector,
            available=available,
            direction=direction,
            member_count=len(members),
            valid_member_count=valid,
            advancing_ratio=_round_optional(advancing_ratio),
            median_session_return_percent=_round_optional(_median(returns)),
        )

    def _session_return(self, symbol: str, session_key: str | None) -> float | None:
        snapshot = self._latest.get(symbol)
        if snapshot is None or session_key is None:
            return None
        opening = self._session_open.get((session_key, symbol))
        if opening is None or opening <= 0:
            return None
        return ((snapshot.last - opening) / opening) * 100

    def _append_history(
        self,
        *,
        symbol: str,
        snapshot: MarketSnapshot,
        session_key: str | None,
    ) -> None:
        history = self._history.setdefault(symbol, deque())
        actual_timestamp = _as_utc(snapshot.timestamp)
        if history:
            latest_timestamp = _as_utc(history[-1].snapshot.timestamp)
            if actual_timestamp < latest_timestamp:
                return
            item = _HistoricalSnapshot(session_key=session_key, snapshot=snapshot)
            if actual_timestamp == latest_timestamp:
                history[-1] = item
            else:
                history.append(item)
        else:
            history.append(_HistoricalSnapshot(session_key=session_key, snapshot=snapshot))

        cutoff = actual_timestamp - timedelta(
            seconds=MARKET_CONTEXT_HISTORY_RETENTION_SECONDS
        )
        while history and _as_utc(history[0].snapshot.timestamp) < cutoff:
            history.popleft()

    def _momentum_percent(
        self,
        *,
        symbol: str,
        session_key: str | None,
        window_seconds: int,
        maximum_reference_lag_seconds: int,
    ) -> float | None:
        snapshot = self._latest.get(symbol)
        if snapshot is None or session_key is None or window_seconds <= 0:
            return None

        target_time = _as_utc(snapshot.timestamp) - timedelta(seconds=window_seconds)
        reference: MarketSnapshot | None = None
        for item in reversed(self._history.get(symbol, ())):
            if item.session_key != session_key:
                continue
            if _as_utc(item.snapshot.timestamp) <= target_time:
                reference = item.snapshot
                break

        if reference is None or reference.last <= 0:
            return None

        reference_lag = (
            target_time - _as_utc(reference.timestamp)
        ).total_seconds()
        if reference_lag > maximum_reference_lag_seconds:
            return None

        return ((snapshot.last - reference.last) / reference.last) * 100

    def _is_symbol_fresh(
        self,
        symbol: str,
        as_of: datetime,
        config: MarketContextConfig,
    ) -> bool:
        snapshot = self._latest.get(symbol)
        return snapshot is not None and self._is_fresh(snapshot, as_of, config)

    def _is_fresh(
        self,
        snapshot: MarketSnapshot,
        as_of: datetime,
        config: MarketContextConfig,
    ) -> bool:
        age = (as_of - _as_utc(snapshot.timestamp)).total_seconds()
        return -5 <= age <= config.maximum_context_age_seconds


def _market_regime(
    benchmark: MarketDirection,
    breadth: MarketDirection,
) -> MarketRegime:
    known = [
        direction
        for direction in (benchmark, breadth)
        if direction != MarketDirection.UNKNOWN
    ]
    if not known:
        return MarketRegime.UNKNOWN
    if all(direction == MarketDirection.BULLISH for direction in known):
        return MarketRegime.RISK_ON
    if all(direction == MarketDirection.BEARISH for direction in known):
        return MarketRegime.RISK_OFF
    if all(direction == MarketDirection.NEUTRAL for direction in known):
        return MarketRegime.MIXED
    return MarketRegime.MIXED


def _context_alignment(
    side: str,
    regime: MarketRegime,
    sector_direction: MarketDirection,
) -> ContextAlignment:
    normalized_side = side.strip().upper()
    if regime == MarketRegime.UNKNOWN:
        return ContextAlignment.UNKNOWN
    aligned_regime = (
        regime == MarketRegime.RISK_ON
        if normalized_side == 'BUY'
        else regime == MarketRegime.RISK_OFF
    )
    opposed_regime = (
        regime == MarketRegime.RISK_OFF
        if normalized_side == 'BUY'
        else regime == MarketRegime.RISK_ON
    )
    opposed_sector = (
        sector_direction == MarketDirection.BEARISH
        if normalized_side == 'BUY'
        else sector_direction == MarketDirection.BULLISH
    )
    if opposed_regime or opposed_sector:
        return ContextAlignment.OPPOSED
    if aligned_regime:
        return ContextAlignment.ALIGNED
    return ContextAlignment.NEUTRAL


def _context_reasons(
    *,
    benchmark: BenchmarkContext,
    breadth: BreadthContext,
    sector: SectorContext,
    regime: MarketRegime,
    alignment: ContextAlignment,
) -> tuple[str, ...]:
    return (
        'benchmark_available' if benchmark.available else 'benchmark_unavailable',
        'breadth_available' if breadth.available else 'breadth_unavailable',
        'sector_available' if sector.available else 'sector_unavailable',
        f'market_regime_{regime.value}',
        f'context_alignment_{alignment.value}',
    )


def _direction_from_return(
    value: float | None,
    minimum_move_percent: float,
) -> MarketDirection:
    if value is None:
        return MarketDirection.UNKNOWN
    if value >= minimum_move_percent:
        return MarketDirection.BULLISH
    if value <= -minimum_move_percent:
        return MarketDirection.BEARISH
    return MarketDirection.NEUTRAL


def _breadth_direction(
    advancing_ratio: float,
    config: MarketContextConfig,
) -> MarketDirection:
    if advancing_ratio >= config.bullish_advancing_ratio:
        return MarketDirection.BULLISH
    if advancing_ratio <= config.bearish_advancing_ratio:
        return MarketDirection.BEARISH
    return MarketDirection.NEUTRAL


def _spread_percent(snapshot: MarketSnapshot) -> float | None:
    midpoint = (snapshot.bid + snapshot.ask) / 2
    if midpoint <= 0:
        return None
    return ((snapshot.ask - snapshot.bid) / midpoint) * 100


def _snapshot_is_strictly_before(
    snapshot: MarketSnapshot,
    as_of: datetime,
) -> bool:
    cutoff = _as_utc(as_of)
    return (
        _as_utc(snapshot.timestamp) < cutoff
        and _as_utc(snapshot.received_at or snapshot.timestamp) < cutoff
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_quote(left: MarketSnapshot, right: MarketSnapshot) -> bool:
    return (
        _as_utc(left.timestamp) == _as_utc(right.timestamp)
        and left.bid == right.bid
        and left.ask == right.ask
        and left.last == right.last
    )
