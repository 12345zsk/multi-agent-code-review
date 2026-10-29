"""Small existing smoke suite, run explicitly from the application root."""

import unittest

from app.checkout import quote_order
from app.repository import get_order, list_orders
from seed import make_database


class SmokeTests(unittest.TestCase):
    def test_regular_quote_does_not_mutate_lines(self):
        lines = [{"unit_price_cents": 1000, "quantity": 2}]
        self.assertEqual(quote_order(lines), 2000)
        self.assertEqual(lines, [{"unit_price_cents": 1000, "quantity": 2}])

    def test_plain_order_lookup_and_connection_lifetime(self):
        connection = make_database()
        self.addCleanup(connection.close)
        self.assertEqual(get_order(connection, "alpha", "A1")["id"], "A1")
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_tenant_list_is_filtered_and_sorted(self):
        connection = make_database()
        self.addCleanup(connection.close)
        self.assertEqual([row["id"] for row in list_orders(connection, "alpha")], ["A1", "A2", "A3", "A4"])


if __name__ == "__main__":
    unittest.main()
