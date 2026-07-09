from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "workspace"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.learning.growth_plan_file = tmp_path / "growth.json"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.gateway.admin_allowed_ips = ["testclient", "127.0.0.1", "::1"]
    return cfg


def test_gateway_project_crud_contract_syncs_across_tri_app_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    monkeypatch.setenv("OMNIDESK_VIEWER_TOKEN", "viewer-token")
    app = create_app(_cfg(tmp_path))
    operator_headers = {
        "authorization": "Bearer operator-token",
        "x-omnidesk-actor": "alice",
    }
    viewer_headers = {
        "authorization": "Bearer viewer-token",
        "x-omnidesk-actor": "alice",
    }

    with TestClient(app) as client:
        empty = client.get("/app/projects", headers=viewer_headers)
        assert empty.status_code == 200, empty.text
        assert empty.json()["projects"] == []

        created = client.post(
            "/app/projects",
            headers={**operator_headers, "idempotency-key": "project-create-1"},
            json={"name": "Launch Plan", "description": "Tri-app project"},
        )
        assert created.status_code == 200, created.text
        project = created.json()["project"]
        assert project["project_id"].startswith("proj_")
        assert project["name"] == "Launch Plan"
        assert project["organization_id"] == "org_default"

        replay = client.post(
            "/app/projects",
            headers={**operator_headers, "idempotency-key": "project-create-1"},
            json={"name": "Launch Plan", "description": "Tri-app project"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["project"]["project_id"] == project["project_id"]

        listed = client.get("/app/projects", headers=viewer_headers)
        assert listed.status_code == 200, listed.text
        assert [item["project_id"] for item in listed.json()["projects"]] == [project["project_id"]]

        updated = client.patch(
            f"/app/projects/{project['project_id']}",
            headers={**operator_headers, "idempotency-key": "project-update-1"},
            json={"name": "Launch Plan v2", "metadata": {"surface": "mobile"}},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["project"]["name"] == "Launch Plan v2"
        assert updated.json()["project"]["metadata"] == {"surface": "mobile"}

        sync = client.get("/app/sync?since_seq=0", headers=viewer_headers)
        assert sync.status_code == 200, sync.text
        event_types = [event["event_type"] for event in sync.json()["events"]]
        assert "project.created" in event_types
        assert "project.updated" in event_types

        deleted = client.delete(
            f"/app/projects/{project['project_id']}",
            headers={**operator_headers, "idempotency-key": "project-delete-1"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["project"]["deleted_at"] is not None

        after_delete = client.get("/app/projects", headers=viewer_headers)
        assert after_delete.status_code == 200, after_delete.text
        assert after_delete.json()["projects"] == []


def test_gateway_project_contract_is_organization_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIDESK_OPERATOR_TOKEN", "operator-token")
    app = create_app(_cfg(tmp_path))

    def headers(idempotency_key: str) -> dict[str, str]:
        return {
            "authorization": "Bearer operator-token",
            "idempotency-key": idempotency_key,
        }

    with TestClient(app) as client:
        monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
        alice_device = client.post(
            "/app/devices/register",
            headers=headers("alice-mobile"),
            json={
                "device_id": "alice-mobile",
                "device_type": "mobile",
                "name": "Alice Mobile",
                "platform": "iOS",
                "organization_id": "org-a",
            },
        )
        assert alice_device.status_code == 200, alice_device.text
        alice_project = client.post(
            "/app/projects",
            headers=headers("alice-project"),
            json={"name": "Org A Project"},
        )
        assert alice_project.status_code == 200, alice_project.text

        monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "bob")
        bob_device = client.post(
            "/app/devices/register",
            headers=headers("bob-mobile"),
            json={
                "device_id": "bob-mobile",
                "device_type": "mobile",
                "name": "Bob Mobile",
                "platform": "Android",
                "organization_id": "org-b",
            },
        )
        assert bob_device.status_code == 200, bob_device.text
        bob_project = client.post(
            "/app/projects",
            headers=headers("bob-project"),
            json={"name": "Org B Project"},
        )
        assert bob_project.status_code == 200, bob_project.text

        monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "alice")
        alice_projects = client.get("/app/projects", headers=headers("alice-list")).json()["projects"]
        monkeypatch.setenv("OMNIDESK_OPERATOR_ACTOR", "bob")
        bob_projects = client.get("/app/projects", headers=headers("bob-list")).json()["projects"]
        assert [project["name"] for project in alice_projects] == ["Org A Project"]
        assert [project["name"] for project in bob_projects] == ["Org B Project"]
