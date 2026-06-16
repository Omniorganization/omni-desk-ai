from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnidesk_agent.config import AppConfig
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.server_routes.admin_routes import register_admin_routes
from scripts import production_smoke_test


async def allow_admin(request, role="viewer"):
    request.state.seen_role = role


class Metrics:
    def render_prometheus(self):
        return "omnidesk_test_metric 1\n"

    def counter_sum(self, name, **labels):
        return 0.0


class Queue:
    def stats(self):
        return {"total": 0, "dead_letter": 0}


class Outbound:
    def stats(self):
        return {"total": 0, "dead_letter": 0}


class Runtime:
    def __init__(self, memory):
        self.job_queue = Queue()
        self.outbound_messages = Outbound()
        self.memory = memory
        self.model_cost_store = None

    def status(self):
        return {"ok": True}


def test_memory_purge_expired_supports_dry_run_execute_and_audit(tmp_path):
    store = ExperienceStore(tmp_path / "memory.sqlite3")
    try:
        expired_id = store.add_experience({"task_type": "ops", "goal": "expired", "expires_at": time.time() - 60})
        fresh_id = store.add_experience({"task_type": "ops", "goal": "fresh", "expires_at": time.time() + 3600})

        dry = store.purge_expired(dry_run=True, now=time.time())
        assert dry["candidate_count"] == 1
        assert dry["deleted_count"] == 0
        assert dry["ids"] == [expired_id]
        assert store.search_similar("expired", limit=5)

        executed = store.purge_expired(dry_run=False, now=time.time())
        assert executed["candidate_count"] == 1
        assert executed["deleted_count"] == 1
        assert not store.search_similar("expired", limit=5)
        assert store.search_similar("fresh", limit=5)[0]["id"] == fresh_id

        audit_events = [json.loads(row[0]) for row in store.conn.execute("SELECT event_json FROM memory_governance_audit ORDER BY id").fetchall()]
        purge_events = [event for event in audit_events if event.get("event") == "memory_retention_purge"]
        assert [event["dry_run"] for event in purge_events[-2:]] == [True, False]
    finally:
        store.close()


def test_admin_memory_purge_expired_endpoints(tmp_path):
    store = ExperienceStore(tmp_path / "memory.sqlite3")
    try:
        store.add_experience({"task_type": "ops", "goal": "expired", "expires_at": time.time() - 60})
        app = FastAPI()
        cfg = AppConfig()
        cfg.workspace.root = tmp_path
        register_admin_routes(app, cfg, Runtime(store), Metrics(), "test-version", allow_admin)
        with TestClient(app) as client:
            dry = client.get("/admin/memory/purge-expired", params={"dry_run": "true"}).json()
            assert dry["ok"] is True
            assert dry["purge"]["candidate_count"] == 1
            assert dry["purge"]["deleted_count"] == 0
            executed = client.post("/admin/memory/purge-expired").json()
            assert executed["purge"]["deleted_count"] == 1
    finally:
        store.close()


def test_production_smoke_cli_exposes_ga_options():
    result = subprocess.run(["python3", "scripts/production_smoke_test.py", "--help"], check=True, text=True, capture_output=True)
    for option in ["--base-url", "--admin-token-env", "--sandbox-url", "--json", "--fail-on-slo", "--check-admin-metrics", "--check-admin-slo", "--expected-version", "--expected-artifact-sha256", "--expected-build-sha", "--expected-image-digest"]:
        assert option in result.stdout


def test_production_smoke_cli_wires_arguments(monkeypatch):
    seen: dict[str, object] = {}

    def fake_check_app(*, check_admin_metrics=False, check_admin_slo=False, fail_on_slo=False, expected_version=None, expected_artifact_sha256=None, expected_build_sha=None, expected_image_digest=None):
        import os
        seen["base_url"] = os.environ["OMNIDESK_SMOKE_BASE_URL"]
        seen["admin_token"] = os.environ["OMNIDESK_SMOKE_ADMIN_TOKEN"]
        seen["check_admin_metrics"] = check_admin_metrics
        seen["check_admin_slo"] = check_admin_slo
        seen["fail_on_slo"] = fail_on_slo
        seen["expected_version"] = expected_version
        seen["expected_artifact_sha256"] = expected_artifact_sha256
        seen["expected_build_sha"] = expected_build_sha
        seen["expected_image_digest"] = expected_image_digest
        return {"health": {"ok": True}}

    monkeypatch.setenv("CUSTOM_ADMIN_TOKEN", "secret-token")
    monkeypatch.setattr(production_smoke_test, "check_app", fake_check_app)
    monkeypatch.setattr(production_smoke_test, "check_sandbox", lambda strict=False: None)

    assert production_smoke_test.main([
        "--base-url", "https://staging.example",
        "--admin-token-env", "CUSTOM_ADMIN_TOKEN",
        "--check-admin-metrics",
        "--check-admin-slo",
        "--fail-on-slo",
        "--expected-version", "0.7.test",
        "--expected-artifact-sha256", "a" * 64,
        "--expected-build-sha", "abc123",
        "--expected-image-digest", "sha256:" + "b" * 64,
    ]) == 0
    assert seen == {
        "base_url": "https://staging.example",
        "admin_token": "secret-token",
        "check_admin_metrics": True,
        "check_admin_slo": True,
        "fail_on_slo": True,
        "expected_version": "0.7.test",
        "expected_artifact_sha256": "a" * 64,
        "expected_build_sha": "abc123",
        "expected_image_digest": "sha256:" + "b" * 64,
    }


def test_release_script_has_dependency_preflight_and_build_contract():
    text = Path("scripts/build_release.sh").read_text(encoding="utf-8")
    assert "PY_RELEASE_PREFLIGHT" in text
    assert "Missing release build dependencies" in text
    assert "python -m build" in text
    assert "bash scripts/release_smoke_locked.sh" in text
    assert "python scripts/verify_release_artifact.py dist --require-signatures --require-metadata" in text


def test_memory_purge_expired_filters_by_actor_and_channel(tmp_path):
    store = ExperienceStore(tmp_path / "memory.sqlite3")
    try:
        now = time.time()
        keep = store.add_experience({"task_type": "ops", "goal": "expired other actor", "expires_at": now - 60}, channel="web", actor="bob")
        purge = store.add_experience({"task_type": "ops", "goal": "expired alice", "expires_at": now - 60}, channel="web", actor="alice")

        dry = store.purge_expired(dry_run=True, now=now, channel="web", actor="alice")
        assert dry["namespace_filter_applied"] is True
        assert dry["ids"] == [purge]

        executed = store.purge_expired(dry_run=False, now=now, channel="web", actor="alice")
        assert executed["deleted_count"] == 1
        remaining = {item["id"] for item in store.list_structured(days=1, limit=10)}
        assert keep in remaining
        assert purge not in remaining
    finally:
        store.close()


def test_deploy_artifact_uses_fixed_adapter_not_arbitrary_shell(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"wheel")
    result = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "staging", "--mode", "noop"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "Validated artifact" in result.stdout

    bad = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "staging", "--mode", "bash -lc id"],
        text=True,
        capture_output=True,
    )
    assert bad.returncode != 0
    assert "unsupported deploy mode" in bad.stderr


def test_staging_and_production_workflows_promote_signed_artifacts():
    for workflow in [Path(".github/workflows/deploy-staging.yml"), Path(".github/workflows/promote-production.yml")]:
        text = workflow.read_text(encoding="utf-8")
        assert "artifact_run_id" in text
        assert "gh run download" in text
        assert "verify_release_artifact.py" in text
        assert "verify_release_signatures.py" in text
        assert "bash scripts/deploy_artifact.sh" in text
        assert "OMNIDESK_STAGING_DEPLOY_COMMAND" not in text
        assert "bash -lc" not in text


def test_attestation_workflow_exists_for_release_artifacts():
    text = Path(".github/workflows/attestation.yml").read_text(encoding="utf-8")
    assert "id-token: write" in text
    assert "attestations: write" in text
    assert "gh attestation sign" in text



def test_production_smoke_expected_runtime_identity():
    ok = production_smoke_test._assert_expected_runtime_identity(
        [{"ok": True, "version": "1.0.0", "artifact_sha256": "a" * 64, "build_sha": "abc", "image_digest": "sha256:" + "b" * 64}],
        expected_version="1.0.0",
        expected_artifact_sha256="a" * 64,
        expected_build_sha="abc",
        expected_image_digest="sha256:" + "b" * 64,
    )
    assert ok["version"] == "1.0.0"

    try:
        production_smoke_test._assert_expected_runtime_identity(
            [{"ok": True, "version": "old"}], expected_version="new"
        )
    except RuntimeError as exc:
        assert "runtime version mismatch" in str(exc)
    else:
        raise AssertionError("expected version mismatch to fail")


def test_deploy_artifact_forbids_noop_for_production_and_checks_sha(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"wheel")
    digest = __import__("hashlib").sha256(b"wheel").hexdigest()

    noop = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "production", "--mode", "noop"],
        text=True,
        capture_output=True,
    )
    assert noop.returncode != 0
    assert "noop deploy mode is forbidden" in noop.stderr

    bad_sha = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "staging", "--mode", "noop", "--artifact-sha256", "0" * 64],
        text=True,
        capture_output=True,
    )
    assert bad_sha.returncode != 0
    assert "artifact sha256 mismatch" in bad_sha.stderr

    good = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "staging", "--mode", "noop", "--artifact-sha256", digest],
        text=True,
        capture_output=True,
        check=True,
    )
    assert digest in good.stdout


def test_release_and_promotion_attestation_gates_are_mandatory():
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    promote = Path(".github/workflows/promote-production.yml").read_text(encoding="utf-8")
    rollback = Path(".github/workflows/rollback-drill.yml").read_text(encoding="utf-8")

    assert "attestations: write" in release
    assert "docker buildx imagetools inspect" in release
    assert 'echo "OMNIDESK_IMAGE_DIGEST=$digest"' in release
    assert 'echo "OMNIDESK_WEB_ADMIN_IMAGE_DIGEST=$web_admin_digest"' in release
    assert "inputs.image_digest" not in release
    assert "--build-arg OMNIDESK_IMAGE_DIGEST" not in release
    assert "find dist -maxdepth 1 -type f" in release
    assert 'gh attestation sign "$artifact"' in release
    assert "python scripts/verify_release_artifact.py dist --expected-version" in release
    assert "cyclonedx-py environment -o sbom.json" not in release
    assert "gh attestation sign sbom.json" not in release
    assert 'gh attestation verify "$artifact"' in promote
    assert "OMNIDESK_WEB_ADMIN_IMAGE_DIGEST" in promote
    assert "find current previous -maxdepth 1 -type f" in rollback
    assert "release_metadata.json" in release
    assert "release_metadata.json" in promote
    assert "noop deploy mode is forbidden for production promotion" in promote
    assert "default: docker-compose" in promote
    assert "--expected-version" in promote
    assert "--expected-artifact-sha256" in promote
    assert "--expected-image-digest" not in promote
    assert "scripts/read_release_metadata.py dist image.digest" in promote
    assert "current_artifact_run_id" in rollback
    assert "previous_artifact_run_id" in rollback
    assert "Roll back to previous artifact" in rollback



def test_ga1_release_metadata_and_systemd_deploy_assets_exist():
    assert Path("scripts/write_release_metadata.py").exists()
    systemd = Path("deploy/systemd/omnidesk-deploy-artifact").read_text(encoding="utf-8")
    assert "--expected-version" in systemd
    assert "artifact sha256 mismatch" in systemd
    assert "runtime artifact sha mismatch" in systemd
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "org.opencontainers.image.version" in dockerfile
    assert "omnidesk.artifact.sha256" in dockerfile
    assert "OMNIDESK_ARTIFACT_SHA256" in dockerfile


def test_deploy_artifact_validates_image_digest_argument(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"wheel")
    bad = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "staging", "--mode", "noop", "--image-digest", "not-a-digest"],
        text=True,
        capture_output=True,
    )
    assert bad.returncode != 0
    assert "image digest must be" in bad.stderr


def test_deploy_artifact_validates_docker_compose_service_image_digest(tmp_path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"wheel")
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  omnidesk:\n    image: registry.example/omnidesk:latest\n", encoding="utf-8")
    digest = "sha256:" + "a" * 64
    config = tmp_path / "compose-config.yml"
    config.write_text(f"services:\n  omnidesk:\n    image: registry.example/omnidesk@{digest}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  if [[ "$arg" == "config" ]]; then
    cat "$DOCKER_COMPOSE_CONFIG_OUTPUT"
    exit 0
  fi
  if [[ "$arg" == "pull" || "$arg" == "up" ]]; then
    exit 0
  fi
done
echo "unexpected docker command: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + ":" + os.environ.get("PATH", ""),
        "DOCKER_COMPOSE_CONFIG_OUTPUT": str(config),
        "OMNIDESK_DEPLOY_COMPOSE_FILE": str(compose),
        "OMNIDESK_DEPLOY_SERVICE": "omnidesk",
    }
    ok = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "production", "--mode", "docker-compose", "--image-digest", digest],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr

    bad = subprocess.run(
        ["bash", "scripts/deploy_artifact.sh", "--artifact", str(artifact), "--environment", "production", "--mode", "docker-compose", "--image-digest", "sha256:" + "b" * 64],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert bad.returncode != 0
    assert "docker-compose service image digest does not match" in bad.stderr


def test_verify_release_artifact_checks_release_metadata(tmp_path):
    import zipfile
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "omnidesk_agent-1.2.3-py3-none-any.whl"
    metadata_name = "omnidesk_agent-1.2.3.dist-info/METADATA"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(metadata_name, "Metadata-Version: 2.1\nName: omnidesk-agent\nVersion: 1.2.3\n")
    digest = __import__("hashlib").sha256(wheel.read_bytes()).hexdigest()
    (dist / "release_metadata.json").write_text(__import__("json").dumps({
        "version": "1.2.3",
        "artifact": {"name": wheel.name, "sha256": digest},
        "image": {"digest": "sha256:" + "c" * 64},
    }), encoding="utf-8")
    checksum_manifest = f"{digest}  {wheel.name}\n{__import__('hashlib').sha256((dist / 'release_metadata.json').read_bytes()).hexdigest()}  release_metadata.json\n"
    (dist / "checksums.txt").write_text(checksum_manifest, encoding="utf-8")
    (dist / "SHA256SUMS.txt").write_text(checksum_manifest, encoding="utf-8")
    ok = subprocess.run([
        "python3", "scripts/verify_release_artifact.py", str(dist),
        "--expected-version", "1.2.3",
        "--require-metadata",
        "--expected-image-digest", "sha256:" + "c" * 64,
    ], text=True, capture_output=True)
    assert ok.returncode == 0, ok.stderr
