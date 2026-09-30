import pytest

from omnidesk_agent.security.execution_profiles import task_execution_policy


@pytest.mark.parametrize("writes,network,high_risk,break_glass,profile,approval,network_policy,credentials,dual", [
    (False, False, False, False, "profile_readonly", "on_request", "deny_by_default", "no_secret_read", False),
    (False, True, False, False, "profile_readonly", "on_request", "deny_by_default", "no_secret_read", False),
    (True, False, False, False, "profile_workspace_write_no_network", "on_request", "deny_by_default", "no_secret_read", False),
    (True, True, False, False, "profile_workspace_write", "on_request", "allowlist", "no_secret_read", False),
    (True, True, True, False, "profile_tool_limited", "owner_required", "allowlist", "approved_secret_refs", False),
    (True, True, True, True, "profile_break_glass", "owner_required", "break_glass", "break_glass", True),
])
def test_execution_profile_priority_preserves_approval_and_secret_boundaries(
    writes, network, high_risk, break_glass, profile, approval, network_policy, credentials, dual,
):
    policy = task_execution_policy(writes=writes, network=network, high_risk=high_risk, break_glass=break_glass)
    assert policy["sandbox_profile"] == profile
    assert policy["approval_policy"] == approval
    assert policy["network_policy"] == network_policy
    assert policy["credential_policy"] == credentials
    assert policy["requires_dual_approval"] is dual
    assert policy["audit_level"] == "full"
    if writes or high_risk or break_glass:
        assert policy["rollback_policy"] == "required"
