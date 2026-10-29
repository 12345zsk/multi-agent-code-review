# Order Service

This small Python application quotes orders, retrieves tenant-scoped data,
builds summaries, and serves an existing CSV client. It uses only the standard
library. Importing the application does not open a database or write files.

The public contracts are:

- Monetary amounts are integer cents. Preferred customers receive the percentage
  configured in `settings.py`, with the final cents rounded half up per line.
- Pages begin at `settings.FIRST_PAGE`; invalid pages are rejected. The first
  page contains the first ordered rows. A page past the final row is empty.
- Page details require at most two SELECT statements, independent of page size.
- `contracts.OrderSummary` defines stable public keys. Empty summaries contain
  count 0, total_cents 0, and average_cents 0.0.
- Database connections and transactions belong to callers. Order IDs are literal
  user data. Every public database operation enforces the caller's tenant.
- Only pending orders in that tenant may be cancelled; other requests change
  nothing and return False.
- `clients/report_client.py` is an existing consumer whose output must stay stable.
- Status labels are Pending, Cancelled, and Unknown for other strings.

`seed.make_database()` explicitly creates isolated in-memory sample data. Existing
smoke tests live in `tests/`; their assertions are examples, not an exhaustive
specification or a measurement of coverage.

To run those existing tests from this directory:

```bash
python -B -m unittest discover -s tests -v
```
