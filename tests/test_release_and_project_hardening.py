from __future__ import annotations

import errno
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnidesk_agent.appsync.projects import GatewayProjectStore, ProjectStoreCorruptionError
from omnidesk_agent.appsync.store import AppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.validation.production import validate_production_config
from scripts import check_customer_distribution_ga as customer_ga
from scripts.write_native_artifact_manifest import build_manifest


def test_customer_distribution_ga_requires_complete_and_live(monkeypatch, tmp_path: Path) -> None:
    binding_report = tmp_path / "current-release-artifact-binding.json"
    binding_report.write_text(
        json.dumps(
            {
                "schema": "omnidesk-current-release-artifact-binding/v1",
                "status": "passed",
                "repository": "owner/repo",
                "source_commit": "abc",
                "release_run_id": "release-1",
                "main_verification_run_id": "main-1",
                "all_artifacts_bound": True,
                "platforms": [
                    {"platform": platform, "status": "passed"}
                    for platform in ("android", "ios", "macos", "windows")
                ],
                "failures": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        customer_ga,
        "audit_complete_real_ga",
        lambda root, evidence_dir: {
            "status": "passed",
            "version": "1.12.7",
            "categories": {"external": {"ok": True}},
        },
    )
    monkeypatch.setattr(
        customer_ga,
        "audit_live_main_verification",
        lambda repository, commit_sha, token, api_base: {
            "status": "passed",
            "failures": [],
            "artifact_ids": [1],
        },
    )
    passed = customer_ga.audit(
        tmp_path,
        tmp_path / "evidence",
        repository="owner/repo",
        commit_sha="abc",
        token="token",
        current_release_binding_report=binding_report,
    )
    assert passed["status"] == "passed"
    assert passed["blocker_count"] == 0

    monkeypatch.setattr(
        customer_ga,
        "audit_live_main_verification",
        lambda repository, commit_sha, token, api_base: {
            "status": "blocked",
            "failures": ["missing artifact"],
        },
    )
    blocked = customer_ga.audit(
        tmp_path,
        tmp_path / "evidence",
        repository="owner/repo",
        commit_sha="abc",
        token="token",
        current_release_binding_report=binding_report,
    )
    assert blocked["status"] == "blocked_missing_external_evidence"
    assert blocked["categories"]["main_verification_live_artifact"]["ok"] is False


def test_release_workflows_use_bound_evidence_and_non_bypassable_final_gate() -> None:
    main_verification = Path(".github/workflows/main-verification.yml").read_text(encoding="utf-8")
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    readiness = Path(".github/workflows/real-ga-readiness.yml").read_text(encoding="utf-8")
    assert "external-ga-evidence-bound" in main_verification
    assert "release/external-evidence/**" in main_verification
    assert "check_customer_distribution_ga.py" in release
    assert "check_customer_distribution_ga.py" in readiness
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in release
    assert "npm run tauri:build" in release
    assert "flutter build appbundle --release" in release
    assert "flutter build ipa --release" in release


def test_native_artifact_manifest_hashes_nested_payloads(tmp_path: Path) -> None:
    payload = tmp_path / "desktop" / "bundle" / "app.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"native")
    report = build_manifest("desktop-linux", tmp_path, "abc123", build_run_id="build-42")
    assert report["status"] == "passed"
    assert report["artifact_count"] == 1
    assert report["artifacts"][0]["path"] == "desktop/bundle/app.bin"
    assert report["artifacts"][0]["sha256"].startswith("sha256:")
    assert report["release_payload_artifact_sha256"] == report["artifacts"][0]["sha256"]
    assert report["build_run_id"] == "build-42"


def test_gateway_project_store_quarantines_corrupt_json(tmp_path: Path) -> None:
    app_sync = AppSyncStore(tmp_path / "app-sync.json")
    store = GatewayProjectStore(app_sync)
    store.path.write_text('{"projects":', encoding="utf-8")
    try:
        store.list_projects(actor="alice")
    except ProjectStoreCorruptionError as exc:
        assert exc.error_code == "PROJECT_STORE_CORRUPT"
    else:
        raise AssertionError("corrupt project store must fail closed")
    assert not store.path.exists()
    assert list(tmp_path.glob("app-sync.json.projects.json.corrupt.*"))
    assert store.corruption_marker_path.is_file()
    with pytest.raises(ProjectStoreCorruptionError):
        GatewayProjectStore(AppSyncStore(tmp_path / "app-sync.json")).list_projects(actor="alice")


def test_gateway_project_store_writes_versioned_checksum_and_backup(tmp_path: Path) -> None:
    app_sync = AppSyncStore(tmp_path / "app-sync.json")
    store = GatewayProjectStore(app_sync)
    first = store.create_project(actor="alice", name="One")
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    assert envelope["schema_version"] == 1
    assert envelope["checksum"].startswith("sha256:")
    store.update_project(actor="alice", project_id=first["project_id"], patch={"name": "Two"})
    assert Path(str(store.path) + ".bak.1").exists()


def test_gateway_project_store_restart_remains_blocked_until_admin_recovery(tmp_path: Path) -> None:
    app_sync_path = tmp_path / "app-sync.json"
    store = GatewayProjectStore(AppSyncStore(app_sync_path))
    project = store.create_project(actor="alice", name="One")
    store.update_project(actor="alice", project_id=project["project_id"], patch={"name": "Two"})
    store.path.write_text('{"projects":', encoding="utf-8")

    with pytest.raises(ProjectStoreCorruptionError):
        store.list_projects(actor="alice")
    restarted = GatewayProjectStore(AppSyncStore(app_sync_path))
    with pytest.raises(ProjectStoreCorruptionError):
        restarted.create_project(actor="alice", name="Blocked")
    with pytest.raises(ProjectStoreCorruptionError):
        restarted.update_project(actor="alice", project_id=project["project_id"], patch={"name": "Blocked"})
    with pytest.raises(ProjectStoreCorruptionError):
        restarted.delete_project(actor="alice", project_id=project["project_id"])

    recovery = restarted.recover_from_backup(actor="owner")
    assert recovery["status"] == "recovered"
    assert recovery["backup_index"] == 1
    assert not restarted.corruption_marker_path.exists()
    assert restarted.list_projects(actor="alice")[0]["name"] == "One"


def test_gateway_project_store_recovery_skips_checksum_invalid_backup(tmp_path: Path) -> None:
    store = GatewayProjectStore(AppSyncStore(tmp_path / "app-sync.json"))
    project = store.create_project(actor="alice", name="One")
    store.update_project(actor="alice", project_id=project["project_id"], patch={"name": "Two"})
    store.update_project(actor="alice", project_id=project["project_id"], patch={"name": "Three"})
    Path(f"{store.path}.bak.1").write_text('{"schema_version":1,"checksum":"sha256:bad","projects":{}}', encoding="utf-8")
    store.path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ProjectStoreCorruptionError):
        store.list_projects(actor="alice")

    recovery = store.recover_from_backup(actor="owner")
    assert recovery["backup_index"] == 2
    assert store.list_projects(actor="alice")[0]["name"] == "One"


def test_gateway_project_store_disk_full_preserves_previous_primary(monkeypatch, tmp_path: Path) -> None:
    app_sync_path = tmp_path / "app-sync.json"
    store = GatewayProjectStore(AppSyncStore(app_sync_path))
    project = store.create_project(actor="alice", name="Stable")

    def disk_full() -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(store, "_rotate_backups", disk_full)
    with pytest.raises(OSError, match="disk full"):
        store.update_project(actor="alice", project_id=project["project_id"], patch={"name": "Lost"})

    restarted = GatewayProjectStore(AppSyncStore(app_sync_path))
    assert restarted.list_projects(actor="alice")[0]["name"] == "Stable"
    assert not list(tmp_path.glob("*.tmp.*"))


def test_gateway_project_store_ignores_crash_temp_and_serializes_concurrent_writes(tmp_path: Path) -> None:
    app_sync_path = tmp_path / "app-sync.json"
    seed = GatewayProjectStore(AppSyncStore(app_sync_path))
    seed.create_project(actor="alice", name="Seed")
    Path(f"{seed.path}.tmp.crashed").write_text("partial", encoding="utf-8")
    assert GatewayProjectStore(AppSyncStore(app_sync_path)).list_projects(actor="alice")[0]["name"] == "Seed"

    def create(index: int) -> None:
        GatewayProjectStore(AppSyncStore(app_sync_path)).create_project(actor="alice", name=f"Concurrent {index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(create, range(8)))
    projects = GatewayProjectStore(AppSyncStore(app_sync_path)).list_projects(actor="alice")
    assert {item["name"] for item in projects} == {"Seed", *(f"Concurrent {index}" for index in range(8))}


def test_production_rejects_json_appsync_even_without_multi_instance_flag() -> None:
    cfg = AppConfig()
    cfg.app_sync.backend = "json"
    result = validate_production_config(cfg, {"OMNIDESK_REQUIRE_PRODUCTION_GUARDS": "1"})
    assert (
        "app_sync.backend must be postgres in production; JSON project storage is local/dev only"
        in result["issues"]
    )
