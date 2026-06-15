from __future__ import annotations

import hashlib
import hmac

import pytest

from omnidesk_agent.channels.capability_matrix import evaluate_channel_action
from omnidesk_agent.channels.identity_firewall import SenderIdentityStore
from omnidesk_agent.config import AppConfig
from omnidesk_agent.evals.promotion_gate import evaluate_promotion
from omnidesk_agent.evals.regression_suite import RegressionResult
from omnidesk_agent.onboarding import build_channel_onboarding_plan, build_evidence_doctor, run_doctor
from omnidesk_agent.security.cik_guard import CIKInput, evaluate_cik
from omnidesk_agent.security.execution_profiles import task_execution_policy
from omnidesk_agent.self_healing import RuntimeSignal
from omnidesk_agent.self_upgrade.evidence_bundle import build_evidence_bundle
from omnidesk_agent.self_upgrade.pr_generator import PRGenerator
from omnidesk_agent.self_upgrade.repair_loop import build_iterative_repair_record
from omnidesk_agent.skills.signed_registry import SignedSkillRegistry


def test_onboarding_doctor_and_evidence_gate_are_structured(tmp_path):
    report = run_doctor(AppConfig(), profile="source-only", root=tmp_path).to_dict()
    evidence = build_evidence_doctor(tmp_path)

    assert "summary" in report
    assert any(item["name"].endswith("AGENTS.md") for item in report["checks"])
    assert evidence["status"] == "not_run"
    assert "self_healing_failure_injection" in evidence["required_categories"]


def test_channel_capability_and_identity_firewall_fail_closed(tmp_path):
    action = evaluate_channel_action("slack", "bypass_approval", risk="critical")
    store = SenderIdentityStore(tmp_path / "senders.json")
    unknown = store.evaluate(channel="slack", sender_id="u1", channel_signature_verified=True)
    paired = store.pair_sender(channel="slack", sender_id="u1", oauth_subject="alice", device_id="d1", trust_level="verified")
    drift = store.evaluate(channel="slack", sender_id="u1", channel_signature_verified=True, oauth_subject="mallory", device_id="d1")
    high_risk = store.evaluate(channel="slack", sender_id="u1", action="send_external_message", channel_signature_verified=True, oauth_subject="alice", device_id="d1", risk="high")

    assert action["allowed"] is False
    assert unknown.decision == "pairing_required"
    assert paired.trust_level == "verified"
    assert drift.decision == "reverification_required"
    assert high_risk.decision == "owner_approval_required"


def test_execution_profiles_and_cik_guard_require_approval():
    policy = task_execution_policy(writes=True, network=False, high_risk=False)
    blocked = evaluate_cik(CIKInput(capability="read_secret", identity_trust="trusted"))
    approval = evaluate_cik(
        CIKInput(
            capability="send_external_message",
            identity_trust="paired",
            knowledge_sources=("external_unverified:webhook",),
            risk="high",
        )
    )

    assert policy["sandbox_profile"] == "profile_workspace_write_no_network"
    assert policy["credential_policy"] == "no_secret_read"
    assert blocked.decision == "block"
    assert approval.decision == "approval_required"
    assert "source_attestation" in approval.required_controls


def test_signed_skill_registry_requires_signature_and_scan(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text("# Demo\n", encoding="utf-8")
    digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    signature = hmac.new(b"secret", digest.encode("utf-8"), hashlib.sha256).hexdigest()
    (skill / "skill.manifest.json").write_text(
        (
            "{"
            '"name":"demo","version":"1.0.0","sandbox_profile":"profile_readonly",'
            f'"sha256":"{digest}","signature":"{signature}","vulnerability_scan":"passed"'
            "}"
        ),
        encoding="utf-8",
    )

    verified = SignedSkillRegistry([tmp_path / "skills"], signing_secret="secret").verified_index()
    assert verified[0]["name"] == "demo"
    with pytest.raises(PermissionError, match="signature requires signing secret"):
        SignedSkillRegistry([tmp_path / "skills"]).verified_index()


def test_repair_pr_and_eval_promotion_gate():
    record = build_iterative_repair_record(RuntimeSignal(component="push.apns", consecutive_failures=4))
    bundle = build_evidence_bundle(
        incident_id=record["review"]["incident_id"],
        branch="ai/push-apns-repair",
        tests=("python -m pytest tests/test_ga_1_11_openclaw_codex_alignment.py",),
        gates=("unit_tests", "ga_release_gate", "owner_approval"),
        rollback_plan="revert ai branch and restore previous credential version",
    )
    draft = PRGenerator().draft(
        incident_id=bundle.incident_id,
        branch=bundle.branch,
        summary="repair APNS delivery failure",
        bundle=bundle,
    )
    promotion = evaluate_promotion([RegressionResult("identity", True, {}), RegressionResult("approval", True, {})])

    assert record["proposal"]["requires_human_approval"] is True
    assert draft.ready_for_review is True
    assert "External evidence status" in draft.body
    assert promotion.allowed is True


def test_channel_onboard_plan_keeps_unknown_channels_blocked():
    known = build_channel_onboarding_plan("slack")
    unknown = build_channel_onboarding_plan("no-such-channel-xyz")

    assert known["status"] == "pairing_required"
    assert "permission_approval_gate" in known["required_controls"]
    assert unknown["status"] == "blocked_unknown_channel"
