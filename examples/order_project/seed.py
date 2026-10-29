"""Explicit in-memory fixtures; importing this module performs no database I/O."""

import sqlite3


def make_database() -> sqlite3.Connection:
    """Return an isolated in-memory connection with deterministic, non-sensitive sample data."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE orders (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            total_cents INTEGER NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE order_items (order_id TEXT NOT NULL, sku TEXT NOT NULL, quantity INTEGER NOT NULL);
    """)
    connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", [
        ("A1", "alpha", 2000, "pending"), ("A2", "alpha", 3000, "pending"),
        ("A3", "alpha", 4000, "pending"), ("A4", "alpha", 5000, "pending"),
        ("B1", "beta", 9000, "pending"), ("O'quote", "quoted", 700, "pending"),
    ])
    connection.executemany("INSERT INTO order_items VALUES (?, ?, ?)", [
        ("A1", "sku-1", 1), ("A2", "sku-2", 2), ("A3", "sku-3", 3),
        ("A4", "sku-4", 4), ("B1", "sku-private", 1),
    ])
    connection.commit()
    return connection
