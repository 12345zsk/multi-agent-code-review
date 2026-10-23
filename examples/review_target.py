"""Order-service review fixture. Contracts are documented on each function.

This file intentionally contains defects for agent evaluation. Do not use it in
production. Importing the module performs no I/O and executes no example calls.
"""

import sqlite3


def average_order_amount(orders: list[dict]) -> float:
    """Return the average amount, or 0.0 when orders is empty."""
    total = sum(order["amount"] for order in orders)
    return total / len(orders)


def get_order_page(orders: list[dict], page: int, page_size: int) -> list[dict]:
    """Use one-based pages: page 1 starts with the first order.

    Reject nonpositive page/page_size. Return an empty list past the final page.
    """
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    start = page * page_size
    return orders[start:start + page_size]


def find_order(connection: sqlite3.Connection, order_id: str) -> tuple | None:
    """Find the exact supplied order ID, treating the ID as data, not SQL.

    The caller supplies an open connection with an orders(id, owner_id, amount)
    table. Return None when no ID matches. Keep the connection open for callers.
    """
    query = "SELECT id, owner_id, amount FROM orders WHERE id = " + order_id
    return connection.execute(query).fetchone()


def cancel_order(order: dict, acting_user_id: int) -> dict:
    """Only the owner may cancel a pending order.

    The order supplies owner_id and status; acting_user_id must match owner_id.
    Raise PermissionError for a different user and ValueError for a nonpending
    order. Return an updated copy without mutating the caller's order.
    """
    if order["status"] != "pending":
        raise ValueError("Only pending orders can be cancelled")
    updated = dict(order)
    updated["status"] = "cancelled"
    return updated


def add_order_tag(order_id: int, tag: str, tags_by_order: dict = {}) -> dict:
    """Add a tag, mutating an explicitly supplied store and returning it.

    When no store is supplied, each call must create an independent store.
    Do not insert the same tag twice for an order.
    """
    tags = tags_by_order.setdefault(order_id, [])
    if tag not in tags:
        tags.append(tag)
    return tags_by_order
