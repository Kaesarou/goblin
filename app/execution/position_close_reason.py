from enum import StrEnum

POSITION_CLOSE_TAXONOMY_VERSION = "position_close_taxonomy_v2"


class PositionCloseReason(StrEnum):
    TAKE_PROFIT = "take_profit"
    INITIAL_STOP = "initial_stop"
    PROTECTED_BREAKEVEN = "protected_breakeven"
    PROTECTED_TRAILING = "protected_trailing"
    STALE_EXIT = "stale_exit"
    SESSION_FORCE_CLOSE = "session_force_close"
    MANUAL_OR_BROKER_CLOSE = "manual_or_broker_close"
    UNKNOWN_CONFIRMED_CLOSE = "unknown_confirmed_close"
