"""Stable client-facing schemas; consumers rely on these exact field names."""

from typing import TypedDict


class OrderSummary(TypedDict):
    count: int
    total_cents: int
    average_cents: float
