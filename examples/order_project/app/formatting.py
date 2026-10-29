"""Status labels for the order presentation layer."""


def format_status(status: str) -> str:
    """Map pending/cancelled to stable labels; unknown strings map to Unknown."""
    return {"pending": "Pending", "cancelled": "Cancelled"}.get(status, "Unknown")
