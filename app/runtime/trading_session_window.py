from datetime import datetime, time, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.instruments.models import AssetClass


SESSION_CLOSED = 'session_closed'
SESSION_TRADABLE = 'session_tradable'
TOO_CLOSE_TO_SESSION_END = 'too_close_to_session_end'
FORCE_CLOSE_BEFORE_SESSION_END = 'force_close_before_session_end'

NEW_ENTRIES_CUTOFF_MINUTES_BEFORE_SESSION_END = 60
FORCE_CLOSE_MINUTES_BEFORE_SESSION_END = 20


class TradingSessionWindow(NamedTuple):
    start: time
    end: time

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


class AssetTradingSessionConfig(NamedTuple):
    asset_class: AssetClass
    sessions: tuple[TradingSessionWindow, ...]

    @property
    def is_24_7(self) -> bool:
        return not self.sessions


class TradingSessionDecision(NamedTuple):
    asset_class: AssetClass
    session_active: bool
    session_24_7: bool
    collect_snapshots: bool
    new_entries_allowed: bool
    force_close_required: bool
    reason: str
    session_start_time: datetime | None
    session_end_time: datetime | None
    time_until_session_end_minutes: float | None
    session_key: str | None

    def state_signature(self) -> tuple[object, ...]:
        """Stable semantic session state used for transition detection.

        ``time_until_session_end_minutes`` is deliberately excluded. It is an
        informational countdown that changes on every evaluation and must not turn
        an unchanged trading session into a new state on every runtime loop.
        """
        return (
            self.asset_class,
            self.session_active,
            self.session_24_7,
            self.collect_snapshots,
            self.new_entries_allowed,
            self.force_close_required,
            self.reason,
            self.session_start_time,
            self.session_end_time,
            self.session_key,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TradingSessionDecision):
            return NotImplemented
        return self.state_signature() == other.state_signature()

    def __ne__(self, other: object) -> bool:
        equal = self.__eq__(other)
        if equal is NotImplemented:
            return NotImplemented
        return not equal

    def __hash__(self) -> int:
        return hash(self.state_signature())


class TradingSessionState:
    def __init__(self):
        self._active_session_key_by_symbol: dict[str, str | None] = {}

    def mark_and_detect_new_session(self, *, symbol: str, decision: TradingSessionDecision) -> bool:
        started, _ = self.mark_and_detect_transition(symbol=symbol, decision=decision)
        return started

    def mark_and_detect_transition(
        self,
        *,
        symbol: str,
        decision: TradingSessionDecision,
    ) -> tuple[bool, str | None]:
        previous_key = self._active_session_key_by_symbol.get(symbol)
        current_key = decision.session_key if decision.session_active else None
        self._active_session_key_by_symbol[symbol] = current_key
        new_session_started = current_key is not None and current_key != previous_key
        ended_session_key = previous_key if previous_key is not None and current_key != previous_key else None
        return new_session_started, ended_session_key


class TradingSessionService:
    def __init__(
        self,
        configs: dict[AssetClass, AssetTradingSessionConfig],
        timezone_name: str,
    ):
        self.configs = configs
        self.timezone = ZoneInfo(timezone_name)

    def evaluate(self, *, asset_class: AssetClass, now: datetime) -> TradingSessionDecision:
        config = self.configs[asset_class]
        local_now = self._to_local_time(now)
        equity = asset_class in {AssetClass.EQUITY_EU, AssetClass.EQUITY_US}
        if equity and local_now.weekday() >= 5:
            return self._decision_closed(asset_class)
        if config.is_24_7:
            return self._decision_24_7(asset_class)

        active_session = self._active_session(config.sessions, local_now)
        if active_session is None:
            return self._decision_closed(asset_class)

        if equity and active_session[0].weekday() >= 5:
            return self._decision_closed(asset_class)
        return self._decision_active(asset_class, local_now, active_session)

    def _decision_24_7(self, asset_class: AssetClass) -> TradingSessionDecision:
        return TradingSessionDecision(
            asset_class=asset_class,
            session_active=True,
            session_24_7=True,
            collect_snapshots=True,
            new_entries_allowed=True,
            force_close_required=False,
            reason=SESSION_TRADABLE,
            session_start_time=None,
            session_end_time=None,
            time_until_session_end_minutes=None,
            session_key=asset_class.value + ':24_7',
        )

    def _decision_closed(self, asset_class: AssetClass) -> TradingSessionDecision:
        return TradingSessionDecision(
            asset_class=asset_class,
            session_active=False,
            session_24_7=False,
            collect_snapshots=False,
            new_entries_allowed=False,
            force_close_required=False,
            reason=SESSION_CLOSED,
            session_start_time=None,
            session_end_time=None,
            time_until_session_end_minutes=None,
            session_key=None,
        )

    def _decision_active(
        self,
        asset_class: AssetClass,
        local_now: datetime,
        active_session: tuple[datetime, datetime],
    ) -> TradingSessionDecision:
        session_start, session_end = active_session
        time_until_end = max(0.0, (session_end - local_now).total_seconds() / 60)
        reason = SESSION_TRADABLE
        new_entries_allowed = True
        force_close_required = False
        if time_until_end <= FORCE_CLOSE_MINUTES_BEFORE_SESSION_END:
            reason = FORCE_CLOSE_BEFORE_SESSION_END
            new_entries_allowed = False
            force_close_required = True
        elif time_until_end <= NEW_ENTRIES_CUTOFF_MINUTES_BEFORE_SESSION_END:
            reason = TOO_CLOSE_TO_SESSION_END
            new_entries_allowed = False

        return TradingSessionDecision(
            asset_class=asset_class,
            session_active=True,
            session_24_7=False,
            collect_snapshots=True,
            new_entries_allowed=new_entries_allowed,
            force_close_required=force_close_required,
            reason=reason,
            session_start_time=session_start,
            session_end_time=session_end,
            time_until_session_end_minutes=round(time_until_end, 4),
            session_key=asset_class.value + ':' + session_start.isoformat() + ':' + session_end.isoformat(),
        )

    def _active_session(
        self,
        sessions: tuple[TradingSessionWindow, ...],
        local_now: datetime,
    ) -> tuple[datetime, datetime] | None:
        for session in sessions:
            start_at, end_at = self._bounds_for_now(session, local_now)
            if start_at <= local_now < end_at:
                return start_at, end_at
        return None

    def _bounds_for_now(
        self,
        session: TradingSessionWindow,
        local_now: datetime,
    ) -> tuple[datetime, datetime]:
        start_at = datetime.combine(local_now.date(), session.start, tzinfo=self.timezone)
        end_day = local_now.date() + timedelta(days=1) if session.crosses_midnight else local_now.date()
        end_at = datetime.combine(end_day, session.end, tzinfo=self.timezone)
        if session.crosses_midnight and local_now.time() < session.end:
            start_at -= timedelta(days=1)
            end_at -= timedelta(days=1)
        return start_at, end_at

    def _to_local_time(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(self.timezone)


def trading_session_service_from_settings(settings: Settings) -> TradingSessionService:
    return TradingSessionService(
        configs={
            AssetClass.CRYPTO: AssetTradingSessionConfig(
                asset_class=AssetClass.CRYPTO,
                sessions=parse_trading_sessions(settings.trading_sessions_crypto),
            ),
            AssetClass.EQUITY_US: AssetTradingSessionConfig(
                asset_class=AssetClass.EQUITY_US,
                sessions=parse_trading_sessions(settings.trading_sessions_equity_us),
            ),
            AssetClass.EQUITY_EU: AssetTradingSessionConfig(
                asset_class=AssetClass.EQUITY_EU,
                sessions=parse_trading_sessions(settings.trading_sessions_equity_eu),
            ),
        },
        timezone_name=settings.trading_session_timezone,
    )


def parse_trading_sessions(raw_sessions: str) -> tuple[TradingSessionWindow, ...]:
    sessions: list[TradingSessionWindow] = []
    for raw_session in raw_sessions.split(','):
        value = raw_session.strip()
        if not value:
            continue
        raw_start, raw_end = value.split('-', maxsplit=1)
        sessions.append(
            TradingSessionWindow(
                start=_parse_time(raw_start.strip()),
                end=_parse_time(raw_end.strip()),
            )
        )
    return tuple(sessions)


def _parse_time(raw_value: str) -> time:
    hour, minute = raw_value.split(':', maxsplit=1)
    return time(hour=int(hour), minute=int(minute))
