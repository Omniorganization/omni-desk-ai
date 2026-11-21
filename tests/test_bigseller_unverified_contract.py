"""BigSeller real mode stays blocked, and its smoke evidence is measured, not asserted.

BigSeller's API documentation is private, so the HTTP client's paths,
signing, fields, pagination and error envelopes are guesses: run for real it
turned error envelopes into phantom orders. Until the client is written
against the vendor contract, real mode must refuse to run. The live smoke
used to fill in "no secret leakage", "error rate 0" and the webhook checks
itself and reported the sum of three probes as p95; evidence has to come from
what the run actually observed.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omnidesk_agent.integrations.bigseller import config as bigseller_config
from omnidesk_agent.integrations.bigseller.client import HttpBigSellerClient
from omnidesk_agent.integrations.bigseller.config import BigSellerConfig
from omnidesk_agent.integrations.bigseller.errors import BigSellerConfigurationError
from omnidesk_agent.integrations.bigseller.idempotency import create_bigseller_idempotency_guard

ROOT = Path(__file__).resolve().parents[1]


def _real_config(tmp_path: Path) -> BigSellerConfig:
    return BigSellerConfig(
        enabled=True,
        use_mock=False,
        base_url="https://bigseller.example.test",
        allowed_hosts=["bigseller.example.test"],
        app_id="app-id",
        app_key="app-key-secret-value",
        access_token="access-token-secret-value",
        refresh_token="refresh-token-secret-value",
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        webhook_secret="webhook-secret-value",
        state_backend="sqlite",
        state_db_path=tmp_path / "bigseller.sqlite3",
        request_signing_enabled=True,
        auth_code_exchange_path="/auth/exchange",
        refresh_token_path="/auth/refresh",
        orders_list_path="/orders",
        order_detail_path="/orders/{external_order_id}",
        inventory_list_path="/inventory",
        inventory_update_path="/inventory/{external_sku}",
        products_list_path="/products",
        product_detail_path="/products/{external_product_id}",
        fulfillment_sync_path="/orders/{external_order_id}/fulfillment",
    )


def test_fully_configured_real_mode_is_still_blocked(tmp_path):
    config = _real_config(tmp_path)

    assert bigseller_config.BIGSELLER_UNVERIFIED_CONTRACT_ISSUE in config.real_mode_issues()
    assert config.health()["ready"] is False
    with pytest.raises(BigSellerConfigurationError, match="real mode is blocked"):
        HttpBigSellerClient(config).list_orders(store_id="S1")


def _load_smoke():
    spec = importlib.util.spec_from_file_location("run_bigseller_live_smoke", ROOT / "scripts" / "run_bigseller_live_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke(monkeypatch, tmp_path):
    module = _load_smoke()
    config = _real_config(tmp_path)
    monkeypatch.setattr(bigseller_config, "BIGSELLER_VENDOR_CONTRACT_VERIFIED", True)
    monkeypatch.setattr(module.BigSellerConfig, "from_env", classmethod(lambda cls, workspace_root=None: config))
    for name, value in {
        "BIGSELLER_LIVE_AUTH_PROBE_PATH": "/auth/check",
        "BIGSELLER_LIVE_ORDER_LIST_PATH": "/orders",
        "BIGSELLER_LIVE_INVENTORY_LIST_PATH": "/inventory",
        "BIGSELLER_LIVE_SMOKE_STORE_ID": "S1",
        "BIGSELLER_LIVE_SMOKE_TRACE_ID": "trace-1",
        "BIGSELLER_LIVE_SMOKE_AUDIT_EVENT_ID": "audit-1",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("BIGSELLER_LIVE_WEBHOOK_EVENT_ID", "BIGSELLER_LIVE_WEBHOOK_SIGNATURE_VERIFIED", "BIGSELLER_LIVE_WEBHOOK_REPLAY_GUARD_VERIFIED"):
        monkeypatch.delenv(name, raising=False)
    return module, config


def _fake_requests(monkeypatch, module, responses, latencies):
    clock = iter(sum(([0.0, latency / 1000.0] for latency in latencies), []))
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(clock))
    replies = iter(responses)
    monkeypatch.setattr(module.HttpBigSellerClient, "request", lambda self, method, path, **kwargs: next(replies))


def test_smoke_ignores_operator_asserted_webhook_flags(smoke, monkeypatch, tmp_path):
    module, config = smoke
    _fake_requests(monkeypatch, module, [{"ok": 1}, {"data": [1]}, {"data": [2]}], [100.0, 300.0, 200.0])
    # Operator-asserted flags no longer count.
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_SIGNATURE_VERIFIED", "true")
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_REPLAY_GUARD_VERIFIED", "true")

    with pytest.raises(RuntimeError, match="webhook_signature_verified must be true"):
        module.build_evidence(tmp_path)


def test_smoke_webhook_checks_come_from_a_recorded_delivery(smoke, monkeypatch, tmp_path):
    module, config = smoke
    guard = create_bigseller_idempotency_guard(config)
    assert guard.claim(external_id="evt-1", store_id="S1", action_type="webhook_event")  # what the gateway did on delivery
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_EVENT_ID", "evt-1")
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_STORE_ID", "S1")
    _fake_requests(monkeypatch, module, [{"ok": 1}, {"data": [1]}, {"data": [2]}], [100.0, 300.0, 200.0])

    evidence = module.build_evidence(tmp_path)

    assert evidence["p95_latency_ms"] == 300.0 and evidence["latency_samples"] == 3
    assert evidence["webhook_signature_verified"] is True and evidence["webhook_replay_guard_verified"] is True
    assert evidence["secret_leakage_checked"] is True and evidence["no_secret_leakage"] is True


def test_smoke_fails_when_a_secret_appears_in_a_response(smoke, monkeypatch, tmp_path):
    module, config = smoke
    guard = create_bigseller_idempotency_guard(config)
    guard.claim(external_id="evt-2", store_id="S1", action_type="webhook_event")
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_EVENT_ID", "evt-2")
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_STORE_ID", "S1")
    _fake_requests(monkeypatch, module, [{"echo": "access-token-secret-value"}, {"data": [1]}, {"data": [2]}], [10.0, 10.0, 10.0])

    with pytest.raises(RuntimeError, match="no_secret_leakage must be true"):
        module.build_evidence(tmp_path)


def test_smoke_webhook_probe_leaves_no_claim_behind(smoke, monkeypatch, tmp_path):
    module, config = smoke
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_EVENT_ID", "never-delivered")
    monkeypatch.setenv("BIGSELLER_LIVE_WEBHOOK_STORE_ID", "S1")

    assert module._webhook_delivery_verified(config) is False
    # The probe released its own claim, so a later real delivery is not mistaken for a replay.
    assert create_bigseller_idempotency_guard(config).claim(external_id="never-delivered", store_id="S1", action_type="webhook_event") is True
