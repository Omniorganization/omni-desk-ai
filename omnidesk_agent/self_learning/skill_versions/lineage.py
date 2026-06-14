from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.storage.migrations import Migration, apply_migrations
from omnidesk_agent.storage.sqlite import connect_sqlite


@dataclass(frozen=True)
class SkillVersion:
    skill_name: str
    version: str
    parent_version: Optional[str]
    status: str
    artifact_hash: str
    created_at: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SkillLineageStore:
    """Tracks Skill -> V2 -> V3 lineage and retirement state."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_versions (
                  skill_name TEXT NOT NULL,
                  version TEXT NOT NULL,
                  parent_version TEXT,
                  status TEXT NOT NULL,
                  artifact_hash TEXT NOT NULL,
                  notes TEXT,
                  created_at REAL NOT NULL,
                  PRIMARY KEY(skill_name, version)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_benchmarks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  skill_name TEXT NOT NULL,
                  version TEXT NOT NULL,
                  metric TEXT NOT NULL,
                  value REAL NOT NULL,
                  created_at REAL NOT NULL
                )
                """
            )
            apply_migrations(con, [Migration(1, "skill_lineage_schema_baseline", lambda _con: None)])

    def register_version(
        self,
        skill_name: str,
        version: str,
        *,
        artifact_hash: str,
        parent_version: Optional[str] = None,
        status: str = "candidate",
        notes: str = "",
    ) -> SkillVersion:
        record = SkillVersion(skill_name, version, parent_version, status, artifact_hash, time.time(), notes)
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO skill_versions(skill_name, version, parent_version, status, artifact_hash, notes, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (skill_name, version, parent_version, status, artifact_hash, notes, record.created_at),
            )
        return record

    def retire_version(self, skill_name: str, version: str, *, reason: str = "") -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE skill_versions SET status=?, notes=? WHERE skill_name=? AND version=?",
                ("retired", reason, skill_name, version),
            )

    def lineage(self, skill_name: str) -> list[SkillVersion]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM skill_versions WHERE skill_name=? ORDER BY created_at ASC", (skill_name,)).fetchall()
        return [
            SkillVersion(
                row["skill_name"], row["version"], row["parent_version"], row["status"], row["artifact_hash"], row["created_at"], row["notes"] or ""
            )
            for row in rows
        ]

    def record_benchmark(self, skill_name: str, version: str, metric: str, value: float) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO skill_benchmarks(skill_name, version, metric, value, created_at) VALUES(?,?,?,?,?)",
                (skill_name, version, metric, float(value), time.time()),
            )

    def latest_metric(self, skill_name: str, version: str, metric: str) -> Optional[float]:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT value FROM skill_benchmarks
                WHERE skill_name=? AND version=? AND metric=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (skill_name, version, metric),
            ).fetchone()
        return float(row["value"]) if row else None
