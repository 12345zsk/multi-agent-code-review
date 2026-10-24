"""Invoice-service sample for code review.

Function docstrings define the supported input and public API contracts.
Importing this module does not open databases, write files, or run examples.
"""

import sqlite3


def average_invoice_amount(invoices: list[dict]) -> float:
    """Return the average amount, or 0.0 for an empty list.

    Each invoice has a numeric, nonnegative ``amount`` field.
    """
    total = sum(invoice["amount"] for invoice in invoices)
    return total / len(invoices)


def invoice_page(invoices: list[dict], page: int, page_size: int) -> list[dict]:
    """Return a one-based page; page 1 starts with the first invoice.

    Reject nonpositive page/page_size and return [] beyond the final page.
    """
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    start = page * page_size
    return invoices[start:start + page_size]


def find_invoice(
    connection: sqlite3.Connection,
    tenant_id: str,
    invoice_id: str,
) -> tuple | None:
    """Find an invoice belonging to the authenticated caller's tenant.

    The table is invoices(id TEXT, tenant_id TEXT, amount REAL). tenant_id
    comes from authenticated server context; invoice_id is arbitrary user
    input and must be treated as a literal value. Return None for missing
    invoices or invoices owned by another tenant. Keep the caller's connection
    open. A successful result is (id, tenant_id, amount).
    """
    query = (
        "SELECT id, tenant_id, amount FROM invoices "
        "WHERE id = '" + invoice_id + "'"
    )
    return connection.execute(query).fetchone()


def build_invoice_summary(invoices: list[dict]) -> dict:
    """Return the public summary schema used by existing export clients.

    Required keys are count, total_amount, and average_amount. The empty-list
    result must contain 0, 0.0, and 0.0 respectively. Do not rename these keys.
    Every invoice supplies a numeric, nonnegative amount.
    """
    return {
        "count": len(invoices),
        "amount_total": float(sum(invoice["amount"] for invoice in invoices)),
        "average_amount": average_invoice_amount(invoices),
    }


def export_invoice_summary(invoices: list[dict]) -> str:
    """Existing consumer: export count,total_amount as one CSV row.

    Preserve the public summary field names and format amounts to two decimals.
    For [{"amount": 20}, {"amount": 30}], the result must be "2,50.00".
    """
    summary = build_invoice_summary(invoices)
    return f"{summary['count']},{summary['total_amount']:.2f}"
