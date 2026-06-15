from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

SandboxProfile = Literal[
    "profile_readonly",
    "profile_workspace_write",
    "profile_workspace_write_no_network",
    "profile_tool_limited",
    "profile_break_glass",
]


@dataclass(frozen=True)
class ExecutionProfile:
    sandbox_profile: SandboxProfile
    approval_policy: Literal["never", "on_request", "owner_required"]
    network_policy: Literal["deny_by_default", "allowlist", "break_glass"]
    filesystem_policy: Literal["read_only", "workspace_only", "approved_paths"]
    credential_policy: Literal["no_secret_read", "approved_secret_refs", "break_glass"]
    rollback_policy: Literal["required", "recommended", "operator_waived"]
    audit_level: Literal["full", "standard"]
    allowed_tools: tuple[str, ...]
    requires_dual_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: dict[SandboxProfile, ExecutionProfile] = {
    "profile_readonly": ExecutionProfile(
        sandbox_profile="profile_readonly",
        approval_policy="on_request",
        network_policy="deny_by_default",
        filesystem_policy="read_only",
        credential_policy="no_secret_read",
        rollback_policy="recommended",
        audit_level="full",
        allowed_tools=("search", "read_files", "doctor", "evidence_doctor"),
    ),
    "profile_workspace_write": ExecutionProfile(
        sandbox_profile="profile_workspace_write",
        approval_policy="on_request",
        network_policy="allowlist",
        filesystem_policy="workspace_only",
        credential_policy="no_secret_read",
        rollback_policy="required",
        audit_level="full",
        allowed_tools=("read_files", "write_files", "tests", "doctor", "evidence_doctor"),
    ),
    "profile_workspace_write_no_network": ExecutionProfile(
        sandbox_profile="profile_workspace_write_no_network",
        approval_policy="on_request",
        network_policy="deny_by_default",
        filesystem_policy="workspace_only",
        credential_policy="no_secret_read",
        rollback_policy="required",
        audit_level="full",
        allowed_tools=("read_files", "write_files", "tests", "local_build"),
    ),
    "profile_tool_limited": ExecutionProfile(
        sandbox_profile="profile_tool_limited",
        approval_policy="owner_required",
        network_policy="allowlist",
        filesystem_policy="approved_paths",
        credential_policy="approved_secret_refs",
        rollback_policy="required",
        audit_level="full",
        allowed_tools=("approved_connector", "approved_channel", "approved_ui_bridge"),
    ),
    "profile_break_glass": ExecutionProfile(
        sandbox_profile="profile_break_glass",
        approval_policy="owner_required",
        network_policy="break_glass",
        filesystem_policy="approved_paths",
        credential_policy="break_glass",
        rollback_policy="required",
        audit_level="full",
        allowed_tools=("incident_response",),
        requires_dual_approval=True,
    ),
}


def resolve_execution_profile(*, writes: bool, network: bool, high_risk: bool, break_glass: bool = False) -> ExecutionProfile:
    if break_glass:
        return PROFILES["profile_break_glass"]
    if high_risk:
        return PROFILES["profile_tool_limited"]
    if writes and network:
        return PROFILES["profile_workspace_write"]
    if writes:
        return PROFILES["profile_workspace_write_no_network"]
    return PROFILES["profile_readonly"]


def task_execution_policy(*, writes: bool, network: bool, high_risk: bool, break_glass: bool = False) -> dict[str, Any]:
    return resolve_execution_profile(writes=writes, network=network, high_risk=high_risk, break_glass=break_glass).to_dict()
