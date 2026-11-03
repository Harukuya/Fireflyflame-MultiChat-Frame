"""Optional AstrBot model adapter; importing the core does not need AstrBot."""
from __future__ import annotations

import json

from multichat.model_port import ModelRequest, ModelResponse, ToolCall, ToolRound


def _openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        parts = []
        for block in content:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                source = block["source"]
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{source['media_type']};base64,{source['data']}"}})
            elif block["type"] == "image_url":
                parts.append(dict(block))
        out.append({"role": message["role"], "content": parts})
    return out


def _native_round(round_: ToolRound):
    from astrbot.core.agent.message import AssistantMessageSegment, TextPart, ThinkPart, ToolCallMessageSegment
    from astrbot.core.provider.entities import ToolCallsResult

    response = round_.response
    parts = []
    if response.reasoning_content is not None or response.reasoning_signature:
        parts.append(ThinkPart(think=response.reasoning_content or "", encrypted=response.reasoning_signature))
    if round_.include_text and response.text:
        parts.append(TextPart(text=response.text))
    native_calls = getattr(response.native, "to_openai_tool_calls_model", None)
    calls = native_calls() if callable(native_calls) else [
        {"id": call.id, "type": "function", "function": {
            "name": call.name, "arguments": json.dumps(call.input, ensure_ascii=False)}}
        for call in response.calls]
    return ToolCallsResult(
        tool_calls_info=AssistantMessageSegment(tool_calls=calls, content=parts or None),
        tool_calls_result=[ToolCallMessageSegment(role="tool", tool_call_id=result.call_id, content=result.content)
                           for result in round_.results],
    )


class AstrBotModelProvider:
    """Wrap one currently selected native provider; recreate if selection changes."""

    def __init__(self, provider):
        if provider is None:
            raise ValueError("Cannot wrap an unavailable AstrBot provider")
        self.native_provider = provider

    @property
    def provider_config(self) -> dict:
        cfg = getattr(self.native_provider, "provider_config", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        return {"id": cfg.get("id"), "model": self.get_model(), "modalities": cfg.get("modalities") or []}

    def get_model(self) -> str:
        getter = getattr(self.native_provider, "get_model", None)
        if callable(getter):
            try:
                model = getter()
            except Exception:
                model = None
            if isinstance(model, str) and model:
                return model
        cfg = getattr(self.native_provider, "provider_config", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        nested = cfg.get("model_config")
        return cfg.get("model") or (nested.get("model") if isinstance(nested, dict) else "") or ""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        kwargs = {"contexts": _openai_messages(request.messages), "system_prompt": request.system,
                  "request_max_retries": 1}
        if request.tools:
            from astrbot.core.agent.tool import FunctionTool, ToolSet
            kwargs["func_tool"] = ToolSet([FunctionTool(name=t["name"], description=t["description"],
                                                       parameters=t["input_schema"]) for t in request.tools])
            kwargs["tool_choice"] = request.tool_choice
        if request.tool_history:
            kwargs["tool_calls_result"] = [_native_round(round_) for round_ in request.tool_history]
        response = await self.native_provider.text_chat(**kwargs)
        names = getattr(response, "tools_call_name", None) or []
        arguments = getattr(response, "tools_call_args", None) or []
        ids = getattr(response, "tools_call_ids", None) or []
        calls = []
        for index, name in enumerate(names):
            args = arguments[index] if index < len(arguments) else {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(ids[index] if index < len(ids) else f"call_{index}", name,
                                  args if isinstance(args, dict) else {}))
        return ModelResponse(text=getattr(response, "completion_text", None) or "", calls=calls,
                             reasoning_content=getattr(response, "reasoning_content", None),
                             reasoning_signature=getattr(response, "reasoning_signature", None),
                             usage=getattr(response, "usage", None),
                             raw_completion=getattr(response, "raw_completion", None), native=response)
