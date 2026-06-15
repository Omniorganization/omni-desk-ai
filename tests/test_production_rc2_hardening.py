from __future__ import annotations

import hmac
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from omnidesk_agent.appsync.store import AppSyncStore, _device_signing_message

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_expression_hygiene_script_passes() -> None:
    import subprocess, sys
    subprocess.run([sys.executable, "scripts/check_workflow_expressions.py", "."], cwd=ROOT, check=True)


def test_device_challenge_uses_asymmetric_signature(tmp_path: Path) -> None:
    store = AppSyncStore(tmp_path / "appsync.json")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo).decode("utf-8")
    store.register_device(actor="owner", device_id="dev1", device_type="desktop", name="d", platform="mac", public_key=public)
    enrollment = store.start_device_enrollment(actor="owner", device_type="desktop", pairing_code="pairing-123")
    store.complete_device_enrollment(actor="owner", enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-123", device_id="dev1", public_key=public)
    challenge = store.issue_device_challenge(actor="owner", enrollment_id=enrollment["enrollment_id"], device_id="dev1")
    signature = private.sign(challenge["signing_message"].encode("utf-8")).hex()
    verified = store.verify_device_challenge(actor="owner", enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id="dev1", signature=signature)
    assert verified["trust_level"] == "challenge_verified"


def test_legacy_hmac_challenge_requires_explicit_prefix(tmp_path: Path) -> None:
    store = AppSyncStore(tmp_path / "appsync.json")
    public_key = "legacy-hmac:secret"
    store.register_device(actor="owner", device_id="dev1", device_type="desktop", name="d", platform="mac", public_key=public_key)
    enrollment = store.start_device_enrollment(actor="owner", device_type="desktop", pairing_code="pairing-123")
    store.complete_device_enrollment(actor="owner", enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-123", device_id="dev1", public_key=public_key)
    challenge = store.issue_device_challenge(actor="owner", enrollment_id=enrollment["enrollment_id"], device_id="dev1")
    message = challenge["signing_message"].encode("utf-8")
    signature = hmac.new(b"secret", message, hashlib.sha256).hexdigest()
    assert store.verify_device_challenge(actor="owner", enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id="dev1", signature=signature)["device_id"] == "dev1"


def test_web_admin_business_api_uses_server_proxy_only() -> None:
    api = (ROOT / "apps/web-admin-next/lib/api.ts").read_text(encoding="utf-8")
    assert "authorization: `Bearer" not in api
    assert "${session.baseUrl" not in api
    assert "/api/omni/bootstrap" in api


def test_postgres_store_declares_transactional_claim_and_lease() -> None:
    text = (ROOT / "omnidesk_agent/appsync/postgres_store.py").read_text(encoding="utf-8")
    assert "FOR UPDATE SKIP LOCKED" in text
    assert "def claim_next_task" in text and "CLAIM_NEXT_TASK_SQL" in text
    assert "def renew_task_lease" in text and "LEASE_RENEWAL_SQL" in text


def test_desktop_shell_sandbox_executor_has_workspace_policy() -> None:
    executor = (ROOT / "apps/desktop-tauri/src/executor.ts").read_text(encoding="utf-8")
    main_rs = (ROOT / "apps/desktop-tauri/src-tauri/src/main.rs").read_text(encoding="utf-8")
    assert "run_workspace_command" in executor
    assert "workspace_only" in executor
    assert "OmniDesktopWorkspace" in main_rs
