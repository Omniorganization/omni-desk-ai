from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request

from omnidesk_agent.appsync.factory import create_appsync_store
from omnidesk_agent.appsync.store import AppSyncStore, IdempotencyConflict

PROJECT_ID_RE = re.compile(r"^proj_[a-f0-9]{16}$")


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


class GatewayProjectStore:
    """Durable project registry used by the tri-app Gateway contract.

    The existing AppSyncStore owns the task/approval/conversation timeline. Projects
    are stored in a sibling JSON document so the new contract can ship without a
    breaking migration of the existing app_sync.json schema. Project mutations still
    emit AppSync events for sync cursors and audit visibility.
    """

    def __init__(self, app_sync: AppSyncStore):
        self.app_sync = app_sync
        base_path = Path(getattr(app_sync, "path", "app_sync.json"))
        self.path = base_path.with_name(f"{base_path.name}.projects.json")

    def _organization_for_actor(self, actor: str) -> str:
        return str(self.app_sync._organization_for_actor(actor))  # noqa: SLF001 - same package boundary

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        projects = raw.get("projects") if isinstance(raw, dict) else {}
        if not isinstance(projects, dict):
            return {}
        return {
            str(project_id): dict(project)
            for project_id, project in projects.items()
            if isinstance(project, dict)
        }

    def _persist(self, projects: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"projects": projects}, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

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

    def list_projects(self, *, actor: str, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self.app_sync._lock:  # noqa: SLF001 - same package boundary
            organization_id = self._organization_for_actor(actor)
            projects = self._load().values()
            scoped = [
                dict(project)
                for project in projects
                if project.get("organization_id") == organization_id
                and (include_deleted or not project.get("deleted_at"))
            ]
            return sorted(scoped, key=lambda item: float(item.get("created_at") or 0), reverse=True)

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
        name = str(name or "").strip()
        if not name:
            raise ValueError("project name is required")
        if len(name) > 120:
            raise ValueError("project name must be 120 characters or less")
        description = str(description or "").strip()[:1000]
        metadata = dict(metadata or {})
        with self.app_sync._lock:  # noqa: SLF001 - same package boundary
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
            self.app_sync._idempotency_put(  # noqa: SLF001
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
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise KeyError("project not found")
        with self.app_sync._lock:  # noqa: SLF001 - same package boundary
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
                    if other.get("organization_id") == organization_id and not other.get("deleted_at") and str(other.get("name") or "").casefold() == name.casefold():
                        raise ValueError("project already exists")
                project["name"] = name
            if "description" in patch:
                project["description"] = str(patch.get("description") or "").strip()[:1000]
            if "metadata" in patch:
                project["metadata"] = dict(patch.get("metadata") or {})
            if "archived" in patch:
                project["archived"] = bool(patch.get("archived"))
            project["updated_at"] = _now()
            projects[project_id] = project
            self._persist(projects)
            self._event("project.updated", actor, project)
            self.app_sync._idempotency_put(  # noqa: SLF001
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
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise KeyError("project not found")
        with self.app_sync._lock:  # noqa: SLF001 - same package boundary
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
            self.app_sync._idempotency_put(  # noqa: SLF001
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

    @app.get("/app/projects")
    async def app_list_projects(request: Request, include_deleted: bool = False):
        decision = await admin(request, "viewer")
        actor = _actor(decision)
        result = projects.list_projects(actor=actor, include_deleted=include_deleted)
        metrics.inc("omnidesk_app_project_requests_total", operation="list") if metrics else None
        return {"ok": True, "projects": result}

    @app.post("/app/projects")
    async def app_create_project(request: Request):
        decision = await admin(request, "operator")
        actor = _actor(decision)
        payload = await request.json()
        idem_key = _require_idempotency(cfg, request, payload)
        try:
            project = projects.create_project(
                actor=actor,
                name=str(payload.get("name") or ""),
                description=str(payload.get("description") or ""),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
                source_device_id=payload.get("source_device_id"),
                idempotency_key=idem_key,
                idempotency_payload=payload,
            )
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
        idem_key = _require_idempotency(cfg, request, payload)
        try:
            project = projects.update_project(
                actor=actor,
                project_id=project_id,
                patch=payload,
                idempotency_key=idem_key,
                idempotency_payload={**payload, "project_id": project_id},
            )
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
        idem_key = _require_idempotency(cfg, request, payload)
        try:
            project = projects.delete_project(
                actor=actor,
                project_id=project_id,
                idempotency_key=idem_key,
                idempotency_payload=payload,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        metrics.inc("omnidesk_app_project_requests_total", operation="delete") if metrics else None
        return {"ok": True, "project": project}
