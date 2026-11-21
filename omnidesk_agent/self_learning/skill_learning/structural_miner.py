"""Mine reusable skills from successful runs without copying user content.

A learned skill becomes part of the planner policy for every requester, so its
text must not carry one requester's message. The miner therefore uses only
structural fields (task type and the tool.action sequence) and requires the
same sequence to have succeeded for several distinct requesters.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

_STEP = re.compile(r"^[a-z][a-z0-9_]{0,40}\.[a-z][a-z0-9_]{0,40}$")


@dataclass(frozen=True)
class MinedSkill:
    name: str
    body: str
    task_type: str
    sequence: tuple[str, ...]
    run_count: int
    requester_count: int
    experience_ids: tuple[int, ...]

    def source(self) -> dict[str, Any]:
        return {
            "miner": "structural_v1",
            "task_type": self.task_type,
            "sequence": list(self.sequence),
            "run_count": self.run_count,
            "requester_count": self.requester_count,
            "experience_ids": list(self.experience_ids),
        }


class StructuralSkillMiner:
    def __init__(self, *, min_runs: int = 3, min_requesters: int = 2, max_steps: int = 8):
        self.min_runs = max(1, int(min_runs))
        self.min_requesters = max(1, int(min_requesters))
        self.max_steps = max(2, int(max_steps))

    def mine(self, experiences: Iterable[dict[str, Any]], *, limit: int = 10) -> list[MinedSkill]:
        groups: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
        for item in experiences:
            if not item.get("success") or str(item.get("privacy_level") or "normal") != "normal":
                continue
            sequence = tuple(str(step) for step in item.get("solution_attempted") or [])
            if not 2 <= len(sequence) <= self.max_steps or not all(_STEP.match(step) for step in sequence):
                continue
            task_type = str(item.get("task_type") or "general")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", task_type):
                continue
            groups[(task_type, sequence)].append(item)

        mined: list[MinedSkill] = []
        for (task_type, sequence), items in groups.items():
            requesters = {(str(i.get("channel") or ""), str(i.get("actor") or "")) for i in items}
            if len(items) < self.min_runs or len(requesters) < self.min_requesters:
                continue
            chain = " -> ".join(sequence)
            body = (
                f"Task type: {task_type}.\n"
                f"This tool sequence completed in {len(items)} recorded runs for {len(requesters)} different requesters: {chain}.\n"
                "Prefer it for this task type when the tools fit the request. Keep every approval step the tools ask for "
                "and check each step's expected result before the next step."
            )
            mined.append(MinedSkill(
                name=f"{task_type}-{'-'.join(step.replace('.', '-') for step in sequence)}",
                body=body,
                task_type=task_type,
                sequence=sequence,
                run_count=len(items),
                requester_count=len(requesters),
                experience_ids=tuple(sorted(int(i["id"]) for i in items if str(i.get("id", "")).isdigit())),
            ))
        mined.sort(key=lambda skill: (-skill.requester_count, -skill.run_count, skill.name))
        return mined[: max(0, int(limit))]
