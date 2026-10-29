"""Independent acceptance checks, kept outside the agent's order_project boundary.

By default these checks run in a disposable file copy, with in-memory SQLite.
This is a manual acceptance command, not a tool executed by the review workflow.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = None


class OrderAcceptanceTests(unittest.TestCase):
    def database(self):
        from seed import make_database
        connection = make_database()
        self.addCleanup(connection.close)
        return connection

    def test_percentage_discount(self):
        from app.checkout import quote_order
        from app.pricing import discounted_total
        self.assertEqual(discounted_total(1000, 2, 25), 1500)
        self.assertEqual(discounted_total(101, 1, 50), 51)
        self.assertEqual(discounted_total(1000, 2, 100), 0)
        self.assertEqual(quote_order([{"unit_price_cents": 1000, "quantity": 2}], True), 1500)

    def test_empty_average(self):
        from app.service import average_order_amount
        self.assertEqual(average_order_amount([]), 0.0)
        self.assertEqual(average_order_amount([{"total_cents": 2000}, {"total_cents": 3000}]), 2500.0)

    def test_one_based_pagination(self):
        from app.service import page_orders
        connection = self.database()
        self.assertEqual([row["id"] for row in page_orders(connection, "alpha", 1, 2)], ["A1", "A2"])
        self.assertEqual([row["id"] for row in page_orders(connection, "alpha", 2, 2)], ["A3", "A4"])
        self.assertEqual(page_orders(connection, "alpha", 3, 2), [])

    def test_summary_schema_and_client(self):
        from app.service import build_order_summary
        from clients.report_client import export_orders
        orders = [{"total_cents": 2000}, {"total_cents": 3000}]
        self.assertEqual(build_order_summary(orders), {"count": 2, "total_cents": 5000, "average_cents": 2500.0})
        self.assertEqual(export_orders(orders), "2,5000")
        self.assertEqual(export_orders([]), "0,0")

    def test_literal_order_id(self):
        from app.repository import get_order
        connection = self.database()
        self.assertIsNone(get_order(connection, "alpha", "missing' OR '1'='1"))
        self.assertEqual(get_order(connection, "quoted", "O'quote")["id"], "O'quote")
        self.assertIsNone(get_order(connection, "alpha", "B1"))

    def test_tenant_authorized_cancellation(self):
        from app.repository import cancel_order
        connection = self.database()
        self.assertFalse(cancel_order(connection, "alpha", "B1"))
        self.assertEqual(connection.execute("SELECT status FROM orders WHERE id = 'B1'").fetchone()[0], "pending")
        self.assertTrue(cancel_order(connection, "alpha", "A1"))
        self.assertFalse(cancel_order(connection, "alpha", "A1"))
        self.assertFalse(cancel_order(connection, "alpha", "missing"))

    def test_bounded_page_queries(self):
        from app.service import page_orders
        connection = self.database()
        statements = []
        connection.set_trace_callback(statements.append)
        rows = page_orders(connection, "alpha", 1, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["items"] for row in rows))
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(selects), 2, "Page details must use a batched item query")

    def test_status_module_syntax_and_behavior(self):
        source = (PROJECT/"app/formatting.py").read_text(encoding="utf-8")
        try:
            compile(source, "app/formatting.py", "exec")
        except SyntaxError as exc:
            self.fail(f"Status module has invalid syntax at line {exc.lineno}")
        from app.formatting import format_status
        self.assertEqual(format_status("pending"), "Pending")
        self.assertEqual(format_status("cancelled"), "Cancelled")
        self.assertEqual(format_status("other"), "Unknown")

    def test_normal_behavior_is_preserved(self):
        from app.checkout import quote_order
        from app.pricing import discounted_total
        from app.repository import get_items_for_orders, get_order, list_orders
        from app.service import page_orders
        self.assertEqual(quote_order([]), 0)
        self.assertEqual(discounted_total(1000, 2, 0), 2000)
        for args in ((-1, 1, 0), (1, 0, 0), (1, 1, 101)):
            with self.assertRaises(ValueError):
                discounted_total(*args)
        connection = self.database()
        self.assertEqual(get_order(connection, "alpha", "A1")["id"], "A1")
        self.assertEqual([row["id"] for row in list_orders(connection, "alpha")], ["A1", "A2", "A3", "A4"])
        self.assertEqual(get_items_for_orders(connection, []), {})
        with self.assertRaises(ValueError):
            page_orders(connection, "alpha", 0, 2)
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="examples/order_project", help="Order-service project to verify")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(args.project).resolve(strict=True)
    if not root.is_dir() or not (root/"app/service.py").is_file():
        parser.error("--project must point to the order-service sample")
    if not args.worker:
        with tempfile.TemporaryDirectory(prefix="order-acceptance-") as directory:
            copied = Path(directory)/"project"
            shutil.copytree(root, copied, ignore=shutil.ignore_patterns("__pycache__", "generated_tests", "output", ".venv"))
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--project", str(copied), "--worker"],
                cwd=copied, timeout=60,
            )
            raise SystemExit(result.returncode)
    global PROJECT
    PROJECT = root
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OrderAcceptanceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
