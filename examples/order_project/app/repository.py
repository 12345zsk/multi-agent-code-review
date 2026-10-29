"""SQLite access; the caller owns the connection and transaction.

Never close or commit the supplied connection. Tenant IDs come from trusted
server context. Order IDs are arbitrary user strings and must remain data.
"""

import sqlite3


def get_order(connection: sqlite3.Connection, tenant_id: str, order_id: str) -> dict | None:
    """Return an exact order in this tenant, or None. Return named columns."""
    row = connection.execute(
        "SELECT id, tenant_id, total_cents, status FROM orders WHERE tenant_id = ? AND id = ?",
        (tenant_id, order_id),
    ).fetchone()
    return dict(row) if row is not None else None


def cancel_order(connection: sqlite3.Connection, tenant_id: str, order_id: str) -> bool:
    """Cancel this tenant's pending order; otherwise return False, changing nothing."""
    result = connection.execute(
        "UPDATE orders SET status = 'cancelled' WHERE id = ? AND tenant_id = ? AND status = 'pending'",
        (order_id, tenant_id),
    )
    return result.rowcount == 1


def list_orders(connection: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """Return this tenant's orders sorted by ID; keep transaction control outside."""
    rows = connection.execute(
        "SELECT id, tenant_id, total_cents, status FROM orders WHERE tenant_id = ? ORDER BY id",
        (tenant_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_orders_page(connection: sqlite3.Connection, tenant_id: str, limit: int, offset: int) -> list[dict]:
    """Return one page of this tenant's orders sorted by ID; keep transaction control outside."""
    rows = connection.execute(
        "SELECT id, tenant_id, total_cents, status FROM orders WHERE tenant_id = ? ORDER BY id LIMIT ? OFFSET ?",
        (tenant_id, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def get_items(connection: sqlite3.Connection, order_id: str) -> list[dict]:
    """Internal helper; order_id must already have passed tenant authorization."""
    rows = connection.execute(
        "SELECT sku, quantity FROM order_items WHERE order_id = ? ORDER BY sku", (order_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_items_for_orders(connection: sqlite3.Connection, order_ids: list[str]) -> dict[str, list[dict]]:
    """Load items for already-authorized order IDs in one query; [] performs no query."""
    grouped = {order_id: [] for order_id in order_ids}
    if not order_ids:
        return grouped
    placeholders = ",".join("?" for _ in order_ids)
    rows = connection.execute(
        f"SELECT order_id, sku, quantity FROM order_items WHERE order_id IN ({placeholders}) ORDER BY order_id, sku",
        order_ids,
    ).fetchall()
    for row in rows:
        grouped[row["order_id"]].append({"sku": row["sku"], "quantity": row["quantity"]})
    return grouped
