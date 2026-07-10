from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.appsync.factory import create_appsync_store
from omnidesk_agent.appsync.store import AppSyncStore, IdempotencyConflict

PROJECT_ID_RE = re.compile(r"^proj_[a-f0-9]{16}$")
MAX_PROJECT_METADATA_BYTES = 16 * 1024
MAX_PROJECT_METADATA_DEPTH = 5
FORBIDDEN_METADATA_KEYS = {"__proto__", "constructor", "prototype"}
PROJECT_PATCH_KEYS = {"name", "description", "metadata", "archived"}
PROJECT_STORE_SCHEMA_VERSION = 1
PROJECT_STORE_BACKUP_COUNT = 3
PROJECT_STORE_LOCK_TIMEOUT_SECONDS = 10.0
PROJECT_STORE_CORRUPTION_CODE = "PROJECT_STORE_CORRUPT"
PROJECT_STORE_CORRUPTION_MARKER_SCHEMA = "omnidesk-project-store-corruption/v1"
PROJECT_STORE_PUBLIC_UNAVAILABLE_MESSAGE = "Project storage is unavailable pending administrator recovery."
PROJECT_STORE_RECOVERY_POLICY = (
    "An owner must restore a checksum-valid rotated backup through the administrator recovery tool."
)

POSTGRES_PROJECTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS omnidesk_appsync_projects (
    namespace TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_actor TEXT NOT NULL,
    source_device_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived BOOLEAN NOT NULL DEFAULT false,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    deleted_at DOUBLE PRECISION,
    PRIMARY KEY(namespace, project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS omnidesk_appsync_projects_active_name_idx
    ON omnidesk_appsync_projects(namespace, organization_id, lower(name))
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS omnidesk_appsync_projects_org_updated_idx
    ON omnidesk_appsync_projects(namespace, organization_id, updated_at DESC);
"""


def _now() -> float:
    return time.time()


def _project_id() -> str:
    return f"proj_{uuid.uuid4().hex[:16]}"


def _actor(decision: Any) -> str:
    return getattr(decision, "actor", "app-client") or "app-client"


def _store(rt: Any, cfg: Any) -> AppSyncStore:
    store = getattr(rt, "app_sync", None)
    if store is None:
        store = create_appsync_store(cfg)
        rt.app_sync = store
    return store


def _idempotency_key(request: Request, payload: dict[str, Any] | None = None) -> str | None:
    header = request.headers.get("idempotency-key") or request.headers.get("x-idempotency-key")
    body_value = (payload or {}).get("idempotency_key")
    value = str(header or body_value or "").strip()
    return value[:180] or None


def _require_idempotency(cfg: Any, request: Request, payload: dict[str, Any] | None = None) -> str | None:
    key = _idempotency_key(request, payload)
    app_sync = getattr(cfg, "app_sync", None)
    if getattr(app_sync, "require_idempotency", False) and not key:
        raise HTTPException(428, "idempotency-key is required for this write operation")
    return key


def _validate_metadata_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_PROJECT_METADATA_DEPTH:
        raise ValueError(f"project metadata must be at most {MAX_PROJECT_METADATA_DEPTH} levels deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_validate_metadata_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in FORBIDDEN_METADATA_KEYS:
                raise ValueError(f"project metadata key is not allowed: {normalized_key}")
            if len(normalized_key) > 120:
                raise ValueError("project metadata keys must be 120 characters or less")
            normalized[normalized_key] = _validate_metadata_value(item, depth=depth + 1)
        return normalized
    raise ValueError(f"project metadata value is not JSON-serializable: {type(value).__name__}")


def _validate_metadata(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("project metadata must be an object")
    metadata = _validate_metadata_value(value)
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROJECT_METADATA_BYTES:
        raise ValueError(f"project metadata must be {MAX_PROJECT_METADATA_BYTES} bytes or less")
    return metadata


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}
    return {}


class ProjectStoreCorruptionError(RuntimeError):
    """Raised when local/dev project storage cannot be trusted."""

    def __init__(self, message: str, *, quarantine_path: Path | None = None):
        self.error_code = PROJECT_STORE_CORRUPTION_CODE
        self.quarantine_path = quarantine_path
        super().__init__(message)


class ProjectStoreLockTimeout(RuntimeError):
    """Raised when another process holds the local project-store lock too long."""


class GatewayProjectStore:
    """Durable project registry used by the tri-app Gateway contract.

    JSON storage remains the local/dev fallback. When AppSync is backed by
    PostgreSQL, projects are persisted in the normalized
    `omnidesk_appsync_projects` table so multiple Gateway instances share the
    same source of truth.
    """

    def __init__(self, app_sync: AppSyncStore):
        self.app_sync = app_sync
        base_path = Path(getattr(app_sync, "path", "app_sync.json"))
        self.path = base_path.with_name(f"{base_path.name}.projects.json")
        self.lock_path = Path(str(self.path) + ".lock")
        self.corruption_marker_path = Path(str(self.path) + ".corruption.json")

    @property
    def _postgres_enabled(self) -> bool:
        return callable(getattr(self.app_sync, "_connect", None)) and callable(getattr(self.app_sync, "_ensure_schema", None))

    def _organization_for_actor(self, actor: str) -> str:
        return str(self.app_sync._organization_for_actor(actor))  # noqa: SLF001 - same package boundary

    def _ensure_postgres_schema(self, conn: Any) -> None:
        self.app_sync._ensure_schema(conn)  # noqa: SLF001 - same package boundary
        with conn.cursor() as cur:
            cur.execute(POSTGRES_PROJECTS_SCHEMA_SQL)

    def _jsonb(self, value: dict[str, Any]) -> Any:
        from psycopg.types.json import Jsonb  # type: ignore

        return Jsonb(value)

    def _project_from_row(self, row: Any) -> dict[str, Any]:
        metadata = _json_dict(row[6])
        return {
            "project_id": row[0],
            "name": row[1],
            "description": row[2],
            "owner_actor": row[3],
            "organization_id": row[4],
            "source_device_id": row[5],
            "metadata": metadata,
            "archived": bool(row[7]),
            "created_at": float(row[8]),
            "updated_at": float(row[9]),
            "deleted_at": float(row[10]) if row[10] is not None else None,
        }

    @staticmethod
    def _canonical_projects(projects: dict[str, dict[str, Any]]) -> bytes:
        return json.dumps(projects, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def _projects_checksum(cls, projects: dict[str, dict[str, Any]]) -> str:
        return "sha256:" + hashlib.sha256(cls._canonical_projects(projects)).hexdigest()

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + PROJECT_STORE_LOCK_TIMEOUT_SECONDS
        acquired = False
        try:
            while not acquired:
                try:
                    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, OSError) as exc:
                    if time.monotonic() >= deadline:
                        raise ProjectStoreLockTimeout(
                            f"timed out acquiring project store lock: {self.lock_path}"
                        ) from exc
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                try:
                    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    @contextmanager
    def _local_store_guard(self) -> Iterator[None]:
        with self.app_sync._lock:  # noqa: SLF001 - same package boundary
            with self._file_lock():
                yield

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _write_json_atomically(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{path}.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_directory()
        finally:
            tmp.unlink(missing_ok=True)

    def _read_store_file(self, path: Path) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProjectStoreCorruptionError(f"invalid json or unreadable file: {exc}") from exc
        return self._decode_store(raw)

    def _backup_status(self) -> list[dict[str, Any]]:
        status: list[dict[str, Any]] = []
        for index in range(1, PROJECT_STORE_BACKUP_COUNT + 1):
            path = Path(f"{self.path}.bak.{index}")
            item: dict[str, Any] = {"index": index, "path": str(path), "present": path.is_file(), "valid": False}
            if path.is_file():
                try:
                    projects = self._read_store_file(path)
                    item.update(
                        {
                            "valid": True,
                            "project_count": len(projects),
                            "checksum": self._projects_checksum(projects),
                        }
                    )
                except ProjectStoreCorruptionError as exc:
                    item["error"] = str(exc)[:500]
            status.append(item)
        return status

    def _read_corruption_marker(self) -> dict[str, Any] | None:
        if not self.corruption_marker_path.exists():
            return None
        try:
            marker = json.loads(self.corruption_marker_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "schema": PROJECT_STORE_CORRUPTION_MARKER_SCHEMA,
                "status": "blocked",
                "reason": f"corruption marker is unreadable: {exc}",
            }
        if not isinstance(marker, dict):
            return {
                "schema": PROJECT_STORE_CORRUPTION_MARKER_SCHEMA,
                "status": "blocked",
                "reason": "corruption marker root must be an object",
            }
        return marker

    def _marked_corruption_error(self, marker: dict[str, Any]) -> ProjectStoreCorruptionError:
        quarantine_value = str(marker.get("quarantine_path") or "").strip()
        quarantine_path = Path(quarantine_value) if quarantine_value else None
        reason = str(marker.get("reason") or "persistent corruption marker is active")
        return ProjectStoreCorruptionError(
            f"local project storage is blocked pending administrator recovery: {reason}",
            quarantine_path=quarantine_path,
        )

    def _raise_if_corruption_marked(self) -> None:
        marker = self._read_corruption_marker()
        if marker is not None:
            raise self._marked_corruption_error(marker)

    def _quarantine_corrupt_store(self, reason: str) -> ProjectStoreCorruptionError:
        existing_marker = self._read_corruption_marker()
        if existing_marker is not None:
            return self._marked_corruption_error(existing_marker)
        quarantine_path: Path | None = None
        marker = {
            "schema": PROJECT_STORE_CORRUPTION_MARKER_SCHEMA,
            "status": "blocked",
            "error_code": PROJECT_STORE_CORRUPTION_CODE,
            "detected_at_unix": time.time(),
            "path": str(self.path),
            "reason": reason[:500],
            "quarantine_path": None,
            "recovery_required": True,
            "recovery_policy": PROJECT_STORE_RECOVERY_POLICY,
        }
        try:
            # Persist the marker before moving the primary file. A crash between
            # these steps must still leave the next process fail closed.
            self._write_json_atomically(self.corruption_marker_path, marker)
        except Exception as exc:
            return ProjectStoreCorruptionError(
                f"local project storage failed integrity validation and the corruption marker could not be persisted: {exc}"
            )
        if self.path.exists():
            quarantine_path = Path(f"{self.path}.corrupt.{time.time_ns()}")
            try:
                os.replace(self.path, quarantine_path)
                self._fsync_directory()
            except OSError as exc:
                marker["quarantine_error"] = str(exc)[:500]
                quarantine_path = None
        marker["quarantine_path"] = str(quarantine_path) if quarantine_path else None
        marker["backup_candidates"] = self._backup_status()
        try:
            self._write_json_atomically(self.corruption_marker_path, marker)
        except OSError:
            # The initial durable marker already blocks every subsequent load.
            pass
        event = getattr(self.app_sync, "_event", None)
        persist = getattr(self.app_sync, "_persist", None)
        if callable(event):
            event(
                "project.store.corruption",
                "system",
                {
                    "error_code": PROJECT_STORE_CORRUPTION_CODE,
                    "path": str(self.path),
                    "quarantine_path": str(quarantine_path) if quarantine_path else None,
                    "reason": reason[:500],
                },
            )
        if callable(persist):
            persist()
        return ProjectStoreCorruptionError(
            f"local project storage failed integrity validation: {reason}",
            quarantine_path=quarantine_path,
        )

    def _decode_store(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict):
            raise ProjectStoreCorruptionError("project store root must be an object")
        schema_version = raw.get("schema_version", 0)
        if schema_version not in {0, PROJECT_STORE_SCHEMA_VERSION}:
            raise ProjectStoreCorruptionError(f"unsupported project store schema_version: {schema_version}")
        projects = raw.get("projects")
        if not isinstance(projects, dict):
            raise ProjectStoreCorruptionError("project store projects must be an object")
        if schema_version == PROJECT_STORE_SCHEMA_VERSION:
            expected = str(raw.get("checksum") or "")
            actual = self._projects_checksum(projects)
            if not expected or not hmac.compare_digest(expected, actual):
                raise ProjectStoreCorruptionError("project store checksum mismatch")
        normalized: dict[str, dict[str, Any]] = {}
        for project_id, project in projects.items():
            key = str(project_id)
            if not PROJECT_ID_RE.fullmatch(key):
                raise ProjectStoreCorruptionError(f"invalid project id in store: {key}")
            if not isinstance(project, dict):
                raise ProjectStoreCorruptionError(f"project record must be an object: {key}")
            record = dict(project)
            if str(record.get("project_id") or "") != key:
                raise ProjectStoreCorruptionError(f"project record id mismatch: {key}")
            normalized[key] = record
        return normalized

    def _load(self) -> dict[str, dict[str, Any]]:
        self._raise_if_corruption_marked()
        if not self.path.exists():
            return {}
        try:
            return self._read_store_file(self.path)
        except ProjectStoreCorruptionError as exc:
            raise self._quarantine_corrupt_store(str(exc)) from exc
        except Exception as exc:
            raise self._quarantine_corrupt_store(f"invalid json or unreadable file: {exc}") from exc

    def _rotate_backups(self) -> None:
        if not self.path.exists():
            return
        oldest = Path(f"{self.path}.bak.{PROJECT_STORE_BACKUP_COUNT}")
        oldest.unlink(missing_ok=True)
        for index in range(PROJECT_STORE_BACKUP_COUNT - 1, 0, -1):
            source = Path(f"{self.path}.bak.{index}")
            target = Path(f"{self.path}.bak.{index + 1}")
            if source.exists():
                os.replace(source, target)
        shutil.copy2(self.path, Path(f"{self.path}.bak.1"))

    def _persist(self, projects: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "schema_version": PROJECT_STORE_SCHEMA_VERSION,
            "checksum": self._projects_checksum(projects),
            "projects": projects,
        }
        tmp = Path(f"{self.path}.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._rotate_backups()
            os.replace(tmp, self.path)
            self._fsync_directory()
        finally:
            tmp.unlink(missing_ok=True)

    def recovery_status(self) -> dict[str, Any]:
        if self._postgres_enabled:
            return {"backend": "postgres", "blocked": False, "recovery_required": False, "backups": []}
        with self._local_store_guard():
            marker = self._read_corruption_marker()
            return {
                "backend": "json",
                "blocked": marker is not None,
                "recovery_required": marker is not None,
                "marker": marker,
                "backups": self._backup_status(),
            }

    def public_recovery_status(self) -> dict[str, Any]:
        """Return owner-facing recovery state without exception text or server paths."""
        if self._postgres_enabled:
            return {"backend": "postgres", "blocked": False, "recovery_required": False, "marker": None, "backups": []}
        with self._local_store_guard():
            marker = self._read_corruption_marker()
            public_marker = None
            if marker is not None:
                public_marker = {
                    "schema": PROJECT_STORE_CORRUPTION_MARKER_SCHEMA,
                    "status": "blocked",
                    "error_code": PROJECT_STORE_CORRUPTION_CODE,
                    "recovery_required": True,
                    "recovery_policy": PROJECT_STORE_RECOVERY_POLICY,
                    "quarantine_present": bool(str(marker.get("quarantine_path") or "").strip()),
                }
            backups: list[dict[str, Any]] = []
            for index in range(1, PROJECT_STORE_BACKUP_COUNT + 1):
                path = Path(f"{self.path}.bak.{index}")
                item: dict[str, Any] = {"index": index, "present": path.is_file(), "valid": False}
                if path.is_file():
                    try:
                        projects = self._read_store_file(path)
                    except ProjectStoreCorruptionError:
                        pass
                    else:
                        item.update(
                            {
                                "valid": True,
                                "project_count": len(projects),
                                "checksum": self._projects_checksum(projects),
                            }
                        )
                backups.append(item)
            return {
                "backend": "json",
                "blocked": marker is not None,
                "recovery_required": marker is not None,
                "marker": public_marker,
                "backups": backups,
            }

    def assert_available(self) -> None:
        """Fail closed before request validation or idempotency replay."""
        if self._postgres_enabled:
            return
        with self._local_store_guard():
            self._raise_if_corruption_marked()

    def recover_from_backup(self, *, actor: str, backup_index: int | None = None) -> dict[str, Any]:
        if self._postgres_enabled:
            raise RuntimeError("project store recovery is only available for the local JSON backend")
        if backup_index is not None and backup_index not in range(1, PROJECT_STORE_BACKUP_COUNT + 1):
            raise ValueError(f"backup_index must be between 1 and {PROJECT_STORE_BACKUP_COUNT}")
        with self._local_store_guard():
            marker = self._read_corruption_marker()
            if marker is None:
                raise ValueError("project store is not marked corrupt")
            candidates = self._backup_status()
            valid = [
                item
                for item in candidates
                if item.get("valid") and (backup_index is None or item.get("index") == backup_index)
            ]
            if not valid:
                raise ProjectStoreCorruptionError(
                    "no checksum-valid project store backup is available; corruption marker remains active"
                )
            selected = valid[0]
            selected_path = Path(str(selected["path"]))
            projects = self._read_store_file(selected_path)
            recovery_quarantine_path: Path | None = None
            if self.path.exists():
                recovery_quarantine_path = Path(f"{self.path}.corrupt.recovery.{time.time_ns()}")
                os.replace(self.path, recovery_quarantine_path)
            self._persist(projects)
            verified = self._read_store_file(self.path)
            if self._projects_checksum(verified) != self._projects_checksum(projects):
                raise ProjectStoreCorruptionError("restored project store checksum verification failed")
            self.corruption_marker_path.unlink()
            self._fsync_directory()
            event = getattr(self.app_sync, "_event", None)
            persist = getattr(self.app_sync, "_persist", None)
            if callable(event):
                event(
                    "project.store.recovered",
                    actor,
                    {
                        "error_code": PROJECT_STORE_CORRUPTION_CODE,
                        "backup_index": selected["index"],
                        "backup_path": str(selected_path),
                        "project_count": len(verified),
                        "checksum": self._projects_checksum(verified),
                        "recovery_quarantine_path": str(recovery_quarantine_path) if recovery_quarantine_path else None,
                    },
                )
            if callable(persist):
                persist()
            return {
                "status": "recovered",
                "backup_index": selected["index"],
                "backup_path": str(selected_path),
                "project_count": len(verified),
                "checksum": self._projects_checksum(verified),
            }

    def _event(self, event_type: str, actor: str, project: dict[str, Any]) -> None:
        event = getattr(self.app_sync, "_event", None)
        persist = getattr(self.app_sync, "_persist", None)
        if callable(event):
            event(
                event_type,
                actor,
                {
                    "project_id": project["project_id"],
                    "name": project.get("name"),
                    "organization_id": project.get("organization_id"),
                },
            )
        if callable(persist):
            persist()

    def _remember_idempotency(
        self,
        *,
        actor: str,
        endpoint: str,
        key: Optional[str],
        payload: dict[str, Any] | None,
        response: dict[str, Any],
    ) -> None:
        self.app_sync._idempotency_put(  # noqa: SLF001
            actor=actor,
            endpoint=endpoint,
            key=key,
            payload=payload,
            response=response,
        )
        if key:
            persist = getattr(self.app_sync, "_persist", None)
            if callable(persist):
                persist()

    def list_projects(self, *, actor: str, include_deleted: bool = False) -> list[dict[str, Any]]:
        if self._postgres_enabled:
            return self._postgres_list_projects(actor=actor, include_deleted=include_deleted)
        with self._local_store_guard():
            organization_id = self._organization_for_actor(actor)
            projects = self._load().values()
            scoped = [
                dict(project)
                for project in projects
                if project.get("organization_id") == organization_id
                and (include_deleted or not project.get("deleted_at"))
            ]
            return sorted(scoped, key=lambda item: float(item.get("created_at") or 0), reverse=True)

    def _postgres_list_projects(self, *, actor: str, include_deleted: bool = False) -> list[dict[str, Any]]:
        organization_id = self._organization_for_actor(actor)
        with self.app_sync._connect() as conn:  # noqa: SLF001 - same package boundary
            self._ensure_postgres_schema(conn)
            with conn.cursor() as cur:
                query = (
                    """
                    SELECT project_id, name, description, owner_actor, organization_id, source_device_id,
                        metadata, archived, created_at, updated_at, deleted_at
                    FROM omnidesk_appsync_projects
                    WHERE namespace=%s AND organization_id=%s
                    ORDER BY created_at DESC
                    """
                    if include_deleted
                    else """
                    SELECT project_id, name, description, owner_actor, organization_id, source_device_id,
                        metadata, archived, created_at, updated_at, deleted_at
                    FROM omnidesk_appsync_projects
                    WHERE namespace=%s AND organization_id=%s AND deleted_at IS NULL
                    ORDER BY created_at DESC
                    """
                )
                cur.execute(
                    query,
                    (self.app_sync.namespace, organization_id),  # noqa: SLF001
                )
                return [self._project_from_row(row) for row in cur.fetchall()]

    def create_project(
        self,
        *,
        actor: str,
        name: str,
        description: str = "",
        metadata: dict[str, Any] | None = None,
        source_device_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        idempotency_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.assert_available()
        name = str(name or "").strip()
        if not name:
            raise ValueError("project name is required")
        if len(name) > 120:
            raise ValueError("project name must be 120 characters or less")
        description = str(description or "").strip()[:1000]
        metadata = _validate_metadata(metadata or {})
        if self._postgres_enabled:
            return self._postgres_create_project(
                actor=actor,
                name=name,
                description=description,
                metadata=metadata,
                source_device_id=source_device_id,
                idempotency_key=idempotency_key,
                idempotency_payload=idempotency_payload,
            )
        with self._local_store_guard():
            self._raise_if_corruption_marked()
            cached = self.app_sync._idempotency_get(  # noqa: SLF001
                actor=actor,
                endpoint="projects.create",
                key=idempotency_key,
                payload=idempotency_payload,
            )
            if cached is not None:
                return dict(cached)
            organization_id = self._organization_for_actor(actor)
            projects = self._load()
            duplicate = [
                project
                for project in projects.values()
                if project.get("organization_id") == organization_id
                and not project.get("deleted_at")
                and str(project.get("name") or "").casefold() == name.casefold()
            ]
            if duplicate:
                raise ValueError("project already exists")
            now = _now()
            project = {
                "project_id": _project_id(),
                "name": name,
                "description": description,
                "owner_actor": actor or "unknown",
                "organization_id": organization_id,
                "source_device_id": source_device_id,
                "metadata": metadata,
                "archived": False,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
            projects[project["project_id"]] = project
            self._persist(projects)
            self._event("project.created", actor, project)
            self._remember_idempotency(
                actor=actor,
                endpoint="projects.create",
                key=idempotency_key,
                payload=idempotency_payload,
                response=project,
            )
            return dict(project)

    def _postgres_create_project(
        self,
        *,
        actor: str,
        name: str,
        description: str,
        metadata: dict[str, Any],
        source_device_id: Optional[str],
        idempotency_key: Optional[str],
        idempotency_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cached = self.app_sync._idempotency_get(  # noqa: SLF001
            actor=actor,
            endpoint="projects.create",
            key=idempotency_key,
            payload=idempotency_payload,
        )
        if cached is not None:
            return dict(cached)
        organization_id = self._organization_for_actor(actor)
        now = _now()
        project = {
            "project_id": _project_id(),
            "name": name,
            "description": description,
            "owner_actor": actor or "unknown",
            "organization_id": organization_id,
            "source_device_id": source_device_id,
            "metadata": metadata,
            "archived": False,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        with self.app_sync._connect() as conn:  # noqa: SLF001 - same package boundary
            self._ensure_postgres_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id FROM omnidesk_appsync_projects
                    WHERE namespace=%s AND organization_id=%s AND deleted_at IS NULL
                    AND lower(name)=lower(%s)
                    LIMIT 1
                    """,
                    (self.app_sync.namespace, organization_id, name),  # noqa: SLF001
                )
                if cur.fetchone():
                    raise ValueError("project already exists")
                cur.execute(
                    """
                    INSERT INTO omnidesk_appsync_projects(
                        namespace, organization_id, project_id, name, description, owner_actor,
                        source_device_id, metadata, archived, created_at, updated_at, deleted_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.app_sync.namespace,  # noqa: SLF001
                        organization_id,
                        project["project_id"],
                        name,
                        description,
                        project["owner_actor"],
                        source_device_id,
                        self._jsonb(metadata),
                        False,
                        now,
                        now,
                        None,
                    ),
                )
        self._event("project.created", actor, project)
        self._remember_idempotency(
            actor=actor,
            endpoint="projects.create",
            key=idempotency_key,
            payload=idempotency_payload,
            response=project,
        )
        return dict(project)

    def update_project(
        self,
        *,
        actor: str,
        project_id: str,
        patch: dict[str, Any],
        idempotency_key: Optional[str] = None,
        idempotency_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.assert_available()
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise KeyError("project not found")
        unknown_keys = set(patch) - PROJECT_PATCH_KEYS - {"idempotency_key"}
        if unknown_keys:
            raise ValueError(f"unsupported project patch fields: {sorted(unknown_keys)}")
        if self._postgres_enabled:
            return self._postgres_update_project(
                actor=actor,
                project_id=project_id,
                patch=patch,
                idempotency_key=idempotency_key,
                idempotency_payload=idempotency_payload,
            )
        with self._local_store_guard():
            self._raise_if_corruption_marked()
            cached = self.app_sync._idempotency_get(  # noqa: SLF001
                actor=actor,
                endpoint="projects.update",
                key=idempotency_key,
                payload=idempotency_payload,
            )
            if cached is not None:
                return dict(cached)
            organization_id = self._organization_for_actor(actor)
            projects = self._load()
            project = projects.get(project_id)
            if not project or project.get("organization_id") != organization_id or project.get("deleted_at"):
                raise KeyError("project not found")
            if "name" in patch:
                name = str(patch.get("name") or "").strip()
                if not name:
                    raise ValueError("project name is required")
                if len(name) > 120:
                    raise ValueError("project name must be 120 characters or less")
                for other_id, other in projects.items():
                    if other_id == project_id:
                        continue
                    duplicate_name = str(other.get("name") or "").casefold() == name.casefold()
                    if other.get("organization_id") == organization_id and not other.get("deleted_at") and duplicate_name:
                        raise ValueError("project already exists")
                project["name"] = name
            if "description" in patch:
                project["description"] = str(patch.get("description") or "").strip()[:1000]
            if "metadata" in patch:
                project["metadata"] = _validate_metadata(patch.get("metadata") or {})
            if "archived" in patch:
                project["archived"] = bool(patch.get("archived"))
            project["updated_at"] = _now()
            projects[project_id] = project
            self._persist(projects)
            self._event("project.updated", actor, project)
            self._remember_idempotency(
                actor=actor,
                endpoint="projects.update",
                key=idempotency_key,
                payload=idempotency_payload,
                response=project,
            )
            return dict(project)

    def _postgres_update_project(
        self,
        *,
        actor: str,
        project_id: str,
        patch: dict[str, Any],
        idempotency_key: Optional[str],
        idempotency_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cached = self.app_sync._idempotency_get(  # noqa: SLF001
            actor=actor,
            endpoint="projects.update",
            key=idempotency_key,
            payload=idempotency_payload,
        )
        if cached is not None:
            return dict(cached)
        organization_id = self._organization_for_actor(actor)
        with self.app_sync._connect() as conn:  # noqa: SLF001 - same package boundary
            self._ensure_postgres_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id, name, description, owner_actor, organization_id, source_device_id,
                        metadata, archived, created_at, updated_at, deleted_at
                    FROM omnidesk_appsync_projects
                    WHERE namespace=%s AND organization_id=%s AND project_id=%s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (self.app_sync.namespace, organization_id, project_id),  # noqa: SLF001
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError("project not found")
                project = self._project_from_row(row)
                if "name" in patch:
                    name = str(patch.get("name") or "").strip()
                    if not name:
                        raise ValueError("project name is required")
                    if len(name) > 120:
                        raise ValueError("project name must be 120 characters or less")
                    cur.execute(
                        """
                        SELECT project_id FROM omnidesk_appsync_projects
                        WHERE namespace=%s AND organization_id=%s AND project_id<>%s
                        AND deleted_at IS NULL AND lower(name)=lower(%s)
                        LIMIT 1
                        """,
                        (self.app_sync.namespace, organization_id, project_id, name),  # noqa: SLF001
                    )
                    if cur.fetchone():
                        raise ValueError("project already exists")
                    project["name"] = name
                if "description" in patch:
                    project["description"] = str(patch.get("description") or "").strip()[:1000]
                if "metadata" in patch:
                    project["metadata"] = _validate_metadata(patch.get("metadata") or {})
                if "archived" in patch:
                    project["archived"] = bool(patch.get("archived"))
                project["updated_at"] = _now()
                cur.execute(
                    """
                    UPDATE omnidesk_appsync_projects
                    SET name=%s, description=%s, metadata=%s, archived=%s, updated_at=%s
                    WHERE namespace=%s AND project_id=%s
                    """,
                    (
                        project["name"],
                        project["description"],
                        self._jsonb(project["metadata"]),
                        project["archived"],
                        project["updated_at"],
                        self.app_sync.namespace,  # noqa: SLF001
                        project_id,
                    ),
                )
        self._event("project.updated", actor, project)
        self._remember_idempotency(
            actor=actor,
            endpoint="projects.update",
            key=idempotency_key,
            payload=idempotency_payload,
            response=project,
        )
        return dict(project)

    def delete_project(
        self,
        *,
        actor: str,
        project_id: str,
        idempotency_key: Optional[str] = None,
        idempotency_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.assert_available()
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise KeyError("project not found")
        if self._postgres_enabled:
            return self._postgres_delete_project(
                actor=actor,
                project_id=project_id,
                idempotency_key=idempotency_key,
                idempotency_payload=idempotency_payload,
            )
        with self._local_store_guard():
            self._raise_if_corruption_marked()
            cached = self.app_sync._idempotency_get(  # noqa: SLF001
                actor=actor,
                endpoint="projects.delete",
                key=idempotency_key,
                payload=idempotency_payload,
            )
            if cached is not None:
                return dict(cached)
            organization_id = self._organization_for_actor(actor)
            projects = self._load()
            project = projects.get(project_id)
            if not project or project.get("organization_id") != organization_id or project.get("deleted_at"):
                raise KeyError("project not found")
            now = _now()
            project["deleted_at"] = now
            project["updated_at"] = now
            projects[project_id] = project
            self._persist(projects)
            self._event("project.deleted", actor, project)
            self._remember_idempotency(
                actor=actor,
                endpoint="projects.delete",
                key=idempotency_key,
                payload=idempotency_payload,
                response=project,
            )
            return dict(project)

    def _postgres_delete_project(
        self,
        *,
        actor: str,
        project_id: str,
        idempotency_key: Optional[str],
        idempotency_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cached = self.app_sync._idempotency_get(  # noqa: SLF001
            actor=actor,
            endpoint="projects.delete",
            key=idempotency_key,
            payload=idempotency_payload,
        )
        if cached is not None:
            return dict(cached)
        organization_id = self._organization_for_actor(actor)
        with self.app_sync._connect() as conn:  # noqa: SLF001 - same package boundary
            self._ensure_postgres_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id, name, description, owner_actor, organization_id, source_device_id,
                        metadata, archived, created_at, updated_at, deleted_at
                    FROM omnidesk_appsync_projects
                    WHERE namespace=%s AND organization_id=%s AND project_id=%s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (self.app_sync.namespace, organization_id, project_id),  # noqa: SLF001
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError("project not found")
                project = self._project_from_row(row)
                now = _now()
                project["deleted_at"] = now
                project["updated_at"] = now
                cur.execute(
                    """
                    UPDATE omnidesk_appsync_projects
                    SET deleted_at=%s, updated_at=%s
                    WHERE namespace=%s AND project_id=%s
                    """,
                    (now, now, self.app_sync.namespace, project_id),  # noqa: SLF001
                )
        self._event("project.deleted", actor, project)
        self._remember_idempotency(
            actor=actor,
            endpoint="projects.delete",
            key=idempotency_key,
            payload=idempotency_payload,
            response=project,
        )
        return dict(project)


def register_project_routes(app: FastAPI, cfg: Any, rt: Any, metrics: Any, admin: Any) -> None:
    app_sync = _store(rt, cfg)
    projects = GatewayProjectStore(app_sync)

    @app.get("/admin/projects/store/recovery")
    async def admin_project_store_recovery_status(request: Request):
        await admin(request, "owner")
        return {"ok": True, "recovery": projects.public_recovery_status()}

    @app.post("/admin/projects/store/recover")
    async def admin_recover_project_store(request: Request):
        decision = await admin(request, "owner")
        payload = await request.json()
        if not _idempotency_key(request, payload):
            raise HTTPException(428, "idempotency-key is required for project store recovery")
        raw_index = payload.get("backup_index")
        try:
            backup_index = int(raw_index) if raw_index is not None else None
            result = projects.recover_from_backup(actor=_actor(decision), backup_index=backup_index)
        except ProjectStoreCorruptionError as exc:
            raise HTTPException(
                503,
                detail={"code": PROJECT_STORE_CORRUPTION_CODE, "message": PROJECT_STORE_PUBLIC_UNAVAILABLE_MESSAGE},
            ) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        metrics.inc("omnidesk_app_project_requests_total", operation="recover") if metrics else None
        return {"ok": True, "recovery": result}

    @app.get("/app/projects")
    async def app_list_projects(request: Request, include_deleted: bool = False):
        decision = await admin(request, "viewer")
        actor = _actor(decision)
        try:
            result = projects.list_projects(actor=actor, include_deleted=include_deleted)
        except ProjectStoreCorruptionError as exc:
            raise HTTPException(
                503,
                detail={"code": PROJECT_STORE_CORRUPTION_CODE, "message": PROJECT_STORE_PUBLIC_UNAVAILABLE_MESSAGE},
            ) from exc
        metrics.inc("omnidesk_app_project_requests_total", operation="list") if metrics else None
        return {"ok": True, "projects": result}

    @app.post("/app/projects")
    async def app_create_project(request: Request):
        decision = await admin(request, "operator")
        actor = _actor(decision)
        payload = await request.json()
        try:
            projects.assert_available()
            idem_key = _require_idempotency(cfg, request, payload)
            project = projects.create_project(
                actor=actor,
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                source_device_id=payload.get("source_device_id"),
                idempotency_key=idem_key,
                idempotency_payload=payload,
            )
        except ProjectStoreCorruptionError as exc:
            raise HTTPException(
                503,
                detail={"code": PROJECT_STORE_CORRUPTION_CODE, "message": PROJECT_STORE_PUBLIC_UNAVAILABLE_MESSAGE},
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        metrics.inc("omnidesk_app_project_requests_total", operation="create") if metrics else None
        return {"ok": True, "project": project}

    @app.patch("/app/projects/{project_id}")
    async def app_update_project(project_id: str, request: Request):
        decision = await admin(request, "operator")
        actor = _actor(decision)
        payload = await request.json()
        try:
            projects.assert_available()
            idem_key = _require_idempotency(cfg, request, payload)
            project = projects.update_project(
                actor=actor,
                project_id=project_id,
                patch=payload,
                idempotency_key=idem_key,
                idempotency_payload={**payload, "project_id": project_id},
            )
        except ProjectStoreCorruptionError as exc:
            raise HTTPException(
                503,
                detail={"code": PROJECT_STORE_CORRUPTION_CODE, "message": PROJECT_STORE_PUBLIC_UNAVAILABLE_MESSAGE},
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        metrics.inc("omnidesk_app_project_requests_total", operation="update") if metrics else None
        return {"ok": True, "project": project}

    @app.delete("/app/projects/{project_id}")
    async def app_delete_project(project_id: str, request: Request):
        decision = await admin(request, "operator")
        actor = _actor(decision)
        payload = {"project_id": project_id}
        try:
            projects.assert_available()
            idem_key = _require_idempotency(cfg, request, payload)
            project = projects.delete_project(
                actor=actor,
                project_id=project_id,
                idempotency_key=idem_key,
                idempotency_payload=payload,
            )
        except ProjectStoreCorruptionError as exc:
            raise HTTPException(
                503,
                detail={"code": PROJECT_STORE_CORRUPTION_CODE, "message": PROJECT_STORE_PUBLIC_UNAVAILABLE_MESSAGE},
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        metrics.inc("omnidesk_app_project_requests_total", operation="delete") if metrics else None
        return {"ok": True, "project": project}
