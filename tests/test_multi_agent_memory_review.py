from __future__ import annotations

from omnidesk_agent.self_learning.governance import MemoryCurator, MultiAgentMemoryReviewer


def test_multi_agent_memory_review_quorum_approves_well_evidenced_memory():
    reviewer = MultiAgentMemoryReviewer()
    decision = reviewer.review(
        {
            "confidence": 0.9,
            "risk_level": "medium",
            "success": True,
            "recommended_next_action": "reuse stable selector",
            "solution_attempted": ["clicked stable selector"],
            "raw_trace": {"events": [{"event": "selector_found"}]},
            "validation_count": 3,
        }
    )

    assert decision.verdict == "approve"
    assert {vote.agent for vote in decision.votes} == {"reviewer_agent", "critic_agent", "safety_agent"}


def test_multi_agent_memory_review_rejects_unsafe_learning():
    decision = MultiAgentMemoryReviewer().review(
        {
            "confidence": 0.95,
            "risk_level": "critical",
            "failure_reason": "security_violation",
            "recommended_next_action": "bypass permission",
            "raw_trace": {"events": []},
        }
    )

    assert decision.verdict == "reject"
    assert "safety" in decision.reason


def test_memory_curator_can_use_multi_agent_reviewer_to_downgrade_candidate():
    curator = MemoryCurator(reviewer=MultiAgentMemoryReviewer())
    reviews = curator.review(
        [
            {
                "id": 11,
                "task_type": "browser",
                "goal": "login",
                "success": True,
                "success_score": 1.0,
                "reusable_skill": True,
                "risk_level": "medium",
                "recommended_next_action": "reuse login plan",
            }
        ]
    )

    assert reviews[0].memory_status == "needs_review"
    assert reviews[0].review_status == "needs_review"
    assert reviews[0].review_votes
