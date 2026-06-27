# BigSeller P0/P1 Review Checklist

- Verify webhook oversized payload returns 413.
- Verify TTL purge removes expired idempotency keys.
- Verify dead-letter metric is a current gauge.
- Verify BigSeller live evidence remains fail-closed until real evidence is present.
