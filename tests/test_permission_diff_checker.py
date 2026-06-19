from __future__ import annotations
from omnidesk_agent.self_upgrade.risk.permission_diff_checker import PermissionDiffChecker

def test_permission_diff_checker_flags_expansion():
    diff = PermissionDiffChecker().compare(["files.read"], ["files.read", "gmail.send"])
    assert diff.added == ["gmail.send"]
    assert diff.risk == "high"
    assert diff.requires_human_approval

def test_permission_diff_checker_low_when_no_added_permission():
    diff = PermissionDiffChecker().compare(["files.read", "browser.read"], ["files.read"])
    assert diff.added == []
    assert diff.risk == "low"
