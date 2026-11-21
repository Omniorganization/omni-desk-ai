"""Self-upgrade pipeline: model-proposed diff -> isolated verification -> ai/* PR.

See docs/SELF_UPGRADE_PIPELINE.md. Nothing here merges or writes to main.
"""
from __future__ import annotations

from omnidesk_agent.self_upgrade.pipeline.diff_policy import DiffPolicy, DiffReport
from omnidesk_agent.self_upgrade.pipeline.issue import UpgradeIssue, load_issue
from omnidesk_agent.self_upgrade.pipeline.proposer import ProviderModelClient, ScriptedModelClient, propose
from omnidesk_agent.self_upgrade.pipeline.publisher import GitHubPublisher, check_branch
from omnidesk_agent.self_upgrade.pipeline.verifier import EvidenceBundle, verify

__all__ = [
    "DiffPolicy", "DiffReport", "EvidenceBundle", "GitHubPublisher", "ProviderModelClient", "ScriptedModelClient",
    "UpgradeIssue", "check_branch", "load_issue", "propose", "verify",
]
