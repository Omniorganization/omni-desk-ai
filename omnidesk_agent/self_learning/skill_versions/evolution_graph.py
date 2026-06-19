from __future__ import annotations

from typing import Any

from omnidesk_agent.self_learning.skill_versions.lineage import SkillLineageStore


class SkillEvolutionGraph:
    def __init__(self, store: SkillLineageStore):
        self.store = store

    def graph(self, skill_name: str) -> dict[str, Any]:
        versions = self.store.lineage(skill_name)
        nodes = [version.to_dict() for version in versions]
        edges = []
        known = {version.version for version in versions}
        for version in versions:
            if version.parent_version and version.parent_version in known:
                edges.append({"from": version.parent_version, "to": version.version, "relation": "evolved_to"})
        active = [version.version for version in versions if version.status in {"stable", "canary", "candidate"}]
        retired = [version.version for version in versions if version.status == "retired"]
        return {"skill_name": skill_name, "nodes": nodes, "edges": edges, "active_versions": active, "retired_versions": retired}
