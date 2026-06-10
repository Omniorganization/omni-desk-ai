from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

ModelTask = Literal['planner','tool_plan','chat','code','vision','private','summarize','upgrade','embed']

@dataclass
class ModelRequest:
    system: str
    user: str
    task: ModelTask = 'chat'
    images: list[str] = field(default_factory=list)
    json_mode: bool = False
    verified_required: bool = False
    task_id: str = 'default'
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class ModelResponse:
    text: str
    provider: str
    model: str
    profile: str
    usage: Optional[dict[str, Any]] = None
    raw: Optional[dict[str, Any]] = None

class ModelProvider(Protocol):
    provider_name: str
    model: str
    profile_name: str
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
