from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalScenario:
    name: str
    input: str
    expected_controls: tuple[str, ...]
    expected_outcome: str
    metadata: dict[str, Any]


def load_scenarios(path: Path) -> list[EvalScenario]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    scenarios = raw.get("scenarios", raw if isinstance(raw, list) else [])
    return [
        EvalScenario(
            name=item["name"],
            input=item["input"],
            expected_controls=tuple(item.get("expected_controls", ())),
            expected_outcome=item.get("expected_outcome", "approval_required"),
            metadata=item.get("metadata", {}),
        )
        for item in scenarios
    ]
