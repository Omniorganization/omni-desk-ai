from __future__ import annotations

from omnidesk_agent.self_learning.causal import CausalGraph, RootCauseAnalyzer
from omnidesk_agent.self_learning.roi import LearningROIAnalyzer
from omnidesk_agent.self_learning.world_model import WorldModel


def test_root_cause_analyzer_distinguishes_root_cause_from_symptom():
    graph = CausalGraph()
    graph.add_chain(["selector_changed", "login_failed", "campaign_data_missing"], weight=3)
    report = RootCauseAnalyzer(graph).analyze("campaign_data_missing")

    assert report.root_cause == "selector_changed"
    assert report.chain == ["selector_changed", "login_failed", "campaign_data_missing"]
    assert report.confidence > 0


def test_root_cause_analyzer_learns_from_experiences():
    report = RootCauseAnalyzer().analyze_experiences(
        [
            {
                "causal_chain": ["api_schema_changed", "parser_failed", "report_empty"],
                "success_score": 2.0,
            }
        ],
        symptom="report_empty",
    )

    assert report.root_cause == "api_schema_changed"
    assert report.evidence[0]["cause"] == "parser_failed"


def test_learning_roi_rejects_low_value_learning_and_approves_high_value_learning():
    analyzer = LearningROIAnalyzer()
    rejected = analyzer.evaluate(success_delta=0.005, affected_task_count=10, compute_cost=10)
    approved = analyzer.evaluate(success_delta=0.05, affected_task_count=100, compute_cost=2, risk_penalty=0.1)

    assert rejected.decision == "reject"
    assert approved.decision == "approve"
    assert approved.roi > rejected.roi


def test_world_model_observes_transitions_and_predicts_next_state():
    model = WorldModel()
    model.observe_entity("tiktok_ads", "platform", surface="gmvmax")
    model.observe_entity("campaign_table", "ui_component", selector="table")
    model.observe_relation("tiktok_ads", "contains", "campaign_table", confidence=0.9)
    model.transition_state("campaign_table", "loaded", trigger="open_dashboard", confidence=0.8)
    model.transition_state("campaign_table", "loaded", trigger="open_dashboard", confidence=0.9)

    prediction = model.predict_next_state("campaign_table", trigger="open_dashboard")
    snapshot = model.snapshot()

    assert prediction == {"entity_id": "campaign_table", "trigger": "open_dashboard", "predicted_state": "loaded", "confidence": 0.85}
    assert snapshot["relations"][0]["relation"] == "contains"
    assert snapshot["current_state"]["campaign_table"] == "loaded"
