"""Compute totals without touching a database or mutating caller input."""

from app.pricing import discounted_total
from settings import PREFERRED_DISCOUNT_PERCENT


def quote_order(lines: list[dict], preferred_customer: bool = False) -> int:
    """Total valid unit_price_cents/quantity lines; preferred customers get the configured percent."""
    discount = PREFERRED_DISCOUNT_PERCENT if preferred_customer else 0
    return sum(discounted_total(line["unit_price_cents"], line["quantity"], discount) for line in lines)
