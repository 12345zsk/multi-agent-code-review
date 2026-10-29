"""Order application services. Public schemas are declared in contracts.py."""

import sqlite3

from app import repository
from contracts import OrderSummary
from settings import FIRST_PAGE, DEFAULT_PAGE_SIZE


def average_order_amount(orders: list[dict]) -> float:
    """Average total_cents, returning 0.0 for no orders. Amounts are nonnegative ints."""
    if not orders:
        return 0.0
    total = sum(order["total_cents"] for order in orders)
    return total / len(orders)


def build_order_summary(orders: list[dict]) -> OrderSummary:
    """Return the public summary consumed by clients/report_client.py; preserve keys."""
    return {
        "count": len(orders),
        "total_cents": sum(order["total_cents"] for order in orders),
        "average_cents": average_order_amount(orders),
    }


def page_orders(
    connection: sqlite3.Connection, tenant_id: str,
    page: int = FIRST_PAGE, page_size: int = DEFAULT_PAGE_SIZE,
) -> list[dict]:
    """Follow settings.FIRST_PAGE; return [] beyond the last page.

    Reject page < FIRST_PAGE or nonpositive page_size. Include each order's items.
    Per the service contract, fetching a nonempty page takes at most two SELECTs,
    independent of page length. The caller owns the database connection.
    """
    if page < FIRST_PAGE or page_size < 1:
        raise ValueError("Invalid page or page size")
    start = (page - FIRST_PAGE) * page_size
    selected = repository.list_orders_page(connection, tenant_id, page_size, start)
    if not selected:
        return []
    items_by_order = repository.get_items_for_orders(connection, [row["id"] for row in selected])
    return [{**row, "items": items_by_order[row["id"]]} for row in selected]
