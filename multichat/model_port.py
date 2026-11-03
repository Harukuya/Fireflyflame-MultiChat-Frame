"""Platform-neutral model boundary. Core code depends only on this module.

Host integrations translate their SDK's messages, tools and usage at the edge.
ModelRequest.messages uses role/content dictionaries; image blocks use
{"type": "image", "source": {"media_type": ..., "data": <base64>}}.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass
class ModelResponse:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    # Optional reported token usage. Never infer missing token counts.
    usage: Any = None
    raw_completion: Any = None
    # Opaque continuation data for an adapter; the core never interprets it.
    native: Any = None


@dataclass
class ToolResult:
    call_id: str
    content: str


@dataclass
class ToolRound:
    response: ModelResponse
    results: list[ToolResult]
    include_text: bool = True

    def text_payload(self) -> dict:
        """Text-only accounting view, without raw SDK objects or image payloads."""
        return {"text": self.response.text if self.include_text else "",
                "think": self.response.reasoning_content or "",
                "tool_calls": [c.as_dict() for c in self.response.calls],
                "results": [{"content": r.content} for r in self.results]}


@dataclass
class ModelRequest:
    system: str
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    tool_choice: str = "auto"
    tool_history: list[ToolRound] = field(default_factory=list)


@runtime_checkable
class ModelProvider(Protocol):
    """Implement complete(); optional identity fields enable richer statistics.

provider_config may contain id/model/modalities, but should not expose secrets.
An absent/empty modalities list preserves the core's permissive default.
"""

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ScriptedModelProvider:
    """Deterministic offline model for examples and integration tests."""

    def __init__(self, responses: list[ModelResponse | BaseException], *,
                 provider_id: str = "scripted", model: str = "offline-example"):
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []
        self.provider_config = {"id": provider_id, "model": model, "modalities": ["text", "image"]}

    def get_model(self) -> str:
        return self.provider_config["model"]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("No scripted response remains")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response
