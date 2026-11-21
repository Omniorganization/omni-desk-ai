"""How long backups that hold personal data may be kept.

Backups copy the same personal data as the stores they back up, including
records that a data-subject erasure has since removed. Security's privacy
requirement (SECURITY.md) is that they are pruned within 35 days. Every
scheduled backup passes ``--retention-days`` (tests/test_backup_retention_policy.py);
a run by hand without it keeps every set, and says so.
"""

from __future__ import annotations

from typing import Optional

MAX_RETENTION_DAYS = 35


def retention_warning(retention_days: Optional[float]) -> Optional[str]:
    """A warning for a backup run that keeps sets longer than the limit, or None."""
    if not retention_days or retention_days <= 0:
        return (
            "warning: without --retention-days every backup is kept; backups that hold personal data "
            f"must be pruned within {MAX_RETENTION_DAYS} days, so pass it wherever this runs on a schedule"
        )
    if retention_days > MAX_RETENTION_DAYS:
        return f"warning: --retention-days {retention_days:g} keeps backups longer than the {MAX_RETENTION_DAYS} days allowed for personal data"
    return None
