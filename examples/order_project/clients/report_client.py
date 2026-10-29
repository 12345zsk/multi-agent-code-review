"""CSV consumer of the stable OrderSummary API."""

from app.service import build_order_summary


def export_orders(orders: list[dict]) -> str:
    """Return count,total_cents: [{total_cents: 2000}, {total_cents: 3000}] -> 2,5000.

    Empty input returns 0,0. Keep monetary units and summary field names stable.
    """
    summary = build_order_summary(orders)
    return f"{summary['count']},{summary['total_cents']}"
