from __future__ import annotations

from omnidesk_agent.privacy.governance import MemoryGovernance


def test_memory_governance_blocks_credential_like_content():
    gov = MemoryGovernance(retention_days=30)
    decision = gov.decide("oauth token abc", channel="gmail", actor="user1")
    assert not decision.allow_write
    assert decision.namespace == "gmail:user1"


def test_memory_governance_allows_normal_and_redacts():
    gov = MemoryGovernance()
    decision = gov.decide("normal task result", channel="web", actor="u")
    assert decision.allow_write
    assert decision.namespace == "web:u"
    assert gov.expires_at() > 0
