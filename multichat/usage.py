"""只记用量，不留提示词；分类跟着异步任务走，不让并行会话串账。"""

from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps


CATEGORIES = ("群回复", "每轮记事", "长期记忆", "私聊回复", "群判断", "看图转述", "其他")
TOKEN_FIELDS = ("inputTokens", "outputTokens", "cachedTokens", "reasoningTokens")
TOOL_RESULT_FIELDS = ("tasks", "firstMissing", "missingResponses", "repairs", "recovered", "failed", "cancelled")
TOOL_OUTCOME_FIELDS = ("firstSuccess", "recovered", "failed", "cancelled")
_scope = ContextVar("multichat_model_usage_scope", default=None)
_retry = ContextVar("multichat_model_usage_retry", default=False)
_tool_models = ContextVar("multichat_required_tool_models", default=None)


def diagnostic_scope(events=None):
    """诊断复用当前用途和事件归属，不读取模型配置或请求内容。"""
    current = _scope.get()
    owner, category = current if current is not None else (events, "其他")
    return owner if owner is not None else events, category


@contextmanager
def measure(events, category: str):
    """同步 with 可覆盖 await；嵌套时内层类别优先，离开后恢复。"""
    token = _scope.set((events, category if category in CATEGORIES else "其他"))
    try:
        yield
    finally:
        _scope.reset(token)


def measure_async(category: str):
    """给带 self.events 的会话入口分类，不改它的任务/取消行为。"""
    def decorate(fn):
        @wraps(fn)
        async def measured(self, *args, **kwargs):
            with measure(getattr(self, "events", None), category):
                return await fn(self, *args, **kwargs)
        return measured
    return decorate


@contextmanager
def retry_attempt(retry: bool):
    token = _retry.set(retry)
    try:
        yield
    finally:
        _retry.reset(token)


@contextmanager
def track_tool_models():
    """每个必需结果任务各持一份模型身份；并行会话和嵌套任务不会互相覆盖。"""
    models = []
    token = _tool_models.set(models)
    try:
        yield models
    finally:
        _tool_models.reset(token)


def observe_tool_provider(provider) -> None:
    """在实际请求发起前固定身份，仅读取来源 ID/型号，绝不保存 Provider 配置对象。"""
    models = _tool_models.get()
    if models is None:
        return
    try:
        cfg = getattr(provider, "provider_config", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        name = cfg.get("id")
        getter = getattr(provider, "get_model", None)
        try:
            model = getter() if callable(getter) else None
        except Exception:
            model = None
        if not isinstance(model, str) or not model.strip():
            model = cfg.get("model")
        if not isinstance(model, str) or not model.strip():
            model_cfg = cfg.get("model_config")
            model = model_cfg.get("model") if isinstance(model_cfg, dict) else None
        identity = {"provider": name if isinstance(name, str) else "未识别来源",
                    "model": model if isinstance(model, str) else "未提供型号"}
        if identity not in models:
            models.append(identity)
    except Exception:
        pass


def record_tool_outcome(outcome: str, models, events=None) -> None:
    """终态独立于旧阶段计数，不从开始数减失败数推算成功，也不改变真实任务结果。"""
    if outcome not in TOOL_OUTCOME_FIELDS:
        return
    try:
        current = _scope.get()
        owner, category = current if current is not None else (events, "其他")
        if owner is None:
            owner = events
        record = getattr(owner, "record_tool_outcome", None)
        if callable(record):
            record(category, outcome, models)
    except Exception:
        pass


def record_tool_model_details(outcome: str, models, details, events=None) -> None:
    """终态时一次提交本轮阶段明细，沿用实际模型归属；观察失败不影响任务。"""
    if outcome not in TOOL_OUTCOME_FIELDS:
        return
    try:
        owner, category = diagnostic_scope(events)
        record = getattr(owner, "record_tool_model_details", None)
        if callable(record):
            record(category, outcome, models, details)
    except Exception:
        pass


def record_tool_detail(field: str, detail: dict, events=None) -> None:
    """只记录阶段、原因代码和允许提交的工具名，不保存答文或错误正文。"""
    if field not in TOOL_RESULT_FIELDS:
        return
    try:
        owner, category = diagnostic_scope(events)
        record = getattr(owner, "record_tool_detail", None)
        if callable(record):
            record(category, field, detail)
    except Exception:
        pass


def _get(obj, key):
    try:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    except Exception:
        return None


def _number(value):
    # bool 是 int 的子类；小数、负数、NaN/Infinity 和数字字符串都不是可信 token 数。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value) if value >= 0 else None


def _valid_tokens(result: dict) -> dict:
    # 两个细分都属于总数的一部分；矛盾的细分视为未报告，不让它污染合计。
    for part, total in (("cachedTokens", "inputTokens"), ("reasoningTokens", "outputTokens")):
        if part in result and total in result and result[part] > result[total]:
            result.pop(part)
    return result if any(result.values()) else {}


def tokens_of(response) -> dict:
    """AstrBot 4.27 TokenUsage，及兼容 Provider 原样返回的常见 usage。

    AstrBot 的 input_other 不含缓存，input_cached 需加回输入。它缺少原始
    usage 时也会给 TokenUsage()，所以全零按未知处理，绝不冒充零成本。
    这里只承诺统计 Provider 报告的字段，不据此猜测价格或未报告的 token。
    """
    # AstrBot 归一化后会省掉推理/缓存写入细分，有些 Provider 还会把缓存
    # 加重一次。保留了原始响应时优先读它的 usage，只读数值，不留响应本身。
    raw = _get(response, "raw_completion")
    usage = _get(raw, "usage")
    if usage is not None:
        return _tokens_of_usage(usage)
    metadata = _get(raw, "usage_metadata")
    if metadata is not None:
        result = {}
        prompt = _number(_get(metadata, "prompt_token_count"))
        tool = _number(_get(metadata, "tool_use_prompt_token_count"))
        output = _number(_get(metadata, "candidates_token_count"))
        reasoning = _number(_get(metadata, "thoughts_token_count"))
        cached = _number(_get(metadata, "cached_content_token_count"))
        if prompt is not None:
            # Gemini 的 prompt 已含 cached；工具结果另算输入、thoughts 算输出。
            result["inputTokens"] = prompt + (tool or 0)
        if output is not None:
            result["outputTokens"] = output + (reasoning or 0)
        if cached is not None:
            result["cachedTokens"] = cached
        if reasoning is not None:
            result["reasoningTokens"] = reasoning
        return _valid_tokens(result)
    return _tokens_of_usage(_get(response, "usage"))


def _tokens_of_usage(usage) -> dict:
    if usage is None:
        return {}
    result = {}
    other, cached = _number(_get(usage, "input_other")), _number(_get(usage, "input_cached"))
    if other is not None or cached is not None:
        if other is not None and cached is not None:
            result["inputTokens"] = other + cached
        if cached is not None:
            result["cachedTokens"] = cached
        output = _number(_get(usage, "output"))
    else:
        prompt = _number(_get(usage, "prompt_tokens"))
        incoming = _number(_get(usage, "input_tokens"))
        if prompt is not None:
            result["inputTokens"] = prompt
        elif incoming is not None:
            result["inputTokens"] = incoming
        output = _number(_get(usage, "completion_tokens"))
        if output is None:
            output = _number(_get(usage, "output_tokens"))
        details = _get(usage, "prompt_tokens_details") or _get(usage, "input_tokens_details")
        cached = _number(_get(details, "cached_tokens"))
        if cached is not None:
            result["cachedTokens"] = cached
        # 原始 Anthropic 的 input_tokens 不含缓存读/写；都报告时加回。
        cache_read = _number(_get(usage, "cache_read_input_tokens"))
        cache_write = _number(_get(usage, "cache_creation_input_tokens"))
        if cache_read is not None:
            result["cachedTokens"] = cache_read
        if incoming is not None and prompt is None and (cache_read is not None or cache_write is not None):
            result["inputTokens"] = incoming + (cache_read or 0) + (cache_write or 0)
    if output is not None:
        result["outputTokens"] = output
    details = _get(usage, "completion_tokens_details") or _get(usage, "output_tokens_details")
    reasoning = _number(_get(details, "reasoning_tokens"))
    if reasoning is None:
        reasoning = _number(_get(usage, "reasoning_tokens"))
    if reasoning is not None:
        result["reasoningTokens"] = reasoning
    return _valid_tokens(result)


def _text_chars(value, depth=0) -> int:
    """计请求里的文本字符，不计图片/base64、角色标识和对象 repr。"""
    if depth > 12 or value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(_text_chars(x, depth + 1) for x in value)
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_url", "input_image"):
            return 0
        if "content" in value:
            return _text_chars(value["content"], depth + 1)
        return sum(_text_chars(v, depth + 1) for k, v in value.items()
                   if k not in ("role", "type", "id", "tool_call_id", "data", "image_url", "source"))
    # AstrBot ToolCallsResult / MessageSegment / ContentPart 的文本字段。
    return sum(_text_chars(_get(value, key), depth + 1) for key in
               ("tool_calls_info", "tool_calls_result", "content", "text", "think", "tool_calls", "function", "arguments"))


def input_chars(system, messages, tools=None, tool_result=None) -> int:
    try:
        return _text_chars(system) + _text_chars(messages) + _text_chars(tools) + _text_chars(tool_result)
    except Exception:
        return 0


def record_request(*, response, elapsed_ms, failed=False, cancelled=False,
                   input_count=0, output_count=0, events=None) -> None:
    """统计是附带工作，Provider/盘上的怪数据或写盘失败都不能影响聊天。"""
    try:
        current = _scope.get()
        owner, category = current if current is not None else (events, "其他")
        if owner is None:
            owner = events
        record = getattr(owner, "record_model_usage", None)
        if not callable(record):
            return
        record(category, {"retries": int(_retry.get()), "failed": int(failed),
                          "cancelled": int(cancelled), "elapsedMs": max(0, round(elapsed_ms)),
                          "inputChars": max(0, input_count), "outputChars": max(0, output_count),
                          "tokens": tokens_of(response)})
    except Exception:
        pass


def record_tool_result(field: str, events=None) -> None:
    """必需工具结果的业务计数，与请求是否成功、是否返回 token 用量分开。

    一次逻辑任务可以产生多次 Provider 请求；这里只记指定的阶段，不再次
    累加 model_usage。统计失败不能改变业务结果，也不保存模型正文。
    """
    if field not in TOOL_RESULT_FIELDS:
        return
    try:
        current = _scope.get()
        owner, category = current if current is not None else (events, "其他")
        if owner is None:
            owner = events
        record = getattr(owner, "record_tool_result", None)
        if callable(record):
            record(category, field)
    except Exception:
        pass
