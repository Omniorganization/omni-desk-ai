"""Every scheduled backup keeps personal data for at most 35 days.

Backups hold the same personal data as the stores they copy, including records
a data-subject erasure has since removed. Security's privacy requirement
(SECURITY.md, #270) is that they are kept at most 35 days. These tests read
every scheduled path in deploy/ (systemd units and timers, Helm charts,
compose files, launchd plists, cron and shell scripts). Each command that runs
a backup script must prune, and never later than that; operators may lower the
value, never raise it. Run by hand, the scripts keep every set and say so.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnidesk_agent.storage.backup_policy import MAX_RETENTION_DAYS, retention_warning
from scripts import backup_postgres, backup_sqlite

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
BACKUP_SCRIPTS = ("backup_sqlite.py", "backup_postgres.py")
SCHEDULE_SUFFIXES = {".service", ".timer", ".yaml", ".yml", ".tpl", ".plist", ".sh", ".cron", ".json"}
RETENTION = re.compile(r"--retention-days[ =]+(\{\{.*?\}\}|\S+)")


def _commands(text: str) -> list[str]:
    """Each command in ``text`` that runs a backup script, as one line.

    A ``Key=...`` line (systemd) or shell line is one command, continuations
    joined. A YAML list continues with its sibling ``- item`` lines.
    """
    lines = text.replace("\\\n", " ").splitlines()
    commands: list[str] = []
    for index, line in enumerate(lines):
        if not any(script in line for script in BACKUP_SCRIPTS):
            continue
        if line.strip().startswith("- "):
            indent = len(line) - len(line.lstrip())
            items = [line]
            for follower in lines[index + 1:]:
                if len(follower) - len(follower.lstrip()) != indent or not follower.strip().startswith("- "):
                    break
                items.append(follower)
            line = " ".join(item.strip()[2:].strip().strip("\"'") for item in items)
        commands.append(line)
    return commands


def _chart_value(path: Path, expression: str) -> Any:
    """``{{ .Values.a.b }}`` resolved from the chart's values.yaml."""
    match = re.fullmatch(r"\{\{-?\s*\.Values\.([A-Za-z0-9_.]+)(?:\s*\|\s*quote)?\s*-?\}\}", expression)
    chart = next((parent for parent in path.parents if (parent / "Chart.yaml").is_file()), None)
    assert match and chart, f"{path}: --retention-days must be a number or a chart value, not {expression}"
    value: Any = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    for key in match.group(1).split("."):
        value = value[key]
    return value


def _retention_days(path: Path, command: str) -> float:
    match = RETENTION.search(command)
    assert match, f"{path}: a scheduled backup must prune old backups (--retention-days): {command.strip()}"
    raw = match.group(1).strip("\"'")
    return float(_chart_value(path, raw) if raw.startswith("{{") else raw)


def _scheduled_backups() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in sorted(DEPLOY.rglob("*")):
        if path.is_file() and (path.suffix in SCHEDULE_SUFFIXES or path.name == "crontab"):
            found += [(path, command) for command in _commands(path.read_text(encoding="utf-8", errors="replace"))]
    return found


def test_every_scheduled_backup_prunes_within_the_privacy_limit() -> None:
    scheduled = _scheduled_backups()
    # The search must find what is known to be scheduled, or it proves nothing.
    assert {DEPLOY / "systemd" / "omnidesk-backup.service", DEPLOY / "systemd" / "omnidesk-postgres-backup.service"} <= {path for path, _ in scheduled}
    for path, command in scheduled:
        days = _retention_days(path, command)
        # 0 would keep every backup forever.
        assert 0 < days <= MAX_RETENTION_DAYS, f"{path}: --retention-days {days:g}"


def test_every_backup_timer_runs_a_service_that_prunes() -> None:
    timers = sorted(DEPLOY.rglob("*.timer"))
    backup_timers = [timer for timer in timers if "backup" in timer.name]
    assert {timer.name for timer in backup_timers} >= {"omnidesk-backup.timer", "omnidesk-postgres-backup.timer"}
    for timer in timers:
        unit = re.search(r"^Unit=(\S+)", timer.read_text(encoding="utf-8"), re.MULTILINE)
        service = timer.with_name(unit.group(1) if unit else timer.stem + ".service")
        if "backup" not in timer.name and "backup" not in service.name:
            continue
        assert service.is_file(), f"{timer}: runs {service.name}, which does not exist"
        [command] = _commands(service.read_text(encoding="utf-8"))
        assert 0 < _retention_days(service, command) <= MAX_RETENTION_DAYS


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("ExecStart=python backup_sqlite.py --encrypt --retention-days 36", False),
        ("ExecStart=python backup_sqlite.py --retention-days=90", False),
        ("ExecStart=python backup_sqlite.py --retention-days 0", False),
        ("ExecStart=python backup_postgres.py --dest /b \\\n  --retention-days 30", True),
        ("command:\n  - python\n  - /opt/omnidesk/scripts/backup_postgres.py\n  - --retention-days\n  - \"14\"\n", True),
        ("command:\n  - python\n  - /opt/omnidesk/scripts/backup_postgres.py\n  - --dest\n  - /backups\n", None),
    ],
)
def test_the_check_reads_each_form_of_a_scheduled_command(command: str, allowed: bool | None) -> None:
    [found] = _commands(command)
    if allowed is None:
        with pytest.raises(AssertionError, match="must prune"):
            _retention_days(Path("unit"), found)
        return
    assert (0 < _retention_days(Path("unit"), found) <= MAX_RETENTION_DAYS) is allowed


def test_a_run_by_hand_without_retention_says_backups_must_be_pruned(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from contextlib import closing

    db = tmp_path / "runtime.sqlite3"
    with closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE t(x)")
    out = tmp_path / "out"
    out.mkdir()
    assert backup_sqlite.main(["--dest", str(out), str(db)]) == 0
    assert "must be pruned within 35 days" in capsys.readouterr().err
    assert backup_sqlite.main(["--dest", str(out), "--retention-days", "30", str(db)]) == 0
    assert capsys.readouterr().err == ""
    # The PostgreSQL script warns before it looks for a database.
    monkeypatch.delenv("OMNIDESK_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNIDESK_APPSYNC_POSTGRES_DSN", raising=False)
    backup_postgres.main(["--dest", str(tmp_path / "pg"), "--retention-days", "90"])
    assert "longer than the 35 days" in capsys.readouterr().err
    assert retention_warning(30) is None and retention_warning(35) is None and retention_warning(None) and retention_warning(0)


def test_a_chart_value_is_read_from_the_charts_values(tmp_path: Path) -> None:
    # Operations' Helm CronJob will take the value from values.yaml.
    chart = tmp_path / "chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("name: omnidesk\n", encoding="utf-8")
    template = chart / "templates" / "backup-cronjob.yaml"
    template.write_text("command:\n  - /opt/omnidesk/scripts/backup_postgres.py\n  - --retention-days\n  - \"{{ .Values.backup.retentionDays }}\"\n", encoding="utf-8")
    for days, allowed in ((30, True), (60, False)):
        (chart / "values.yaml").write_text(f"backup:\n  retentionDays: {days}\n", encoding="utf-8")
        [command] = _commands(template.read_text(encoding="utf-8"))
        assert (0 < _retention_days(template, command) <= MAX_RETENTION_DAYS) is allowed
