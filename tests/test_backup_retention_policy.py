"""Scheduled backups keep personal data for at most 35 days.

Backups hold the same personal data as the stores they copy, including records
a data-subject erasure has since removed. Security's privacy requirement
(SECURITY.md, #270) is that they are kept at most 35 days. The scheduled
backup unit is the default every systemd deployment gets, so it must prune,
and never later than that. Operators may lower the value, never raise it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "omnidesk-backup.service"
MAX_RETENTION_DAYS = 35


def _retention_days(unit_text: str) -> float:
    [command] = [line for line in unit_text.splitlines() if line.startswith("ExecStart=")]
    match = re.search(r"--retention-days[ =](\S+)", command)
    assert match, "the scheduled backup must prune old backups (--retention-days)"
    return float(match.group(1))


def test_the_scheduled_backup_prunes_within_the_privacy_limit() -> None:
    days = _retention_days(UNIT.read_text(encoding="utf-8"))
    # 0 would keep every backup forever.
    assert 0 < days <= MAX_RETENTION_DAYS


def test_the_check_fails_for_a_unit_that_keeps_backups_longer() -> None:
    for command, kept in (("--retention-days 36", 36.0), ("--retention-days=90", 90.0), ("--retention-days 0", 0.0)):
        days = _retention_days(f"ExecStart=/opt/omnidesk/venv/bin/python backup_sqlite.py --encrypt {command}\n")
        assert days == kept and not 0 < days <= MAX_RETENTION_DAYS
