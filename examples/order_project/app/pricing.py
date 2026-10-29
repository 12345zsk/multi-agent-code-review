"""Integer-cent pricing for order lines."""


def discounted_total(unit_price_cents: int, quantity: int, discount_percent: int) -> int:
    """Apply a percentage discount, rounding the final cents half up.

    Inputs are integers. Price is nonnegative, quantity is positive, and the
    discount is in [0, 100]. For 1000 cents * 2 at 25%, return 1500 cents.
    """
    if unit_price_cents < 0 or quantity < 1 or not 0 <= discount_percent <= 100:
        raise ValueError("Invalid price, quantity or discount")
    gross = unit_price_cents * quantity
    return (gross * (100 - discount_percent) + 50) // 100
