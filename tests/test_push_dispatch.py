"""Pushes go out automatically, once per lease, with retries, and are given up honestly.

Before this, a queued push carried no push target, so the Firebase provider
failed every item with "push token is required" and nothing was ever
delivered. Pushes went out only when an owner called POST /app/push/dispatch,
two dispatchers could send the same push, every failure was final, a target
FCM had unregistered kept being used, and a dry run was recorded as "sent".
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient

from omnidesk_agent.appsync import push as push_module
from omnidesk_agent.appsync import routes
from omnidesk_agent.appsync.push import (
    DryRunPushProvider,
    FcmHttpV1Client,
    FirebasePushProvider,
    PushDeliveryResult,
    PushRetryPolicy,
    PushSendError,
    classify_fcm_error,
    dispatch_pending_push,
)
from omnidesk_agent.appsync.strict_json_store import StrictJsonAppSyncStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app

OWNER = "olivia-owner"


class Key:
    """A P-256 device key that signs like the clients do (raw r||s, base64)."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, message: str) -> str:
        r, s = decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        return "base64:" + base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


class RecordingProvider:
    """Plays back scripted provider answers and records what it was asked to send."""

    name = "firebase"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    def send(self, item: dict[str, Any]) -> PushDeliveryResult:
        self.sent.append(item)
        answer = self.answers.pop(0) if self.answers else "ok"
        push_id = str(item["push_id"])
        if answer == "ok":
            return PushDeliveryResult(push_id=push_id, ok=True, provider=self.name, provider_message_id=f"projects/p/messages/{len(self.sent)}")
        if answer == "unavailable":
            return PushDeliveryResult(push_id=push_id, ok=False, provider=self.name, error="FCM send failed: HTTP 503 UNAVAILABLE", retryable=True)
        if answer == "unregistered":
            return PushDeliveryResult(push_id=push_id, ok=False, provider=self.name, error="FCM send failed: HTTP 404 UNREGISTERED", invalid_target=True)
        return PushDeliveryResult(push_id=push_id, ok=False, provider=self.name, error="FCM send failed: HTTP 400 INVALID_ARGUMENT")


def _postgres(prefix: str) -> tuple[Any, Any]:
    dsn = os.getenv("OMNIDESK_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("OMNIDESK_TEST_POSTGRES_DSN is not configured")
    from omnidesk_agent.appsync.migrated_postgres_store import MigratedMultiInstancePostgresAppSyncStore
    from omnidesk_agent.appsync.postgres_migrations import apply_appsync_migrations

    namespace = f"{prefix}_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    return (
        MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=3),
        MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=3),
    )


@pytest.fixture(params=["json", "postgres"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    """Two handles on one AppSync state: two replicas on PostgreSQL."""
    if request.param == "json":
        store = StrictJsonAppSyncStore(tmp_path / "appsync.json")
        yield store, store
        return
    first, second = _postgres("push")
    try:
        yield first, second
    finally:
        first.close()
        second.close()


@pytest.fixture
def store(stores: tuple[Any, Any]) -> Any:
    return stores[0]


def _phone(store: Any, *, device_id: str = "dev_phone", token: str = "fcm-token-1") -> None:
    key = Key()
    store.ensure_user(OWNER)
    store.register_device(actor=OWNER, device_id=device_id, device_type="mobile", name="phone", platform="android", public_key=key.public_pem)
    enrollment = store.start_device_enrollment(actor=OWNER, device_type="mobile", pairing_code="pairing-code-123")
    store.complete_device_enrollment(actor=OWNER, enrollment_id=enrollment["enrollment_id"], pairing_code="pairing-code-123", device_id=device_id, public_key=key.public_pem)
    challenge = store.issue_device_challenge(actor=OWNER, enrollment_id=enrollment["enrollment_id"], device_id=device_id)
    store.verify_device_challenge(actor=OWNER, enrollment_id=enrollment["enrollment_id"], challenge_id=challenge["challenge_id"], device_id=device_id, signature=key.sign(challenge["signing_message"]))
    store.register_push_token(actor=OWNER, device_id=device_id, push_token=token, platform="android")


def _approval_request(store: Any) -> str:
    conversation = store.create_conversation(actor=OWNER, title="ops")
    created = store.add_message_and_task(actor=OWNER, conversation_id=conversation["conversation_id"], content="deploy the release", requires_desktop_runtime=True)
    return str(created["approval"]["approval_id"])


def _row(store: Any, push_id: str) -> dict[str, Any]:
    if hasattr(store, "push_outbox"):
        return asdict(store.push_outbox[push_id])
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempt_count, last_error, next_attempt_at, provider, provider_message_id, claimed_by, lease_expires_at FROM omnidesk_appsync_push_outbox WHERE namespace = %s AND push_id = %s",
            (store.namespace, push_id),
        )
        names = ("status", "attempt_count", "last_error", "next_attempt_at", "provider", "provider_message_id", "claimed_by", "lease_expires_at")
        return dict(zip(names, cur.fetchone()))


def _push_ids(store: Any) -> list[str]:
    if hasattr(store, "push_outbox"):
        return sorted(store.push_outbox)
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT push_id FROM omnidesk_appsync_push_outbox WHERE namespace = %s ORDER BY push_id", (store.namespace,))
        return [row[0] for row in cur.fetchall()]


def _time_passes(store: Any, push_id: str, *, retry: bool = False, lease: bool = False, age: float = 0.0) -> None:
    """Moves a push's clock fields into the past, as if time had passed."""
    past = time.time() - 1
    if hasattr(store, "push_outbox"):
        item = store.push_outbox[push_id]
        if retry:
            item.next_attempt_at = past
        if lease:
            item.lease_expires_at = past
        item.created_at -= age
        return
    with store._connect() as conn, conn.cursor() as cur:
        if retry:
            cur.execute("UPDATE omnidesk_appsync_push_outbox SET next_attempt_at = %s WHERE namespace = %s AND push_id = %s", (past, store.namespace, push_id))
        if lease:
            cur.execute("UPDATE omnidesk_appsync_push_outbox SET lease_expires_at = %s WHERE namespace = %s AND push_id = %s", (past, store.namespace, push_id))
        if age:
            cur.execute("UPDATE omnidesk_appsync_push_outbox SET created_at = created_at - %s WHERE namespace = %s AND push_id = %s", (age, store.namespace, push_id))


def _device_push_token(store: Any, device_id: str) -> Any:
    if hasattr(store, "devices"):
        return store.devices[device_id].push_token
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT push_token FROM omnidesk_appsync_devices WHERE namespace = %s AND device_id = %s", (store.namespace, device_id))
        return cur.fetchone()[0]


# ------------------------------------------------------------------ delivery


def test_the_firebase_provider_gets_the_target_and_the_routing_data(tmp_path: Path) -> None:
    # On main the queued item had no push_token: this send failed with
    # "push token is required" and the FCM client was never called.
    class FakeFcm:
        calls: list[tuple[str, str, dict[str, str], dict[str, str]]] = []

        def send(self, kind: str, target: str, notification: dict[str, str], data: dict[str, str]) -> str:
            self.calls.append((kind, target, notification, data))
            return "projects/p/messages/1"

    store = StrictJsonAppSyncStore(tmp_path / "appsync.json")
    _phone(store)
    approval_id = _approval_request(store)
    fcm = FakeFcm()
    results = dispatch_pending_push(store, FirebasePushProvider(http_client=fcm))  # type: ignore[arg-type]
    assert [(r.ok, r.error) for r in results] == [(True, None)]
    kind, target, notification, data = fcm.calls[0]
    assert (kind, target) == ("fcm_token", "fcm-token-1")
    assert notification["title"] == "Omni approval required"
    assert data["event_type"] == "approval.requested" and data["related_id"] == approval_id


def test_a_push_goes_once_to_the_devices_current_target(store: Any) -> None:
    _phone(store)
    approval_id = _approval_request(store)
    # FCM re-registered the install after the push was queued.
    store.register_push_token(actor=OWNER, device_id="dev_phone", push_token="fcm-token-2", platform="android")
    provider = RecordingProvider()
    results = dispatch_pending_push(store, provider)
    assert [r.ok for r in results] == [True]
    item = provider.sent[0]
    assert item["push_token"] == "fcm-token-2" and item["event_type"] == "approval.requested" and item["related_id"] == approval_id
    row = _row(store, item["push_id"])
    assert (row["status"], row["attempt_count"], row["provider"], row["provider_message_id"], row["claimed_by"]) == ("sent", 1, "firebase", "projects/p/messages/1", None)
    assert dispatch_pending_push(store, provider) == [] and len(provider.sent) == 1


def test_a_dry_run_is_recorded_as_a_dry_run_not_as_sent(store: Any) -> None:
    _phone(store)
    _approval_request(store)
    [result] = dispatch_pending_push(store, DryRunPushProvider())
    row = _row(store, result.push_id)
    assert (row["status"], row["provider"]) == ("dry_run", "dry_run")


def test_a_retryable_failure_backs_off_then_delivers(store: Any) -> None:
    _phone(store)
    _approval_request(store)
    provider = RecordingProvider("unavailable", "ok")
    before = time.time()
    [first] = dispatch_pending_push(store, provider)
    row = _row(store, first.push_id)
    assert (row["status"], row["attempt_count"]) == ("pending", 1) and "503" in row["last_error"]
    assert row["next_attempt_at"] >= before + PushRetryPolicy().base_delay_seconds
    # Not before the backoff has passed.
    assert dispatch_pending_push(store, provider) == []
    _time_passes(store, first.push_id, retry=True)
    [second] = dispatch_pending_push(store, provider)
    assert second.ok and _row(store, first.push_id)["status"] == "sent" and _row(store, first.push_id)["attempt_count"] == 2


def test_backoff_doubles_up_to_the_cap() -> None:
    policy = PushRetryPolicy()
    assert [policy.retry_at(attempt, 0.0) for attempt in (1, 2, 3, 4)] == [10.0, 20.0, 40.0, 80.0]
    assert policy.retry_at(20, 0.0) == policy.max_delay_seconds


def test_a_push_is_given_up_after_max_attempts(store: Any) -> None:
    _phone(store)
    _approval_request(store)
    policy = PushRetryPolicy(max_attempts=2)
    provider = RecordingProvider("unavailable", "unavailable")
    [first] = dispatch_pending_push(store, provider, policy=policy)
    _time_passes(store, first.push_id, retry=True)
    dispatch_pending_push(store, provider, policy=policy)
    row = _row(store, first.push_id)
    assert (row["status"], row["attempt_count"]) == ("failed", 2) and "503" in row["last_error"]
    _time_passes(store, first.push_id, retry=True)
    assert dispatch_pending_push(store, provider, policy=policy) == [] and len(provider.sent) == 2


def test_a_permanent_failure_is_not_retried(store: Any) -> None:
    _phone(store)
    _approval_request(store)
    [result] = dispatch_pending_push(store, RecordingProvider("invalid"))
    assert _row(store, result.push_id)["status"] == "failed"


def test_an_unregistered_target_is_removed_from_the_device(stores: tuple[Any, Any]) -> None:
    store, _ = stores
    _phone(store)
    _approval_request(store)
    [result] = dispatch_pending_push(store, RecordingProvider("unregistered"))
    assert _row(store, result.push_id)["status"] == "failed"
    assert _device_push_token(store, "dev_phone") is None
    # Until the app registers again, nothing is queued for it.
    _approval_request(store)
    assert dispatch_pending_push(store, RecordingProvider()) == []


def test_a_target_registered_meanwhile_is_kept(store: Any) -> None:
    _phone(store)
    _approval_request(store)
    [item] = store.claim_push_deliveries(worker_id="w1")
    store.register_push_token(actor=OWNER, device_id="dev_phone", push_token="fcm-token-new", platform="android")
    store.complete_push_delivery(push_id=item["push_id"], worker_id="w1", outcome="failed", provider="firebase", error="UNREGISTERED", invalid_target=item["push_token"])
    assert _device_push_token(store, "dev_phone") == "fcm-token-new"


# -------------------------------------------------------------------- leases


def test_concurrent_dispatchers_never_send_the_same_push(stores: tuple[Any, Any]) -> None:
    first, second = stores
    _phone(first)
    for _ in range(6):
        _approval_request(first)
    barrier = threading.Barrier(2)
    claimed: dict[str, list[str]] = {}

    def claim(name: str, store: Any) -> None:
        barrier.wait()
        claimed[name] = [item["push_id"] for item in store.claim_push_deliveries(worker_id=name, limit=6)]

    threads = [threading.Thread(target=claim, args=(name, store)) for name, store in (("a", first), ("b", second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not set(claimed["a"]) & set(claimed["b"])
    assert sorted(claimed["a"] + claimed["b"]) == _push_ids(first)


def test_an_expired_lease_is_taken_over_and_the_old_holder_is_ignored(stores: tuple[Any, Any]) -> None:
    first, second = stores
    _phone(first)
    _approval_request(first)
    [item] = first.claim_push_deliveries(worker_id="crashed", lease_seconds=60)
    assert second.claim_push_deliveries(worker_id="b") == []
    _time_passes(first, item["push_id"], lease=True)
    [again] = second.claim_push_deliveries(worker_id="b")
    assert again["push_id"] == item["push_id"] and again["attempt_count"] == 2
    # The first dispatcher's late report does not overwrite the new holder's lease.
    assert first.complete_push_delivery(push_id=item["push_id"], worker_id="crashed", outcome="sent", provider="firebase") is None
    assert _row(first, item["push_id"])["claimed_by"] == "b"
    assert second.complete_push_delivery(push_id=item["push_id"], worker_id="b", outcome="sent", provider="firebase")["status"] == "sent"


def test_a_push_whose_dispatcher_keeps_dying_is_given_up(store: Any) -> None:
    _phone(store)
    _approval_request(store)
    [item] = store.claim_push_deliveries(worker_id="w1", max_attempts=1)
    _time_passes(store, item["push_id"], lease=True)
    assert store.claim_push_deliveries(worker_id="w2", max_attempts=1) == []
    row = _row(store, item["push_id"])
    assert (row["status"], row["last_error"]) == ("failed", "max_attempts")


def test_old_and_ineligible_pushes_are_given_up(store: Any) -> None:
    _phone(store)
    _phone(store, device_id="dev_lost", token="fcm-lost")
    _approval_request(store)
    by_device = {item["device_id"]: item["push_id"] for item in store.pending_push_outbox()}
    store.revoke_device(actor=OWNER, device_id="dev_lost", reason="lost")
    _time_passes(store, by_device["dev_phone"], age=2 * 86_400)
    assert store.claim_push_deliveries(worker_id="w1") == []
    assert _row(store, by_device["dev_phone"])["last_error"] == "expired"
    assert _row(store, by_device["dev_lost"])["status"] == "failed"


# --------------------------------------------------------------------- FCM


def test_fcm_errors_are_classified() -> None:
    unregistered = classify_fcm_error(404, "UNREGISTERED")
    assert unregistered.invalid_target and not unregistered.retryable
    assert classify_fcm_error(403, "SENDER_ID_MISMATCH").invalid_target
    for status, code in ((503, "UNAVAILABLE"), (429, "QUOTA_EXCEEDED"), (500, "INTERNAL"), (401, "THIRD_PARTY_AUTH_ERROR")):
        error = classify_fcm_error(status, code)
        assert error.retryable and not error.invalid_target, code
    invalid = classify_fcm_error(400, "INVALID_ARGUMENT")
    assert not invalid.retryable and not invalid.invalid_target


def test_the_fcm_client_reports_an_unregistered_token() -> None:
    class Response:
        def __init__(self, status_code: int, body: dict[str, Any]) -> None:
            self.status_code, self._body = status_code, body

        def json(self) -> dict[str, Any]:
            return self._body

    class FakeHttp:
        def post(self, url: str, **_: Any) -> Response:
            if url == push_module.GOOGLE_TOKEN_URI:
                return Response(200, {"access_token": "t", "expires_in": 3600})
            return Response(404, {"error": {"status": "UNREGISTERED"}})

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    client = FcmHttpV1Client({"client_email": "push@p.iam.gserviceaccount.com", "private_key": private_key, "project_id": "omni-push-test"}, http=FakeHttp())
    with pytest.raises(PushSendError) as raised:
        client.send("fcm_token", "stale", {"title": "t", "body": "b"}, {})
    assert raised.value.invalid_target and not raised.value.retryable
    result = FirebasePushProvider(http_client=client).send({"push_id": "push_1", "push_token": "stale"})
    assert (result.ok, result.invalid_target, result.retryable) == (False, True, False)


# ------------------------------------------------------------------ gateway


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


def _post(client: TestClient, path: str, payload: dict[str, Any], *, key: Key | None = None, device_id: str = "") -> Any:
    body = json.dumps(payload).encode()
    headers = {"authorization": "Bearer owner-token", "x-omnidesk-actor": OWNER, "content-type": "application/json", "idempotency-key": secrets.token_hex(8)}
    if key is not None:
        timestamp, nonce = str(int(time.time() * 1000)), secrets.token_hex(24)
        message = f"omnidesk-device-request:v1:POST:{path}:{hashlib.sha256(body).hexdigest()}:{timestamp}:{nonce}"
        headers.update({"x-omnidesk-device-id": device_id, "x-omnidesk-timestamp": timestamp, "x-omnidesk-nonce": nonce, "x-omnidesk-device-signature": key.sign(message)})
    return client.post(path, content=body, headers=headers)


def test_the_gateway_pushes_an_approval_request_without_being_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("OMNIDESK_PUSH_DISPATCH_INTERVAL_SECONDS", "0.05")
    monkeypatch.setattr(routes, "_device_signature_enforced", lambda _cfg: True)
    provider = RecordingProvider()
    monkeypatch.setattr(push_module, "provider_from_env", lambda: provider)
    phone, device_id = Key(), f"mob_{secrets.token_hex(18)}"
    with TestClient(create_app(_cfg(tmp_path))) as client:
        dispatcher = client.app.state.push_dispatcher  # type: ignore[attr-defined]
        try:
            assert _post(client, "/app/devices/register", {"device_id": device_id, "device_type": "mobile", "name": "phone", "platform": "android", "public_key": phone.public_pem}).status_code == 200
            enrollment = _post(client, "/app/devices/enrollment/start", {"device_type": "mobile", "pairing_code": "pairing-code-123"}).json()["enrollment"]
            _post(client, f"/app/devices/enrollment/{enrollment['enrollment_id']}/complete", {"pairing_code": "pairing-code-123", "device_id": device_id, "public_key": phone.public_pem})
            challenge = _post(client, f"/app/devices/enrollment/{enrollment['enrollment_id']}/challenge", {"device_id": device_id}).json()["challenge"]
            verified = _post(client, f"/app/devices/enrollment/{enrollment['enrollment_id']}/verify", {"challenge_id": challenge["challenge_id"], "device_id": device_id, "signature": phone.sign(challenge["signing_message"])})
            assert verified.status_code == 200, verified.text
            path = f"/app/devices/{device_id}/push-token"
            assert _post(client, path, {"push_token": "fcm-token-1", "platform": "android"}, key=phone, device_id=device_id).status_code == 200

            conversation = _post(client, "/app/conversations", {"title": "ops"}).json()["conversation"]
            created = _post(client, f"/app/conversations/{conversation['conversation_id']}/messages", {"content": "deploy the release", "requires_desktop_runtime": True}).json()
            deadline = time.time() + 10
            while not provider.sent and time.time() < deadline:
                time.sleep(0.02)
            assert provider.sent, "no push was dispatched"
            item = provider.sent[0]
            assert (item["push_token"], item["event_type"], item["related_id"]) == ("fcm-token-1", "approval.requested", created["approval"]["approval_id"])
            deadline = time.time() + 5
            while _row(dispatcher.store, item["push_id"])["status"] != "sent" and time.time() < deadline:
                time.sleep(0.02)
            assert _row(dispatcher.store, item["push_id"])["status"] == "sent"
        finally:
            dispatcher.stop()


def test_without_a_push_provider_nothing_is_dispatched_automatically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIDESK_PUSH_PROVIDER", raising=False)
    monkeypatch.setenv("OMNIDESK_OWNER_TOKEN", "owner-token")
    with TestClient(create_app(_cfg(tmp_path))) as client:
        # A dry run would mark pushes delivered that never reached a device.
        assert client.app.state.push_dispatcher is None  # type: ignore[attr-defined]
