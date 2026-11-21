"""Scheduled backups keep personal data at most 35 days.

Why: erasure does not rewrite backups (SECURITY.md). An erased subject leaves
the backups only when every set older than the erasure is purged, and the
privacy policy promises that within 35 days. The backup scripts default to
--retention-days 0, which keeps every set forever, and --keep counts sets
rather than days, so every scheduled run must pass --retention-days between 1
and 35 (the schedules use 30). A schedule that dumps Postgres directly
bypasses the scripts' purge, so it fails too. Only invocations count: a line
that names a script without passing it flags (a unit's Description=) is prose.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAX_RETENTION_DAYS = 35
# An invocation names the program and passes it at least one flag later on the line.
BACKUP_RUN = re.compile(r"\bbackup_(?:sqlite|postgres)\.py\b(?=.*?[\s\"',\[]--[a-z])")
RAW_DUMP = re.compile(r"\bpg_dump(?:all)?\b(?=.*?[\s\"',\[]--?[A-Za-z])")
RETENTION = re.compile(r"--retention-days(?:=|[\s\"',]+)(-?\d+(?:\.\d+)?)")


def violations(where: str, text: str) -> list[str]:
    """Problems in one schedule file. Comment lines do not count."""
    found: list[str] = []
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not BACKUP_RUN.search(line):
            if RAW_DUMP.search(line):
                found.append(f"{where}: schedules pg_dump directly, outside backup_postgres.py's purge: {line}")
            continue
        days = [float(value) for value in RETENTION.findall(line)]
        if not days:
            found.append(f"{where}: no --retention-days, so every backup set is kept forever: {line}")
        found += [f"{where}: --retention-days {value:g} is outside 1..{MAX_RETENTION_DAYS}: {line}"
                  for value in days if not 0 < value <= MAX_RETENTION_DAYS]
    return found


def _is_scheduled(workflow: Path) -> bool:
    data = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
    triggers = data.get("on", data.get(True)) or {}  # YAML 1.1 reads a bare `on` as True
    return isinstance(triggers, dict) and "schedule" in triggers


def schedule_files() -> list[Path]:
    """Where backups get scheduled: deployment assets and scheduled workflows."""
    files = sorted(path for path in (ROOT / "deploy").rglob("*") if path.is_file() and path.suffix != ".md")
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    return files + [path for path in workflows if _is_scheduled(path)]


def test_every_scheduled_backup_keeps_sets_at_most_35_days() -> None:
    problems: list[str] = []
    runs = 0
    for path in schedule_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        runs += len(BACKUP_RUN.findall(text))
        problems += violations(str(path.relative_to(ROOT)), text)
    # The systemd backup unit and the backup/restore drill schedule backups today.
    assert runs >= 2, "the scan no longer sees the scheduled backups"
    assert not problems, "\n".join(problems)


def test_docs_never_recommend_keeping_backups_longer_than_35_days() -> None:
    docs = [ROOT / "SECURITY.md", *(ROOT / "docs").rglob("*.md"), *(ROOT / "deploy").rglob("*.md")]
    too_long = [f"{path.relative_to(ROOT)}: --retention-days {value}"
                for path in docs if path.is_file()
                for value in RETENTION.findall(path.read_text(encoding="utf-8"))
                if not 0 < float(value) <= MAX_RETENTION_DAYS]
    assert not too_long, too_long


def test_the_check_rejects_every_way_of_keeping_backups_too_long() -> None:
    assert violations("unit", "ExecStart=/opt/venv/bin/python backup_sqlite.py --dest /b --retention-days 35 a.sqlite3") == []
    assert violations("cronjob", 'args: ["backup_postgres.py", "--encrypt", "--retention-days", "30"]') == []
    assert violations("unit", "# backup_postgres.py runs nightly from the timer") == []
    assert violations("unit", "Description=OmniDesk PostgreSQL backup (backup_postgres.py, pg_dump)") == []
    for bad in (
        "ExecStart=python backup_sqlite.py --dest /b a.sqlite3",  # script default 0: forever
        "python backup_postgres.py --keep 14",  # a count of sets, not an age
        "python backup_postgres.py --retention-days 0",
        "python backup_postgres.py --retention-days 36",
        "python backup_postgres.py --retention-days=90",
        "python backup_postgres.py --encrypt \\\n  --retention-days 40",
        "pg_dump --format=custom --file /backups/omnidesk.dump",
        "pg_dump -Fc -f /backups/omnidesk.dump",
    ):
        assert violations("schedule", bad), bad
