from __future__ import annotations

import subprocess

import pytest

from omnidesk_agent.self_upgrade.pr_manager import SelfUpgradePRManager
from omnidesk_agent.self_upgrade.upgrade_policy import UpgradePolicyEngine


def _git(path, *args):
    return subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True)


def test_pr_manager_rejects_main_direct_patch(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")

    manager = SelfUpgradePRManager(tmp_path)
    with pytest.raises(PermissionError):
        manager.ensure_not_main()


def test_upgrade_policy_blocks_security_runtime_changes():
    decision = UpgradePolicyEngine().evaluate_paths(["omnidesk_agent/security/permissions.py"])
    assert not decision.allowed
    assert decision.requires_human_review
