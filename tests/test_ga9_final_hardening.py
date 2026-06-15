from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from omnidesk_agent.observability import JsonEventLogger, MetricsRegistry
from omnidesk_agent.observability_tracing import current_trace_id, trace_span

ROOT = Path(__file__).resolve().parents[1]


def run_py(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=merged, text=True, capture_output=True, check=True)


def test_release_workflow_generates_image_digest_contract() -> None:
    text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "docker build" in text
    assert "docker push" in text
    assert "docker buildx imagetools inspect" in text
    assert 'echo "OMNIDESK_IMAGE_DIGEST=$digest"' in text
    assert "inputs.image_digest" not in text
    assert "--build-arg OMNIDESK_IMAGE_DIGEST" not in text
    assert "docker buildx imagetools inspect" in text


def test_actionlint_yamllint_are_part_of_ci() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "yamllint .github/workflows" in text
    assert "actionlint" in text


def test_systemd_production_and_maintenance_assets_exist() -> None:
    required = [
        "deploy/systemd/omnidesk-agent.production.service",
        "deploy/systemd/omnidesk-backup.timer",
        "deploy/systemd/omnidesk-maintenance.timer",
        "deploy/systemd/logrotate.omnidesk",
        "scripts/maintenance_sqlite.py",
        "scripts/check_disk_guard.py",
        ".github/workflows/maintenance-drill.yml",
        ".github/workflows/alert-drill.yml",
    ]
    for rel in required:
        assert (ROOT / rel).exists(), rel
    service = (ROOT / "deploy/systemd/omnidesk-agent.production.service").read_text(encoding="utf-8")
    assert "User=omnidesk" in service
    assert "/etc/omnidesk/config.yaml" in service
    assert "/var/lib/omnidesk" in service


def test_sqlite_maintenance_script(tmp_path: Path) -> None:
    db = tmp_path / "maintenance.sqlite3"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table t(id integer primary key, v text)")
    con.execute("insert into t(v) values ('ok')")
    con.commit()
    con.close()
    cp = run_py("scripts/maintenance_sqlite.py", "--integrity-check", "--wal-checkpoint", "--vacuum", "--json", str(db))
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["results"][0]["integrity_check"] == "ok"


def test_disk_guard_allows_generous_threshold() -> None:
    cp = run_py("scripts/check_disk_guard.py", "--path", ".", "--max-used-percent", "100", "--min-free-mb", "1")
    assert json.loads(cp.stdout)["ok"] is True


def test_backup_manifest_hmac_and_public_manifest(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "backup.sqlite3"
    con = sqlite3.connect(db)
    con.execute("create table t(id integer primary key, v text)")
    con.execute("insert into t(v) values ('ok')")
    con.commit()
    con.close()
    dest = tmp_path / "backups"
    env = {
        "OMNIDESK_BACKUP_ENCRYPTION_KEY": "x" * 40,
        "OMNIDESK_BACKUP_MANIFEST_KEY": "y" * 40,
    }
    run_py("scripts/backup_sqlite.py", "--dest", str(dest), "--encrypt", "--sign-manifest", "--redact-manifest-paths", str(db), env=env)
    run_py("scripts/verify_backup.py", "--require-encryption", "--require-manifest-signature", str(dest / "backup_manifest.json"), env=env)
    public_manifest = json.loads((dest / "backup_manifest.public.json").read_text(encoding="utf-8"))
    assert public_manifest["items"][0]["source"] == "<redacted>"


def test_observability_tracing_records_metrics() -> None:
    metrics = MetricsRegistry()
    logger = JsonEventLogger("test-ga9-tracing")
    with trace_span("webhook_to_outbound", metrics=metrics, logger=logger, run_id="run-1") as span:
        assert span.trace_id == current_trace_id()
    assert metrics.counter_sum("omnidesk_trace_spans_total", span="webhook_to_outbound") == 1


def test_slsa_provenance_contains_complete_artifact_subjects(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ["pkg-0.0.1-py3-none-any.whl", "pkg-0.0.1.tar.gz", "sbom.json", "release_metadata.json", "checksums.txt", "release_signatures.json"]:
        (dist / name).write_text(name, encoding="utf-8")
    cp = run_py("scripts/write_slsa_provenance.py", str(dist), "--image-ref", "ghcr.io/acme/omnidesk:abc", "--image-digest", "sha256:" + "a" * 64)
    assert cp.returncode == 0
    payload = json.loads((dist / "slsa-provenance.json").read_text(encoding="utf-8"))
    names = {subject["name"] for subject in payload["subject"]}
    assert "pkg-0.0.1-py3-none-any.whl" in names
    assert "pkg-0.0.1.tar.gz" in names
    assert "sbom.json" in names
    assert "release_metadata.json" in names
    assert "checksums.txt" in names
    assert "release_signatures.json" in names
    assert "ghcr.io/acme/omnidesk:abc" in names
