"""Credentials and approved content do not leak into audit or approval records.

Regression tests for the 2026-11-21 security audit (medium): proposals carry the
full content of side effects (typed text, file bodies, messages), but audit
redaction matched key names only, so a typed password was stored in plaintext in
audit.log and in the approvals database.
"""

from __future__ import annotations

import asyncio

import pytest

from omnidesk_agent.config import PermissionConfig
from omnidesk_agent.privacy.redaction import redact_secret_values
from omnidesk_agent.security.approval_required import ApprovalRequired
from omnidesk_agent.security.approval_store import ApprovalStore
from omnidesk_agent.security.permissions import PermissionManager
from omnidesk_agent.tools.base import ToolContext, bound_content, proposal
from omnidesk_agent.tools.files import FilesTool

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 4
AWS_KEY_ID = "AKIA" + "ABCDEFGHIJ234567"
TYPED_PASSWORD = "correct horse battery staple"


class _CapturingStore:
    """Stands in for any approval store backend; records what would be persisted."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, proposal: dict) -> str:
        self.created.append(proposal)
        return f"approval-{len(self.created)}"


def test_typed_text_and_file_bodies_never_reach_the_audit_log(tmp_path):
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))
    ctx = ToolContext(permissions=pm, actor="owner")

    pm.verify(proposal("computer", "type_text", {**bound_content("text", TYPED_PASSWORD), "expected_result": "log in"}, "high", "type", ctx))
    asyncio.run(FilesTool(tmp_path / "ws", allow_write=True).call("write_text", {"path": "notes.txt", "text": "db password: " + TYPED_PASSWORD}, ctx))

    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert TYPED_PASSWORD not in log
    assert "[CONTENT_WITHHELD]" in log and "text_sha256" in log
    assert (tmp_path / "ws" / "notes.txt").read_text() == "db password: " + TYPED_PASSWORD  # the action itself is unchanged


def test_credentials_in_other_fields_are_redacted_in_the_audit_log(tmp_path):
    pm = PermissionManager(PermissionConfig(default_mode="allow", audit_log=tmp_path / "audit.log"))

    pm.verify({"tool": "browser", "action": "navigate", "risk": "low", "source": "local-cli", "actor": "owner",
               "args": {"url": f"https://admin:{TYPED_PASSWORD.replace(' ', '')}@example.org/?token=abc123", "note": f"use {GITHUB_TOKEN}"}})

    log = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert GITHUB_TOKEN not in log and TYPED_PASSWORD.replace(" ", "") not in log and "abc123" not in log


def test_approval_records_drop_credentials_but_keep_identities_and_content(tmp_path):
    store = _CapturingStore()
    pm = PermissionManager(PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log"), store)
    text = f"deploy with {AWS_KEY_ID} and {GITHUB_TOKEN}, then reply done"

    with pytest.raises(ApprovalRequired) as raised:
        pm.verify({"tool": "channels", "action": "send_text", "risk": "high", "source": "telegram",
                   "actor": "token:OMNIDESK_OWNER_TOKEN", "scope_hash": "s1", "args": {"text": text}})

    stored = store.created[0]
    assert AWS_KEY_ID not in str(stored) and GITHUB_TOKEN not in str(stored)
    assert stored["args"]["text"].startswith("deploy with [REDACTED_SECRET]") and stored["args"]["text"].endswith("then reply done")
    # Identities drive the requester-cannot-approve rule and must survive untouched.
    assert stored["actor"] == stored["created_by"] == stored["proposer"] == "token:OMNIDESK_OWNER_TOKEN"
    assert raised.value.proposal == stored  # the run keeps the redacted copy too


def test_redacted_approval_still_resumes_the_exact_unredacted_call(tmp_path):
    approvals = ApprovalStore(tmp_path / "approvals.sqlite3")
    pm = PermissionManager(PermissionConfig(approval_mode="remote_approval", audit_log=tmp_path / "audit.log"), approvals)
    original = {"tool": "channels", "action": "send_text", "risk": "high", "source": "telegram", "actor": "owner",
                "scope_hash": "s1", "args": {"text": f"token {GITHUB_TOKEN}"}}
    with pytest.raises(ApprovalRequired) as raised:
        pm.verify(original)
    approvals.decide(raised.value.approval_id, "approved")
    consumed = approvals.consume_approved(raised.value.approval_id, raised.value.proposal)

    pm.allow_approved_proposal(consumed["proposal"])

    assert pm.verify(original).allowed


@pytest.mark.parametrize("secret", [
    GITHUB_TOKEN,
    AWS_KEY_ID,
    "xoxb-1234567890-abcdefghij",
    "sk-ant-" + "a" * 40,
    "sk_live_" + "b" * 24,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "AIza" + "c" * 35,
])
def test_known_credential_formats_are_redacted(secret):
    assert secret not in redact_secret_values({"a": [f"x {secret} y"]})["a"][0]


@pytest.mark.parametrize("ordinary", [
    "token:OMNIDESK_OWNER_TOKEN",
    "a" * 64,  # sha256 digests and scope hashes
    "basic usage of the tool",
    "the session token expires tomorrow",
])
def test_identities_digests_and_ordinary_text_are_kept(ordinary):
    assert redact_secret_values(ordinary) == ordinary


def test_the_terminal_prompt_masks_credentials_but_shows_the_content(tmp_path, monkeypatch, capsys):
    # The interactive approval prompt printed the raw proposal, so a password in a
    # URL or a token in a note reached the terminal's scrollback. CodeQL flagged it.
    pm = PermissionManager(PermissionConfig(audit_log=tmp_path / "audit.log"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    url = f"https://admin:{TYPED_PASSWORD.replace(' ', '')}@example.org/"

    with pytest.raises(PermissionError):
        pm.verify({"tool": "browser", "action": "navigate", "risk": "high", "source": "local-cli", "actor": "owner",
                   "args": {"url": url, "note": f"use {GITHUB_TOKEN}", "text": "please file the report"}})

    shown = capsys.readouterr().out
    assert TYPED_PASSWORD.replace(" ", "") not in shown and GITHUB_TOKEN not in shown
    assert "please file the report" in shown and "example.org" in shown  # what is approved stays visible
