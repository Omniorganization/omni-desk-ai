from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional, Protocol

ModelTask = Literal[
    "planner",
    "tool_plan",
    "chat",
    "code",
    "vision",
    "private",
    "summarize",
    "upgrade",
    "embed",
]


@dataclass
class ModelRequest:
    system: str
    user: str
    task: ModelTask = "chat"
    images: list[str] = field(default_factory=list)
    json_mode: bool = False
    verified_required: bool = False
    task_id: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str
    provider: str
    model: str
    profile: str
    usage: Optional[dict[str, Any]] = None
    raw: Optional[dict[str, Any]] = None


@dataclass
class ModelDelta:
    """One governed provider-stream event.

    Providers emit text/reasoning increments and a final event carrying usage and
    finish metadata. ``native`` is false only when a provider has no streaming
    transport and the compatibility fallback emits a single completed delta.
    """

    sequence: int
    provider: str
    model: str
    profile: str
    text: str = ""
    reasoning: str = ""
    usage: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None
    provider_request_id: Optional[str] = None
    native: bool = True


class ModelProvider(Protocol):
    provider_name: str
    model: str
    profile_name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelDelta]: ...
