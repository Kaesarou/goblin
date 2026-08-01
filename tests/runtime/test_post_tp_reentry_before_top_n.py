from datetime import datetime, timedelta, timezone

from app.execution.candidate_selector import CandidateSelectionConfig, select_trade_candidates
from app.execution.position_close_reason import PositionCloseReason
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.persistence.trade_cooldown_store import TradeCooldownStore
from app.risk.trade_cooldown import ClosedTradeMemoryEntry
from app.risk.trade_cooldown_guard import TradeCooldownGuard
from app.strategies.signals import Signal


class FakeRiskManager:
    def risk_profile_for(self, symbol):
        return type('RiskProfile', (), {'trade_cooldown': type('Cfg', (), {'enabled': True})()})()

    def instrument_profile_for(self, symbol):
        return type('InstrumentProfile', (), {'asset_class': 'EQUITY_US'})()


def candidate(symbol: str, side: str, score: float, price: float) -> TradeCandidate:
    now = datetime.now(timezone.utc)
    candle = Candle(symbol, 60, price, price + 0.1, price - 0.1, price, None, now, now)
    snapshot = MarketSnapshot(symbol, price - 0.01, price + 0.01, price, now)
    return TradeCandidate(
        symbol=symbol,
        snapshot=snapshot,
        candle=candle,
        signal=Signal(action=side, setup_quality=0.8, reason='test', metadata={}),
        rank_reason='test',
    )


def save_consumed_sell(store: TradeCooldownStore, symbol: str) -> None:
    closed_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    store.save_or_replace(
        ClosedTradeMemoryEntry(
            symbol=symbol,
            side='SELL',
            close_reason=PositionCloseReason.TAKE_PROFIT,
            opened_at=closed_at - timedelta(minutes=5),
            closed_at=closed_at,
            cooldown_expires_at=closed_at - timedelta(minutes=5),
            position_id=f'{symbol}-1',
            pnl_entry_price=100.0,
            pnl_exit_price=99.0,
            take_profit=99.0,
            lowest_executable_price=98.8,
        )
    )


def test_post_tp_reentries_do_not_take_top_n_slots(tmp_path):
    store = TradeCooldownStore(str(tmp_path / 'goblin.sqlite'))
    save_consumed_sell(store, 'AMD')
    save_consumed_sell(store, 'AVGO')

    result = TradeCooldownGuard(store).filter_candidates(
        candidates=[
            candidate('AMD', 'SELL', 165.95, 98.85),
            candidate('AVGO', 'SELL', 154.16, 98.85),
            candidate('META', 'BUY', 130.0, 101.0),
            candidate('NVDA', 'BUY', 124.0, 101.0),
        ],
        risk_manager=FakeRiskManager(),
        now=datetime.now(timezone.utc),
    )
    selection = select_trade_candidates(
        result.selected_candidates,
        CandidateSelectionConfig(top_n=2),
    )

    assert [candidate.symbol for candidate in selection.selected_candidates] == ['META', 'NVDA']
    assert {rejected.candidate.symbol for rejected in result.rejected_candidates} == {'AMD', 'AVGO'}
