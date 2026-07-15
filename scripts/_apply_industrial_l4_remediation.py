from __future__ import annotations

import json
from pathlib import Path

ROOT = Path('.')


def replace_once(path: str, old: str, new: str) -> None:
    p = ROOT / path
    text = p.read_text(encoding='utf-8')
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{path}: expected exactly one match, found {count}: {old[:80]!r}')
    p.write_text(text.replace(old, new, 1), encoding='utf-8')


def append_once(path: str, marker: str, addition: str) -> None:
    p = ROOT / path
    text = p.read_text(encoding='utf-8')
    if addition.strip() in text:
        return
    if marker not in text:
        raise RuntimeError(f'{path}: marker missing: {marker!r}')
    p.write_text(text.replace(marker, marker + addition, 1), encoding='utf-8')


replace_once(
    'omnidesk_agent/appsync/chat_repository.py',
    '    def __init__(self, store: Any, *, lease_seconds: int = 180) -> None:\n',
    '    def __init__(\n        self,\n        store: Any,\n        *,\n        lease_seconds: int = 180,\n        allow_implicit_provisioning: bool = True,\n    ) -> None:\n',
)
replace_once(
    'omnidesk_agent/appsync/chat_repository.py',
    '        self.lease_seconds = max(30, min(int(lease_seconds), 1800))\n',
    '        self.lease_seconds = max(30, min(int(lease_seconds), 1800))\n        self.allow_implicit_provisioning = bool(allow_implicit_provisioning)\n',
)
replace_once(
    'omnidesk_agent/appsync/chat_repository.py',
    '            row = cur.fetchone()\n            return str(row[0] if row else "org_default")\n',
    '            row = cur.fetchone()\n            if row:\n                return str(row[0])\n            if not self.allow_implicit_provisioning:\n                raise PermissionError("identity_not_provisioned")\n            return "org_default"\n',
)
replace_once(
    'omnidesk_agent/appsync/chat_repository.py',
    '            organization_id = str(row[0] if row else "org_default")\n            if not row:\n',
    '            organization_id = str(row[0] if row else "org_default")\n            if not row and not self.allow_implicit_provisioning:\n                raise PermissionError("identity_not_provisioned")\n            if not row:\n',
)

replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    'class ChatLeaseLost(RuntimeError):\n    """A stale worker attempted to mutate a request it no longer owns."""\n\n\nclass PostgresChatRepository',
    'class ChatLeaseLost(RuntimeError):\n    """A stale worker attempted to mutate a request it no longer owns."""\n\n\nclass ChatEventSequenceConflict(RuntimeError):\n    """A stream sequence was reused with different event content."""\n\n\nclass PostgresChatRepository',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '''    def _lock_owned_request(self, cur: Any, reservation: ChatReservation) -> str:\n        cur.execute(\n            "SELECT status,lease_owner FROM omnidesk_appsync_chat_requests "\n            "WHERE namespace=%s AND organization_id=%s AND actor=%s "\n            "AND endpoint=%s AND idempotency_key=%s FOR UPDATE",\n            (\n                reservation.namespace,\n                reservation.organization_id,\n                reservation.actor,\n                reservation.endpoint,\n                reservation.idempotency_key,\n            ),\n        )\n        row = cur.fetchone()\n        if not row:\n            raise ChatLeaseLost("chat request no longer exists")\n        if str(row[0]) not in ACTIVE or row[1] != reservation.lease_owner:\n            raise ChatLeaseLost("chat request lease is no longer owned")\n        return str(row[0])\n''',
    '''    def _lock_owned_request(self, cur: Any, reservation: ChatReservation) -> str:\n        cur.execute(\n            "UPDATE omnidesk_appsync_chat_requests "\n            "SET lease_expires_at=EXTRACT(EPOCH FROM clock_timestamp())+%s,"\n            "updated_at=EXTRACT(EPOCH FROM clock_timestamp()) "\n            "WHERE namespace=%s AND organization_id=%s AND actor=%s "\n            "AND endpoint=%s AND idempotency_key=%s "\n            "AND status IN ('reserved','running','finalizing') "\n            "AND lease_owner=%s "\n            "AND lease_expires_at>EXTRACT(EPOCH FROM clock_timestamp()) "\n            "RETURNING status",\n            (\n                self.lease_seconds,\n                reservation.namespace,\n                reservation.organization_id,\n                reservation.actor,\n                reservation.endpoint,\n                reservation.idempotency_key,\n                reservation.lease_owner,\n            ),\n        )\n        row = cur.fetchone()\n        if not row:\n            raise ChatLeaseLost("chat request lease is expired or no longer owned")\n        return str(row[0])\n\n    def renew_lease(self, reservation: ChatReservation) -> None:\n        with self._connect() as conn, conn.cursor() as cur:\n            self._lock_owned_request(cur, reservation)\n            conn.commit()\n''',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '''            cur.execute(\n                "INSERT INTO omnidesk_appsync_chat_stream_events"\n                "(namespace,organization_id,actor,endpoint,idempotency_key,"\n                "sequence,event_type,payload,created_at) "\n                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "\n                "ON CONFLICT DO NOTHING",\n                (\n                    reservation.namespace,\n                    reservation.organization_id,\n                    reservation.actor,\n                    reservation.endpoint,\n                    reservation.idempotency_key,\n                    sequence,\n                    event,\n                    Jsonb(data),\n                    now,\n                ),\n            )\n            terminal = status in TERMINAL\n''',
    '''            cur.execute(\n                "INSERT INTO omnidesk_appsync_chat_stream_events"\n                "(namespace,organization_id,actor,endpoint,idempotency_key,"\n                "sequence,event_type,payload,created_at) "\n                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "\n                "ON CONFLICT DO NOTHING RETURNING sequence",\n                (\n                    reservation.namespace,\n                    reservation.organization_id,\n                    reservation.actor,\n                    reservation.endpoint,\n                    reservation.idempotency_key,\n                    sequence,\n                    event,\n                    Jsonb(data),\n                    now,\n                ),\n            )\n            inserted = cur.fetchone()\n            if not inserted:\n                cur.execute(\n                    "SELECT event_type,payload "\n                    "FROM omnidesk_appsync_chat_stream_events "\n                    "WHERE namespace=%s AND organization_id=%s AND actor=%s "\n                    "AND endpoint=%s AND idempotency_key=%s AND sequence=%s",\n                    (\n                        reservation.namespace,\n                        reservation.organization_id,\n                        reservation.actor,\n                        reservation.endpoint,\n                        reservation.idempotency_key,\n                        sequence,\n                    ),\n                )\n                existing = cur.fetchone()\n                existing_payload = existing[1] if existing and isinstance(existing[1], dict) else {}\n                if not existing or str(existing[0]) != event or existing_payload != data:\n                    raise ChatEventSequenceConflict(\n                        f"stream sequence {sequence} was reused with different content"\n                    )\n            terminal = status in TERMINAL\n''',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                "SET status=%s,last_sequence=GREATEST(last_sequence,%s),"\n                "lease_owner=%s,lease_expires_at=%s,updated_at=%s "\n',
    '                "SET status=%s,last_sequence=GREATEST(last_sequence,%s),"\n                "lease_owner=%s,"\n                "lease_expires_at=CASE WHEN %s THEN NULL ELSE EXTRACT(EPOCH FROM clock_timestamp())+%s END,"\n                "updated_at=EXTRACT(EPOCH FROM clock_timestamp()) "\n',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                    None if terminal else reservation.lease_owner,\n                    None if terminal else now + self.lease_seconds,\n                    now,\n                    reservation.namespace,\n',
    '                    None if terminal else reservation.lease_owner,\n                    terminal,\n                    self.lease_seconds,\n                    reservation.namespace,\n',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                "SET status=%s,response=%s,lease_owner=%s,"\n                "lease_expires_at=%s,updated_at=%s "\n',
    '                "SET status=%s,response=%s,lease_owner=%s,"\n                "lease_expires_at=CASE WHEN %s THEN NULL ELSE EXTRACT(EPOCH FROM clock_timestamp())+%s END,"\n                "updated_at=EXTRACT(EPOCH FROM clock_timestamp()) "\n',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                    Jsonb(result),\n                    None if terminal else reservation.lease_owner,\n                    None if terminal else now + self.lease_seconds,\n                    now,\n                    reservation.namespace,\n',
    '                    Jsonb(result),\n                    None if terminal else reservation.lease_owner,\n                    terminal,\n                    self.lease_seconds,\n                    reservation.namespace,\n',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                "SET status=%s,error=%s,lease_owner=NULL,"\n                "lease_expires_at=NULL,updated_at=%s "\n',
    '                "SET status=%s,error=%s,lease_owner=NULL,"\n                "lease_expires_at=NULL,updated_at=EXTRACT(EPOCH FROM clock_timestamp()) "\n',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '                    status,\n                    Jsonb(error),\n                    time.time(),\n                    reservation.namespace,\n',
    '                    status,\n                    Jsonb(error),\n                    reservation.namespace,\n',
)
replace_once(
    'omnidesk_agent/appsync/lease_safe_chat_repository.py',
    '    "ChatLeaseLost",\n',
    '    "ChatEventSequenceConflict",\n    "ChatLeaseLost",\n',
)

replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    'import logging\nimport os\nfrom contextvars import ContextVar\n',
    'import asyncio\nimport logging\nimport os\nfrom contextlib import suppress\nfrom contextvars import ContextVar\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    'from omnidesk_agent.models.base import ModelRequest, ModelResponse\n',
    'from omnidesk_agent.models.base import ModelRequest, ModelResponse\nfrom omnidesk_agent.validation.production import is_production_mode\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    '                self.store,\n                lease_seconds=_lease_seconds(self.cfg),\n            )\n',
    '                self.store,\n                lease_seconds=_lease_seconds(self.cfg),\n                allow_implicit_provisioning=not is_production_mode(self.cfg),\n            )\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    '        self._active: ContextVar[ChatReservation | None] = ContextVar(\n            "omnidesk_chat_reservation", default=None\n        )\n\n    def _preflight(\n',
    '        self._active: ContextVar[ChatReservation | None] = ContextVar(\n            "omnidesk_chat_reservation", default=None\n        )\n\n    async def _lease_heartbeat(self, reservation: ChatReservation) -> None:\n        repository = self.atomic_repository\n        if repository is None:\n            return\n        interval = max(5.0, min(float(repository.lease_seconds) / 3.0, 60.0))\n        while True:\n            await asyncio.sleep(interval)\n            try:\n                await asyncio.to_thread(repository.renew_lease, reservation)\n            except ChatLeaseLost:\n                logger.warning(\n                    "chat lease heartbeat stopped after lease loss",\n                    extra={"conversation_id": reservation.conversation_id},\n                )\n                return\n\n    async def _cancel_heartbeat(self, task: asyncio.Task[None] | None) -> None:\n        if task is None:\n            return\n        task.cancel()\n        with suppress(asyncio.CancelledError):\n            await task\n\n    def _preflight(\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    '        try:\n            response = await complete(\n                self._model_request(\n                    reservation,\n                    role=role,\n                    payload=payload,\n                    content=content,\n                )\n            )\n        except Exception as exc:\n',
    '        heartbeat = asyncio.create_task(\n            self._lease_heartbeat(reservation),\n            name=f"omnidesk-chat-lease-{reservation.idempotency_key}",\n        )\n        try:\n            response = await complete(\n                self._model_request(\n                    reservation,\n                    role=role,\n                    payload=payload,\n                    content=content,\n                )\n            )\n        except Exception as exc:\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    '            logger.exception(\n                "model router failed",\n                extra={\n                    "trace_id": trace_id,\n                    "conversation_id": reservation.conversation_id,\n                },\n            )\n            raise HTTPException(502, error) from exc\n        try:\n            result = self.atomic_repository.complete(reservation, response)\n',
    '            logger.exception(\n                "model router failed",\n                extra={\n                    "trace_id": trace_id,\n                    "conversation_id": reservation.conversation_id,\n                },\n            )\n            raise HTTPException(502, error) from exc\n        finally:\n            await self._cancel_heartbeat(heartbeat)\n        try:\n            result = self.atomic_repository.complete(reservation, response)\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    '        token = (\n            self._active.set(reservation)\n            if isinstance(reservation, ChatReservation)\n            else None\n        )\n        try:\n',
    '        token = (\n            self._active.set(reservation)\n            if isinstance(reservation, ChatReservation)\n            else None\n        )\n        heartbeat = (\n            asyncio.create_task(\n                self._lease_heartbeat(reservation),\n                name=f"omnidesk-chat-stream-lease-{reservation.idempotency_key}",\n            )\n            if isinstance(reservation, ChatReservation) and not reservation.terminal\n            else None\n        )\n        try:\n',
)
replace_once(
    'omnidesk_agent/appsync/industrial_chat_service.py',
    '        finally:\n            if token is not None:\n                self._active.reset(token)\n',
    '        finally:\n            await self._cancel_heartbeat(heartbeat)\n            if token is not None:\n                self._active.reset(token)\n',
)

replace_once(
    'omnidesk_agent/appsync/__init__.py',
    'from omnidesk_agent.appsync.store import AppSyncStore\n\n\ndef register_appsync_routes(',
    'from omnidesk_agent.appsync.store import AppSyncStore\n\n\nCANONICAL_CHAT_ROUTE_KEYS = {\n    ("POST", "/app/conversations/{conversation_id}/ask"),\n    ("POST", "/api/chat"),\n    ("POST", "/api/chat/stream"),\n}\n\n\ndef _remove_shadowed_chat_routes(app: FastAPI) -> None:\n    seen: set[tuple[str, str]] = set()\n    retained = []\n    for route in app.router.routes:\n        path = str(getattr(route, "path", ""))\n        methods = set(getattr(route, "methods", set()) or set())\n        keys = {(method, path) for method in methods if (method, path) in CANONICAL_CHAT_ROUTE_KEYS}\n        if keys and any(key in seen for key in keys):\n            continue\n        retained.append(route)\n        seen.update(keys)\n    app.router.routes[:] = retained\n\n\ndef register_appsync_routes(',
)
replace_once(
    'omnidesk_agent/appsync/__init__.py',
    '    _register_appsync_routes(app, cfg, rt, metrics, admin)\n    register_project_routes(app, cfg, rt, metrics, admin)\n',
    '    _register_appsync_routes(app, cfg, rt, metrics, admin)\n    _remove_shadowed_chat_routes(app)\n    register_project_routes(app, cfg, rt, metrics, admin)\n',
)

(ROOT / 'scripts/check_unique_api_routes.py').write_text('''#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict

from omnidesk_agent.config import AppConfig
from omnidesk_agent.server import create_app


def main() -> int:
    app = create_app(AppConfig())
    owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        methods = set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            owners[(method, path)].append(str(getattr(route, "name", "unnamed")))
    duplicates = {key: names for key, names in owners.items() if len(names) > 1}
    if duplicates:
        for (method, path), names in sorted(duplicates.items()):
            print(f"duplicate API route {method} {path}: {names}")
        return 1
    print(f"verified {len(owners)} unique API method/path pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''', encoding='utf-8')

policy = {
    'schema': 'omnidesk-real-ga-evidence-policy/v1',
    'base_categories': {
        'native_build': {'label': 'true Flutter/Rust/Tauri native build', 'files': ['native-build/flutter-android-release.json', 'native-build/flutter-ios-release.json', 'native-build/tauri-desktop-release.json', 'native-build/rust-cargo-check-locked.json'], 'requires_artifact': True},
        'signed_artifacts': {'label': 'true Android/iOS/Desktop signed artifacts', 'files': ['signed-artifacts/android-signed-aab.json', 'signed-artifacts/ios-signed-ipa.json', 'signed-artifacts/desktop-macos-notarized.json', 'signed-artifacts/desktop-windows-signed.json'], 'requires_artifact': True},
        'live_branch_protection': {'label': 'true GitHub branch protection control-plane verification', 'files': ['control-plane/github-branch-protection-live.json']},
        'model_live_smoke': {'label': 'true live model Q&A smoke with audit and budget ledger evidence', 'files': ['model/model-live-smoke.json']},
        'bigseller_live_smoke': {'label': 'true BigSeller staging smoke with auth, data, webhook, trace, audit, and leakage proof', 'files': ['integrations/bigseller-live-smoke.json']},
        'push_delivery': {'label': 'true APNS/FCM push delivery', 'files': ['push/apns-live-delivery.json', 'push/fcm-live-delivery.json']},
        'postgres_soak': {'label': 'true multi-instance Postgres soak', 'files': ['drills/postgres-multi-instance-soak.json']},
        'rollback_drill': {'label': 'true rollback drill', 'files': ['drills/rollback-drill.json']},
        'backup_restore_drill': {'label': 'true backup/restore drill', 'files': ['drills/backup-restore-drill.json']},
        'self_healing_failure_injection': {'label': 'true self-healing failure injection report', 'files': ['drills/self-healing-failure-injection.json']},
    },
    'extended_categories': {
        'team_governance_control_plane': {'label': 'true GitHub organization/team CODEOWNERS control-plane verification', 'files': ['control-plane/github-team-governance-live.json']},
        'native_signed_artifact_bindings': {'label': 'Main Verification binding for native build and signed artifact evidence', 'files': ['control-plane/native-signed-artifact-binding.json']},
    },
}
(ROOT / 'release/evidence-policy-v1.json').write_text(json.dumps(policy, indent=2, sort_keys=True) + '\n', encoding='utf-8')

replace_once(
    'scripts/check_external_ga_evidence.py',
    'REQUIRED_EVIDENCE: dict[str, dict[str, Any]] = {',
    'EVIDENCE_POLICY_PATH = Path(__file__).resolve().parents[1] / "release/evidence-policy-v1.json"\n\n\ndef _load_required_evidence() -> dict[str, dict[str, Any]]:\n    policy = json.loads(EVIDENCE_POLICY_PATH.read_text(encoding="utf-8"))\n    categories = policy.get("base_categories")\n    if not isinstance(categories, dict) or not categories:\n        raise RuntimeError("Real GA evidence policy has no base_categories")\n    return categories\n\n\nREQUIRED_EVIDENCE: dict[str, dict[str, Any]] = _load_required_evidence()\n\n\n_LEGACY_REQUIRED_EVIDENCE_REMOVED = {',
)
p = ROOT / 'scripts/check_external_ga_evidence.py'
text = p.read_text(encoding='utf-8')
start = text.index('_LEGACY_REQUIRED_EVIDENCE_REMOVED = {')
end_marker = '\n}\n\n\ndef _read_json'
end = text.index(end_marker, start)
text = text[:start] + '# Evidence category paths and labels are loaded from release/evidence-policy-v1.json.\n\n\ndef _read_json' + text[end + len(end_marker):]
p.write_text(text, encoding='utf-8')

(ROOT / 'scripts/check_real_ga_policy_consistency.py').write_text('''#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release/evidence-policy-v1.json"
AUDIT = ROOT / "release/real-ga-evidence-audit-1.12.7.json"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    base = policy.get("base_categories") or {}
    extended = policy.get("extended_categories") or {}
    external = load_module(ROOT / "scripts/check_external_ga_evidence.py", "external_ga_policy")
    if set(external.REQUIRED_EVIDENCE) != set(base):
        raise SystemExit("check_external_ga_evidence.py category set differs from evidence-policy-v1.json")
    expected = set(base) | set(extended)
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    actual = set((audit.get("categories") or {}).keys())
    if actual != expected:
        raise SystemExit(f"static Real GA audit categories differ: expected={sorted(expected)} actual={sorted(actual)}")
    blockers = sum(1 for item in audit["categories"].values() if not item.get("ok"))
    if int(audit.get("blocker_count", -1)) != blockers:
        raise SystemExit("static Real GA audit blocker_count is stale")
    print(f"Real GA evidence policy verified across {len(expected)} categories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''', encoding='utf-8')

replace_once(
    '.github/workflows/ci.yml',
    '          python scripts/check_industrial_remediation_contract.py .\n',
    '          python scripts/check_industrial_remediation_contract.py .\n          python scripts/check_unique_api_routes.py\n          python scripts/check_real_ga_policy_consistency.py\n',
)

replace_once(
    'tests/test_postgres_appsync_atomic_chat.py',
    '    ChatLeaseLost,\n',
    '    ChatEventSequenceConflict,\n    ChatLeaseLost,\n',
)
append_once(
    'tests/test_postgres_appsync_atomic_chat.py',
    '    store_b.close()\n',
    '''


def test_expired_lease_is_rejected_and_event_conflicts_fail_closed() -> None:
    dsn = _dsn()
    namespace = f"test_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    repo = PostgresChatRepository(store, lease_seconds=30)
    actor = f"operator-{uuid.uuid4().hex[:8]}"

    reservation = repo.reserve(
        actor=actor, endpoint="conversations.ask", idempotency_key="event-key",
        payload={"content": "hello"}, conversation_id=None, title="Events",
        source_device_id=None, content="hello", last_event_id=0,
    )
    repo.append_event(
        reservation, sequence=1, event="chat.started",
        data={"conversation_id": reservation.conversation_id}, status="running",
    )
    repo.append_event(
        reservation, sequence=1, event="chat.started",
        data={"conversation_id": reservation.conversation_id}, status="running",
    )
    with pytest.raises(ChatEventSequenceConflict):
        repo.append_event(
            reservation, sequence=1, event="chat.delta",
            data={"text": "different"}, status="running",
        )

    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE omnidesk_appsync_chat_requests "
            "SET lease_expires_at=EXTRACT(EPOCH FROM clock_timestamp())-1 "
            "WHERE namespace=%s AND organization_id=%s AND actor=%s "
            "AND endpoint=%s AND idempotency_key=%s",
            (namespace, reservation.organization_id, actor, "conversations.ask", "event-key"),
        )
        conn.commit()
    with pytest.raises(ChatLeaseLost):
        repo.renew_lease(reservation)
    with pytest.raises(ChatLeaseLost):
        repo.fail(reservation, {"code": "must-not-write"})
    store.close()


def test_strict_repository_rejects_unprovisioned_actor() -> None:
    dsn = _dsn()
    namespace = f"test_{uuid.uuid4().hex}"
    apply_appsync_migrations(dsn, namespace=namespace)
    store = MigratedMultiInstancePostgresAppSyncStore(dsn=dsn, namespace=namespace, pool_size=2)
    repo = PostgresChatRepository(
        store, lease_seconds=30, allow_implicit_provisioning=False
    )
    actor = f"missing-{uuid.uuid4().hex[:8]}"
    with pytest.raises(PermissionError, match="identity_not_provisioned"):
        repo.organization_for_actor(actor)
    with pytest.raises(PermissionError, match="identity_not_provisioned"):
        repo.reserve(
            actor=actor, endpoint="conversations.ask", idempotency_key="strict-key",
            payload={"content": "hello"}, conversation_id=None, title="Strict",
            source_device_id=None, content="hello", last_event_id=0,
        )
    store.close()
''',
)

(ROOT / 'docs/INDUSTRIAL_L4_OPERATOR_RUNBOOK.md').write_text('''# Industrial L4 Operator Closure Runbook

This runbook covers the work that cannot be truthfully completed by source changes alone.

## Required topology

- 3 gateway replicas across at least two failure domains
- 2 background workers
- PgBouncer transaction pooling
- PostgreSQL staging/HA cluster
- OpenTelemetry Collector, Tempo, Prometheus and Grafana

## Soak gates

Run a 24-hour baseline and a 72-hour release-candidate soak. Record request volume, p50/p95/p99 latency, error rate, duplicate assistant-message count, event-sequence conflicts, lease losses, pool acquisition latency, failovers and recovery.

Acceptance criteria:

- no cross-tenant reads or writes
- no duplicate assistant messages
- no conflicting stream events
- no stale worker commits after lease loss
- PostgreSQL pool utilization below 80% sustained
- all SLO/error-budget thresholds satisfied

## Failure drills

Execute and capture evidence for gateway kill, worker kill, rolling restart, PostgreSQL primary failover, provider 429/500/timeouts, network latency/partition, SSE disconnect/reconnect, backup restore, failed rollout rollback and sandbox-runner isolation.

Targets:

- RPO <= 5 minutes
- RTO <= 30 minutes
- rollback <= 15 minutes

## Signed distribution

Produce and verify Android AAB, iOS IPA, macOS notarized bundle and Windows Authenticode artifacts. Bind each artifact digest to the source commit, build run, signing run, Main Verification run and GitHub artifact attestation.

## Live evidence

Capture APNS/FCM receipts, real model smoke, BigSeller smoke when in scope, live organization/team governance, OTLP trace export and dashboard/rule loading. Import only operator-produced evidence through the approved import scripts.

Do not mark customer-distribution Real GA until `python scripts/check_real_ga_complete.py .` and `python scripts/check_customer_distribution_ga.py .` pass without `--audit-only`.
''', encoding='utf-8')

print('industrial L4 source remediation applied')
