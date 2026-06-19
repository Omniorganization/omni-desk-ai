from __future__ import annotations

from omnidesk_agent.self_learning.skill_versions import SkillEvolutionGraph, SkillLineageStore, SkillPerformanceHistory


def test_skill_lineage_tracks_versions_benchmarks_and_retirement(tmp_path):
    store = SkillLineageStore(tmp_path / "skills.sqlite3")
    v1 = store.register_version("TikTokParser", "v1", artifact_hash="hash-v1", status="stable")
    v2 = store.register_version("TikTokParser", "v2", artifact_hash="hash-v2", parent_version="v1", status="canary")
    store.record_benchmark("TikTokParser", "v1", "success_rate", 0.72)
    store.record_benchmark("TikTokParser", "v2", "success_rate", 0.84)

    comparison = SkillPerformanceHistory(store).compare("TikTokParser", "v1", "v2")
    assert v1.version == "v1"
    assert v2.parent_version == "v1"
    assert comparison.improved is True
    assert comparison.delta == 0.12

    store.retire_version("TikTokParser", "v1", reason="v2 has higher benchmark")
    graph = SkillEvolutionGraph(store).graph("TikTokParser")
    assert graph["edges"] == [{"from": "v1", "to": "v2", "relation": "evolved_to"}]
    assert "v1" in graph["retired_versions"]
    assert "v2" in graph["active_versions"]
