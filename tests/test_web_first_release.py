from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.web_first_host import private_write, restore, validate_origin
from scripts.web_first_release import (
    PAYLOADS, REQUIRED_RUNTIME_CHECKS, REQUIRED_WEB_CHECKS, SCHEMA, digest, verify,
)

SHA = "a" * 40


def candidate(root: Path):
    root.mkdir(exist_ok=True)
    for name in PAYLOADS:
        (root / name).write_bytes(b"Test payload; never actual runtime evidence")
    for name, required in (("acceptance.json", REQUIRED_RUNTIME_CHECKS), ("browser.json", REQUIRED_WEB_CHECKS)):
        (root / name).write_text(json.dumps({
            "ok": True, "checkout_sha": SHA, "run_id": "123", "browser_errors": [],
            "customer_ga": False, "persistent_deployment": False,
            "checks": [{"name": check, "ok": True} for check in sorted(required)],
        }))
    manifest = {"schema": SCHEMA, "scope": "web-only", "checkout_sha": SHA, "run_id": "123",
                "web_image_id": "sha256:" + "b" * 64, "customer_ga": False,
                "persistent_deployment": False, "native_distribution": [],
                "artifacts": [{"name": name, "bytes": (root / name).stat().st_size,
                               "sha256": digest(root / name)} for name in sorted(PAYLOADS)]}
    write_manifest(root, manifest)
    return manifest


def write_manifest(root, manifest):
    (root / "web-candidate.json").write_text(json.dumps(manifest))


def test_candidate_requires_exact_checkout_and_immutable_payload(tmp_path):
    candidate(tmp_path)
    assert verify(tmp_path, SHA)["customer_ga"] is False
    with pytest.raises(ValueError, match="SHA mismatch"):
        verify(tmp_path, "c" * 40)
    (tmp_path / "runtime-source.tar.gz").write_bytes(b"altered")
    with pytest.raises(ValueError, match="digest/size"):
        verify(tmp_path, SHA)


@pytest.mark.parametrize("mutation", ["native", "missing", "duplicate", "traversal", "false-ga", "live-claim"])
def test_candidate_rejects_scope_and_artifact_confusion(tmp_path, mutation):
    manifest = candidate(tmp_path)
    if mutation == "native":
        manifest["native_distribution"] = ["windows"]
    elif mutation == "missing":
        manifest["artifacts"].pop()
    elif mutation == "duplicate":
        manifest["artifacts"][0] = manifest["artifacts"][1]
    elif mutation == "traversal":
        manifest["artifacts"][0]["name"] = "../private-key"
    elif mutation == "false-ga":
        manifest["customer_ga"] = True
    else:
        manifest["persistent_deployment"] = True
    write_manifest(tmp_path, manifest)
    with pytest.raises(ValueError):
        verify(tmp_path, SHA)


def test_candidate_rejects_symlink_even_with_matching_contents(tmp_path):
    candidate(tmp_path)
    file = tmp_path / "web-image.tar.gz"
    other = tmp_path / "other"
    file.rename(other)
    file.symlink_to(other)
    with pytest.raises(ValueError, match="regular file"):
        verify(tmp_path, SHA)


@pytest.mark.parametrize("change", ["omitted-behavior", "failed-check", "mixed-run", "browser-error"])
def test_candidate_rejects_incomplete_evidence_even_when_rehashed(tmp_path, change):
    manifest = candidate(tmp_path)
    name = "acceptance.json" if change == "mixed-run" else "browser.json"
    report = json.loads((tmp_path / name).read_text())
    if change == "omitted-behavior":
        report["checks"].pop()
    elif change == "failed-check":
        report["checks"][0]["ok"] = False
    elif change == "mixed-run":
        report["run_id"] = "456"
    else:
        report["browser_errors"] = ["Hydration failed"]
    (tmp_path / name).write_text(json.dumps(report))
    for item in manifest["artifacts"]:
        item.update(sha256=digest(tmp_path / item["name"]), bytes=(tmp_path / item["name"]).stat().st_size)
    write_manifest(tmp_path, manifest)
    with pytest.raises(ValueError):
        verify(tmp_path, SHA)


@pytest.mark.parametrize("origin", ["http://live.example.com", "https://localhost", "https://host.invalid",
                                    "https://live.example.com/path", "https://user:secret@live.example.com",
                                    "https://live.example.com:3000"])
def test_host_refuses_insecure_or_placeholder_public_ingress(origin):
    with pytest.raises(ValueError):
        validate_origin(origin)


def test_host_accepts_https_origin_and_preserves_existing_secrets(tmp_path, monkeypatch):
    assert validate_origin("https://live.example.com/") == "https://live.example.com"
    monkeypatch.setattr("scripts.web_first_host.os.chown", lambda *_: None)
    path = tmp_path / "runtime.env"
    private_write(path, "original", user_id=1001, group_id=1001)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_write(path, "replacement", user_id=1001, group_id=1001)
    assert path.read_text() == "original"


def test_restore_never_targets_original_or_nonempty_database(tmp_path, monkeypatch):
    args = SimpleNamespace(source_dsn_env="SOURCE", target_dsn_env="TARGET", backup=tmp_path / "backup")
    monkeypatch.setenv("SOURCE", "postgresql://actor:secret@host/production")
    monkeypatch.setenv("TARGET", "postgresql://actor:secret@other/production")
    with pytest.raises(ValueError, match="separately named"):
        restore(args)
    monkeypatch.setenv("TARGET", "postgresql://actor:secret@host/recovery")
    args.backup.write_bytes(b"backup fixture")
    args.backup.with_suffix(".sha256").write_text(digest(args.backup))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="4\n")

    monkeypatch.setattr("scripts.web_first_host.subprocess.run", run)
    with pytest.raises(ValueError, match="empty independent"):
        restore(args)
    assert len(calls) == 1
    assert "secret" not in str(calls)
