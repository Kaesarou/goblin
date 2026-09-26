"""Read-only eToro account preflight for pending opening orders.

The portfolio endpoint proves current positions, not orders that might turn
into a position later. The documented P&L endpoint contains ordersForOpen
(pending market opens) and orders (pending MIT opens). An unknown response is
NOT an empty account. No broker mutation is performed here.
"""

from __future__ import annotations


def pending_open_order_descriptions(client) -> tuple[str, ...]:
    """Return descriptions of all pending opens; fail closed on incomplete data.

    ``client`` is the underlying ResilientEtoroClient, not its caching wrapper.
    Include manually submitted and copy/mirror orders: a reset requires an
    *entirely* flat account, so excluding an order based on mirrorID is unsafe.
    """
    if client.env == "demo":
        path = "/api/v1/trading/info/demo/pnl"
    elif client.env == "real":
        path = "/api/v1/trading/info/real/pnl"
    else:
        raise ValueError("Unsupported eToro account environment for preflight")

    payload = client._get(path)
    if not isinstance(payload, dict):
        raise ValueError("Invalid eToro P&L payload")
    if "ordersForOpen" not in payload or "orders" not in payload:
        raise ValueError("Missing authoritative pending-order collections in eToro P&L")

    pending: list[str] = []
    for field in ("ordersForOpen", "orders"):
        entries = payload.get(field)
        if not isinstance(entries, list) or not all(isinstance(x, dict) for x in entries):
            raise ValueError(f"Missing or invalid eToro P&L {field} collection")
        for index, order in enumerate(entries):
            identity = (
                str(order["orderId"])
                if order.get("orderId") is not None
                else f"index-{index}"
            )
            pending.append(f"{field}:{identity}")
    return tuple(pending)
