from __future__ import annotations

import json
import sqlite3
import time
import weakref
from pathlib import Path
from typing import Any, Optional
from omnidesk_agent.config import MemoryPrivacyConfig
from omnidesk_agent.privacy.encryption import EncryptionProvider
from omnidesk_agent.privacy.redaction import MemoryPrivacyFilter
from omnidesk_agent.memory.governed_writer import GovernedMemoryWriter
from omnidesk_agent.storage.migrations import Migration, apply_migrations
from omnidesk_agent.storage.sqlite import connect_sqlite


class ExperienceStore:
    """SQLite-backed experience memory.

    It keeps the original task log table for compatibility, and adds a structured
    `structured_experiences` table that is actually retrievable by planner and
    learning jobs.
    """

    SENSITIVE_LEGACY_FIELDS = ["task", "plan", "outcome", "tags"]
    SENSITIVE_STRUCTURED_FIELDS = ["goal", "failure_reason", "solution_attempted", "recommended_next_action", "human_feedback", "tags", "raw_trace"]

    def __init__(self, db_path: Path, privacy_config: Optional[MemoryPrivacyConfig] = None, encryption: Optional[EncryptionProvider] = None):
        self.db_path = db_path.expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.privacy = MemoryPrivacyFilter()
        self.privacy_config = privacy_config or MemoryPrivacyConfig()
        if encryption is not None:
            self.encryption = encryption
        elif self.privacy_config.encrypt_at_rest:
            self.encryption = EncryptionProvider.from_env(
                self.privacy_config.encryption_key_env,
                required=True,
                key_id=self.privacy_config.encryption_key_id,
            )
        else:
            self.encryption = EncryptionProvider.disabled()
        self.governed_writer = GovernedMemoryWriter()
        self.conn = connect_sqlite(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._closed = False
        self._finalizer = weakref.finalize(self, self.conn.close)
        self._init()

    def close(self) -> None:
        if not getattr(self, "_closed", False):
            finalizer = getattr(self, "_finalizer", None)
            if finalizer is not None and finalizer.alive:
                finalizer()
            else:
                self.conn.close()
            self._closed = True

    def __enter__(self) -> "ExperienceStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort resource cleanup
        try:
            self.close()
        except Exception:
            pass

    def _init(self) -> None:
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS experiences (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at REAL NOT NULL,
          task TEXT NOT NULL,
          plan TEXT,
          outcome TEXT,
          tags TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS structured_experiences (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          task_type TEXT NOT NULL,
          goal TEXT NOT NULL,
          success INTEGER NOT NULL,
          failure_reason TEXT,
          solution_attempted TEXT,
          recommended_next_action TEXT,
          risk_level TEXT NOT NULL,
          reusable_skill INTEGER NOT NULL DEFAULT 0,
          tool_cost REAL DEFAULT 0,
          success_score REAL DEFAULT 0,
          human_feedback TEXT,
          last_used_at REAL,
          privacy_level TEXT DEFAULT 'normal',
          expires_at REAL,
          memory_status TEXT NOT NULL DEFAULT 'candidate',
          confidence REAL NOT NULL DEFAULT 0.5,
          validation_count INTEGER NOT NULL DEFAULT 0,
          negative_example_count INTEGER NOT NULL DEFAULT 0,
          last_reviewed_at REAL,
          promotion_reason TEXT,
          tags TEXT,
          raw_trace TEXT
        )
        """)
        for name, ddl in {
            "memory_status": "ALTER TABLE structured_experiences ADD COLUMN memory_status TEXT NOT NULL DEFAULT 'candidate'",
            "confidence": "ALTER TABLE structured_experiences ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5",
            "validation_count": "ALTER TABLE structured_experiences ADD COLUMN validation_count INTEGER NOT NULL DEFAULT 0",
            "negative_example_count": "ALTER TABLE structured_experiences ADD COLUMN negative_example_count INTEGER NOT NULL DEFAULT 0",
            "last_reviewed_at": "ALTER TABLE structured_experiences ADD COLUMN last_reviewed_at REAL",
            "promotion_reason": "ALTER TABLE structured_experiences ADD COLUMN promotion_reason TEXT",
        }.items():
            self._ensure_column("structured_experiences", name, ddl)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS learning_metrics (
          day TEXT PRIMARY KEY,
          task_count INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0,
          failure_count INTEGER NOT NULL DEFAULT 0,
          manual_intervention_count INTEGER NOT NULL DEFAULT 0,
          tool_error_count INTEGER NOT NULL DEFAULT 0,
          repeat_failure_count INTEGER NOT NULL DEFAULT 0,
          skill_reuse_count INTEGER NOT NULL DEFAULT 0,
          rollback_count INTEGER NOT NULL DEFAULT 0,
          security_violation_count INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL
        )
        """)
        try:
            cur.execute("CREATE VIRTUAL TABLE IF NOT EXISTS experiences_fts USING fts5(task, plan, outcome, tags, content='experiences', content_rowid='id')")
            cur.execute("""
            CREATE TRIGGER IF NOT EXISTS experiences_ai AFTER INSERT ON experiences BEGIN
              INSERT INTO experiences_fts(rowid, task, plan, outcome, tags) VALUES (new.id, new.task, new.plan, new.outcome, new.tags);
            END;
            """)
            cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS structured_experiences_fts
            USING fts5(task_type, goal, failure_reason, recommended_next_action, tags, content='structured_experiences', content_rowid='id')
            """)
            cur.execute("""
            CREATE TRIGGER IF NOT EXISTS structured_experiences_ai AFTER INSERT ON structured_experiences BEGIN
              INSERT INTO structured_experiences_fts(rowid, task_type, goal, failure_reason, recommended_next_action, tags)
              VALUES (new.id, new.task_type, new.goal, new.failure_reason, new.recommended_next_action, new.tags);
            END;
            """)
        except sqlite3.OperationalError:
            # Some Python builds omit FTS5. LIKE fallback remains available.
            pass
        GovernedMemoryWriter.ensure_audit_table(self.conn)
        apply_migrations(self.conn, [Migration(1, "experience_store_schema_baseline", lambda _con: None)])
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            self.conn.execute(ddl)

    def _audit_memory_governance(self, event: dict[str, Any]) -> None:
        GovernedMemoryWriter.sqlite_audit(self.conn, event)

    def add(self, task: str, plan: str = "", outcome: str = "", tags: Optional[list[str]] = None, *, channel: str = "unknown", actor: str = "unknown", privacy_level: str = "normal") -> int:
        # Legacy task log remains permissive but redacted, so existing normal
        # task traces are searchable while secrets are not persisted.
        self._audit_memory_governance({
            "event": "memory_governance_decision",
            "allow_write": True,
            "namespace": f"{channel}:{actor}",
            "privacy_level": privacy_level,
            "reason": "legacy task trace redacted before persistence",
        })
        task = self._encrypt_text(self.privacy.redact_text(task))
        plan = self._encrypt_text(self.privacy.redact_text(plan))
        outcome = self._encrypt_text(self.privacy.redact_text(outcome))
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO experiences(created_at, task, plan, outcome, tags) VALUES(?,?,?,?,?)",
            (time.time(), task, plan, outcome, self._encrypt_text(json.dumps(tags or [], ensure_ascii=False))),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if self.encryption.enabled:
            rows = cur.execute("SELECT * FROM experiences ORDER BY created_at DESC LIMIT ?", (max(limit * 10, limit),)).fetchall()
            decoded = [self._decode_legacy(dict(r)) for r in rows]
            q = query.lower()
            matched = [r for r in decoded if q in str(r.get("task", "")).lower() or q in str(r.get("plan", "")).lower() or q in str(r.get("outcome", "")).lower() or q in str(r.get("tags", "")).lower()]
            return matched[:limit]
        try:
            rows = cur.execute(
                """
                SELECT e.* FROM experiences_fts f
                JOIN experiences e ON e.id = f.rowid
                WHERE experiences_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            rows = cur.execute(
                """
                SELECT * FROM experiences
                WHERE task LIKE ? OR plan LIKE ? OR outcome LIKE ? OR tags LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (like, like, like, like, limit),
            ).fetchall()
        return [self._decode_legacy(dict(r)) for r in rows]

    def add_experience(self, experience: dict[str, Any], *, channel: str = "unknown", actor: str = "unknown") -> int:
        governed = self.governed_writer.prepare(
            experience,
            channel=channel,
            actor=actor,
            privacy_level=str(experience.get("privacy_level", "normal")),
            audit=self._audit_memory_governance,
        )
        if not governed.ok:
            self.conn.commit()
            return -1
        experience = governed.payload
        now = time.time()
        values = {
            "created_at": now,
            "updated_at": now,
            "task_type": experience.get("task_type", "unknown"),
            "goal": experience.get("goal", ""),
            "success": 1 if experience.get("success") else 0,
            "failure_reason": experience.get("failure_reason"),
            "solution_attempted": json.dumps(experience.get("solution_attempted", []), ensure_ascii=False),
            "recommended_next_action": experience.get("recommended_next_action"),
            "risk_level": experience.get("risk_level", "medium"),
            "reusable_skill": 1 if experience.get("reusable_skill") else 0,
            "tool_cost": float(experience.get("tool_cost", 0) or 0),
            "success_score": float(experience.get("success_score", 1.0 if experience.get("success") else 0.0)),
            "human_feedback": experience.get("human_feedback"),
            "last_used_at": experience.get("last_used_at"),
            "privacy_level": experience.get("privacy_level", "normal"),
            "expires_at": experience.get("expires_at"),
            "memory_status": experience.get("memory_status", "candidate"),
            "confidence": float(experience.get("confidence", 0.5) or 0.5),
            "validation_count": int(experience.get("validation_count", 0) or 0),
            "negative_example_count": int(experience.get("negative_example_count", 0) or 0),
            "last_reviewed_at": experience.get("last_reviewed_at"),
            "promotion_reason": experience.get("promotion_reason"),
            "tags": json.dumps(experience.get("tags", []), ensure_ascii=False),
            "raw_trace": json.dumps(experience.get("raw_trace", {}), ensure_ascii=False),
        }
        for _field in self.SENSITIVE_STRUCTURED_FIELDS:
            values[_field] = self._encrypt_text(values.get(_field))
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO structured_experiences (
                created_at, updated_at, task_type, goal, success, failure_reason, solution_attempted,
                recommended_next_action, risk_level, reusable_skill, tool_cost, success_score, human_feedback,
                last_used_at, privacy_level, expires_at, memory_status, confidence, validation_count,
                negative_example_count, last_reviewed_at, promotion_reason, tags, raw_trace
            ) VALUES (
                :created_at, :updated_at, :task_type, :goal, :success, :failure_reason, :solution_attempted,
                :recommended_next_action, :risk_level, :reusable_skill, :tool_cost, :success_score, :human_feedback,
                :last_used_at, :privacy_level, :expires_at, :memory_status, :confidence, :validation_count,
                :negative_example_count, :last_reviewed_at, :promotion_reason, :tags, :raw_trace
            )
            """,
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def search_similar(self, query: str, limit: int = 5, *, only_reusable: bool = False) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if self.encryption.enabled:
            reusable_filter = "WHERE reusable_skill = 1" if only_reusable else ""
            rows = cur.execute(f"SELECT * FROM structured_experiences {reusable_filter} ORDER BY updated_at DESC LIMIT ?", (max(limit * 10, limit),)).fetchall()
            decoded = [self._decode_structured(dict(r)) for r in rows]
            q = query.lower()
            matched = [r for r in decoded if q in str(r.get("goal", "")).lower() or q in str(r.get("failure_reason", "")).lower() or q in str(r.get("recommended_next_action", "")).lower() or q in str(r.get("tags", "")).lower()]
            return matched[:limit]
        filters = "AND e.reusable_skill = 1" if only_reusable else ""
        try:
            rows = cur.execute(
                f"""
                SELECT e.* FROM structured_experiences_fts f
                JOIN structured_experiences e ON e.id = f.rowid
                WHERE structured_experiences_fts MATCH ? {filters}
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            rows = cur.execute(
                f"""
                SELECT * FROM structured_experiences e
                WHERE (goal LIKE ? OR failure_reason LIKE ? OR recommended_next_action LIKE ? OR tags LIKE ?)
                {filters}
                ORDER BY updated_at DESC LIMIT ?
                """,
                (like, like, like, like, limit),
            ).fetchall()
        return [self._decode_structured(dict(r)) for r in rows]

    def retrieve_for_task(self, task: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.search_similar(task, limit=limit)
        now = time.time()
        cur = self.conn.cursor()
        for row in rows:
            cur.execute("UPDATE structured_experiences SET last_used_at=? WHERE id=?", (now, row["id"]))
        self.conn.commit()
        return rows

    def summarize_failures(self, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        rows = self.conn.execute(
            """
            SELECT failure_reason, COUNT(*) AS count
            FROM structured_experiences
            WHERE success = 0 AND created_at >= ?
            GROUP BY failure_reason
            ORDER BY count DESC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_structured(self, *, days: int = 30, limit: int = 200, statuses: Optional[list[str]] = None) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        params: list[Any] = [since]
        status_filter = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            status_filter = f"AND memory_status IN ({placeholders})"
            params.extend(statuses)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT * FROM structured_experiences
            WHERE created_at >= ? {status_filter}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._decode_structured(dict(r)) for r in rows]

    def update_memory_review(
        self,
        experience_id: int,
        *,
        memory_status: str,
        confidence: float,
        reason: str = "",
        contradiction: bool = False,
        stale: bool = False,
    ) -> None:
        now = time.time()
        self.conn.execute(
            """
            UPDATE structured_experiences
            SET memory_status = ?,
                confidence = ?,
                validation_count = validation_count + ?,
                negative_example_count = negative_example_count + ?,
                last_reviewed_at = ?,
                promotion_reason = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                memory_status,
                max(0.0, min(1.0, confidence)),
                1 if memory_status in {"validated", "trusted"} else 0,
                1 if memory_status in {"deprecated", "blocked"} or contradiction else 0,
                now,
                reason,
                now,
                experience_id,
            ),
        )
        self._audit_memory_governance({
            "event": "memory_curator_review",
            "experience_id": experience_id,
            "memory_status": memory_status,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": reason,
            "contradiction": contradiction,
            "stale": stale,
        })
        self.conn.commit()

    def record_metric(self, *, success: bool, manual_intervention: bool = False, tool_error: bool = False,
                      repeat_failure: bool = False, skill_reuse: bool = False, rollback: bool = False,
                      security_violation: bool = False, day: Optional[str] = None) -> None:
        if day is None:
            day = time.strftime("%Y-%m-%d", time.localtime())
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO learning_metrics(day, updated_at) VALUES(?, ?)
            ON CONFLICT(day) DO NOTHING
            """,
            (day, time.time()),
        )
        cur.execute(
            """
            UPDATE learning_metrics
            SET task_count = task_count + 1,
                success_count = success_count + ?,
                failure_count = failure_count + ?,
                manual_intervention_count = manual_intervention_count + ?,
                tool_error_count = tool_error_count + ?,
                repeat_failure_count = repeat_failure_count + ?,
                skill_reuse_count = skill_reuse_count + ?,
                rollback_count = rollback_count + ?,
                security_violation_count = security_violation_count + ?,
                updated_at = ?
            WHERE day = ?
            """,
            (
                1 if success else 0,
                0 if success else 1,
                1 if manual_intervention else 0,
                1 if tool_error else 0,
                1 if repeat_failure else 0,
                1 if skill_reuse else 0,
                1 if rollback else 0,
                1 if security_violation else 0,
                time.time(),
                day,
            ),
        )
        self.conn.commit()

    def metrics_report(self, days: int = 7) -> dict[str, Any]:
        since_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        rows = self.conn.execute(
            "SELECT * FROM learning_metrics WHERE day >= ? ORDER BY day DESC",
            (since_day,),
        ).fetchall()
        items = [dict(r) for r in rows]
        totals = {
            "task_count": sum(i["task_count"] for i in items),
            "success_count": sum(i["success_count"] for i in items),
            "failure_count": sum(i["failure_count"] for i in items),
            "manual_intervention_count": sum(i["manual_intervention_count"] for i in items),
            "tool_error_count": sum(i["tool_error_count"] for i in items),
            "repeat_failure_count": sum(i["repeat_failure_count"] for i in items),
            "skill_reuse_count": sum(i["skill_reuse_count"] for i in items),
            "rollback_count": sum(i["rollback_count"] for i in items),
            "security_violation_count": sum(i["security_violation_count"] for i in items),
        }
        task_count = totals["task_count"] or 1
        return {
            "days": days,
            "daily": items,
            "totals": totals,
            "task_success_rate": totals["success_count"] / task_count,
            "manual_intervention_rate": totals["manual_intervention_count"] / task_count,
            "tool_error_rate": totals["tool_error_count"] / task_count,
            "repeat_failure_rate": totals["repeat_failure_count"] / task_count,
            "skill_reuse_rate": totals["skill_reuse_count"] / task_count,
        }

    def _encrypt_text(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.encryption.encrypt_text(value)
        return value

    def _decrypt_text(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.encryption.decrypt_text(value)
        return value

    def _decode_legacy(self, row: dict[str, Any]) -> dict[str, Any]:
        for key in self.SENSITIVE_LEGACY_FIELDS:
            if key in row:
                row[key] = self._decrypt_text(row[key])
        if isinstance(row.get("tags"), str):
            try:
                row["tags"] = json.loads(row["tags"])
            except json.JSONDecodeError:
                pass
        return row

    def _decode_structured(self, row: dict[str, Any]) -> dict[str, Any]:
        for key in self.SENSITIVE_STRUCTURED_FIELDS:
            if key in row:
                row[key] = self._decrypt_text(row[key])
        for key in ("solution_attempted", "tags"):
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except json.JSONDecodeError:
                    row[key] = []
        if isinstance(row.get("raw_trace"), str):
            try:
                row["raw_trace"] = json.loads(row["raw_trace"])
            except json.JSONDecodeError:
                row["raw_trace"] = {}
        row["success"] = bool(row.get("success"))
        row["reusable_skill"] = bool(row.get("reusable_skill"))
        return row


# User-facing alias requested in architecture notes.
ExperienceMemory = ExperienceStore
