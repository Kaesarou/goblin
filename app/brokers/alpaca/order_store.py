from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from app.brokers.alpaca.environment import AlpacaEnvironment
from app.brokers.alpaca.schema import TERMINAL_STATUSES, number, order, text, timestamp


class AlpacaOrderStore:
    """Durable request identities and cumulative fills; no guessed lot allocation.

    A BUY client ID is a Goblin leg. Every SELL names that leg. Quantities are
    projected from the latest cumulative order fills, so duplicate REST/stream
    evidence cannot debit an inventory twice. Keep this DB with the V3 state DB.
    """

    def __init__(self, path: str, *, environment=AlpacaEnvironment.DEMO) -> None:
        self.environment = AlpacaEnvironment(environment)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS alpaca_meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS alpaca_orders (
                client_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                request TEXT NOT NULL, response TEXT
            )""")

    @contextmanager
    def _transaction(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def bind_account(self, account_id: str) -> None:
        identity = self.environment.account_namespace + ":" + text(account_id)
        with self._transaction() as db:
            old = db.execute("SELECT value FROM alpaca_meta WHERE key='account'").fetchone()
            if old and old["value"] != identity:
                raise ValueError("Alpaca journal belongs to another account")
            db.execute("INSERT OR IGNORE INTO alpaca_meta VALUES ('account', ?)", (identity,))

    def reserve(self, request: dict, *, position_id: str) -> None:
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM alpaca_meta WHERE key='account'").fetchone():
                raise ValueError("Alpaca journal account is not verified")
            # Serialize reservations across threads and processes. A previous
            # unknown submission must be resolved before another order for its leg.
            rows = db.execute(
                "SELECT * FROM alpaca_orders WHERE position_id=?", (position_id,)
            ).fetchall()
            if any(self._pending(dict(row)) for row in rows):
                raise ValueError("Unresolved Alpaca order already owns this leg")
            if request["side"] == "sell":
                remaining = Decimal(0)
                for row in rows:
                    if row["symbol"] != request["symbol"]:
                        raise ValueError("Alpaca reservation symbol does not match its leg")
                    response = json.loads(row["response"]) if row["response"] else None
                    filled = number(response["filled_qty"]) if response else Decimal(0)
                    remaining += filled if row["side"] == "buy" else -filled
                if number(request["qty"], positive=True) > remaining:
                    raise ValueError("Alpaca reservation exceeds remaining leg quantity")
            elif request["side"] != "buy" or request["client_order_id"] != position_id:
                raise ValueError("Invalid Alpaca opening leg identity")
            db.execute(
                "INSERT INTO alpaca_orders VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    request["client_order_id"],
                    position_id,
                    request["symbol"],
                    request["side"],
                    json.dumps(request, sort_keys=True),
                ),
            )

    def record_fault(self, reason: str) -> None:
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO alpaca_meta VALUES ('evidence_fault', ?)", (reason,))

    def check_health(self) -> None:
        with self._transaction() as db:
            fault = db.execute(
                "SELECT value FROM alpaca_meta WHERE key='evidence_fault'"
            ).fetchone()
            if fault:
                raise RuntimeError(
                    "Alpaca execution evidence requires manual reconciliation: " + fault["value"]
                )

    def observe(self, payload: dict) -> dict | None:
        payload = order(payload)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM alpaca_orders WHERE client_id=?", (payload["client_order_id"],)
            ).fetchone()
            if row is None:
                return None  # An external order is never assigned to a Goblin leg.
            if payload["symbol"] != row["symbol"] or payload["side"] != row["side"]:
                raise ValueError("Alpaca order identity conflicts with journal")
            request = json.loads(row["request"])
            qty = number(payload["filled_qty"])
            if row["side"] == "sell" and qty > number(request["qty"]):
                raise ValueError("Alpaca SELL filled more than the reserved leg quantity")
            old = json.loads(row["response"]) if row["response"] else None
            if old:
                if old["id"] != payload["id"]:
                    raise ValueError("Alpaca client order ID changed broker identity")
                if timestamp(payload["updated_at"]) < timestamp(old["updated_at"]):
                    return old
                old_qty = number(old["filled_qty"])
                if qty < old_qty:
                    raise ValueError("Alpaca cumulative fill quantity regressed")
                if old["status"] in TERMINAL_STATUSES:
                    if qty != old_qty or (
                        qty
                        and number(payload["filled_avg_price"]) != number(old["filled_avg_price"])
                    ):
                        raise ValueError("Alpaca terminal execution was revised")
                    return old
            db.execute(
                "UPDATE alpaca_orders SET response=? WHERE client_id=?",
                (json.dumps(payload, sort_keys=True), payload["client_order_id"]),
            )
            return payload

    def rows(self) -> list[dict]:
        with self._transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM alpaca_orders ORDER BY rowid")]

    def get(self, client_id: str) -> dict:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM alpaca_orders WHERE client_id=?", (client_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown Alpaca journal order identity")
            return dict(row)

    def pending(self) -> list[dict]:
        return [row for row in self.rows() if self._pending(row)]

    @staticmethod
    def _pending(row: dict) -> bool:
        return not row["response"] or json.loads(row["response"])["status"] not in TERMINAL_STATUSES

    def positions(self) -> dict[str, tuple[str, Decimal]]:
        return self.position_snapshot()[0]

    def position_snapshot(self, close_order_ids: dict[str, str] | None = None):
        """Project quantities and attributed fills from one journal snapshot."""
        legs: dict[str, tuple[str, Decimal]] = {}
        fills: dict[str, Decimal] = {}
        requested = close_order_ids or {}
        for row in self.rows():
            response = json.loads(row["response"]) if row["response"] else None
            qty = number(response["filled_qty"]) if response else Decimal(0)
            key = row["position_id"]
            if row["client_id"] in requested:
                if row["side"] != "sell" or key != requested[row["client_id"]]:
                    raise ValueError("Alpaca close identity does not own the requested leg")
                fills[row["client_id"]] = qty
            if row["side"] == "buy":
                legs[key] = row["symbol"], qty
            elif key in legs:
                symbol, previous = legs[key]
                legs[key] = symbol, previous - qty
            else:
                raise ValueError("Alpaca SELL has no opening leg")
        if any(qty < 0 for _, qty in legs.values()):
            raise ValueError("Alpaca journal has negative leg exposure")
        return legs, fills
