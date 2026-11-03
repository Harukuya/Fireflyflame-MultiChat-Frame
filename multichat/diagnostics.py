"""模型诊断：只留请求坐标、结果与脱敏错误，不转储消息、配置或局部变量。"""

from __future__ import annotations

import json
import math
import ntpath
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from uuid import uuid4

from .events import redact


_trace = ContextVar("multichat_request_trace", default=None)
_attempt = ContextVar("multichat_request_attempt", default=None)
_URL = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>]+")
_AUTH_LINE = re.compile(r"(?im)\b(?:proxy-)?authorization(?:\\?[\"'])?\s*[:=]\s*[^\r\n]+")
_PRIVATE_HEADER = re.compile(
    r"(?im)(\b(?:set-cookie|cookie|x-auth-token|x-access-token|x-csrf-token|x-xsrf-token)"
    r"(?:\\?[\"'])?\s*[:=]\s*)[^\r\n]+")
_PAYLOAD_NAMES = (
    "messages", "contexts", "system_prompt", "prompt", "provider_config", "raw_response",
    "response", "request_body", "response_body", "request_payload", "response_payload",
    "payload", "headers", "completion", "tool_calls_result", "image_urls", "audio_urls",
)
_PAYLOAD = re.compile(
    r"(?im)(\b(?:" + "|".join(_PAYLOAD_NAMES) + r")(?:\\?[\"'])?\s*[:=]\s*)")
_PAYLOAD_KEY = re.compile(r"(?i)^(?:" + "|".join(_PAYLOAD_NAMES) + r")$")
_CREDENTIAL_KEY = re.compile(
    r"(?i)^(?:authorization|proxy-authorization|cookie|set-cookie|x-auth-token|api[ _-]?key|"
    r"apikey|x-api-key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|secret|password)$")
_EVENTS = {"start", "success", "error", "timeout", "cancel", "retry", "stop",
           "tool-followup-failed", "required-missing", "stack"}
_DYNAMIC = {"error", "timeout", "cancel", "retry", "stop", "tool-followup-failed", "required-missing"}


def safe_detail(text) -> str:
    """先完整掩凭据，再隐藏 URL 和明显载荷；限长由调用方在脱敏之后处理。"""
    try:
        value = str(text).replace("\r\n", "\n").replace("\r", "\n")
        value = redact(value)
        # Events.note 还会再次 redact。JSON 序列化后的换行是字面 \n，保留
        # Authorization: 前缀会让第二次打码吞掉整条 JSON；改成不能重匹配的标签。
        value = _AUTH_LINE.sub("[Authorization 已隐藏]", value)
        value = _URL.sub("[URL 已隐藏]", value)
        value = _PRIVATE_HEADER.sub(lambda m: m.group(1) + "***", value)
        payload = _PAYLOAD.search(value)
        if payload:
            # 载荷可能跨行，不能只遮住开始一行后把正文续行漏出来。
            value = value[:payload.end()] + "[载荷及后续内容已隐藏]"
        return value
    except Exception:
        return "[诊断文字不可用]"


def safe_stack(exc: BaseException) -> str:
    """保留最多四段异常链与各段末十二帧，不读取源码行、locals 或异常正文。"""
    try:
        return _stack_coordinates(exc)
    except Exception:
        return f"exception: {type(exc).__name__}\n[调用栈不可用]"


def _stack_coordinates(exc: BaseException) -> str:
    pieces, seen = [], set()
    current = exc
    relation = "exception"
    while current is not None and len(seen) < 4 and id(current) not in seen:
        seen.add(id(current))
        pieces.append(f"{relation}: {type(current).__name__}")
        frames = []
        tb = current.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            # ntpath 同时识别 Windows 与正斜杠路径，不输出用户目录或完整路径。
            frames.append(f"  {ntpath.basename(code.co_filename)}:{tb.tb_lineno}:{code.co_name}")
            tb = tb.tb_next
        if len(frames) > 12:
            pieces.append(f"  …省略前 {len(frames) - 12} 帧")
        pieces.extend(frames[-12:])
        if current.__cause__ is not None:
            current, relation = current.__cause__, "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, relation = current.__context__, "context"
        else:
            current = None
    if current is not None:
        pieces.append("…异常链已截断")
    return safe_detail("\n".join(pieces))


def _value(value, depth=0):
    if depth > 3:
        return "[结构已省略]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return safe_detail(value)
    if isinstance(value, (list, tuple)):
        return [_value(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        return {safe_detail(str(k)): "[已隐藏]" if (_PAYLOAD_KEY.fullmatch(str(k)) or _CREDENTIAL_KEY.fullmatch(str(k))) else _value(v, depth + 1)
                for k, v in list(value.items())[:20]}
    # 不调用不受信任对象的 repr 或 str，避免把 Provider / 请求对象展开。
    return f"<{type(value).__name__}>"


class Trace:
    def __init__(self, logger, events=None):
        from .usage import diagnostic_scope

        try:
            owner, category = diagnostic_scope(events)
        except Exception:
            owner, category = events, "其他"
        self.logger = logger
        self.events = owner
        self.category = category
        self.id = uuid4().hex[:12]
        self.round_number = 0
        self.last_round = None

    def new_round(self):
        self.round_number += 1
        self.last_round = Round(self, self.round_number)
        return self.last_round

    def emit(self, event: str, **fields):
        active = _attempt.get()
        if active is not None and active.trace is self:
            active.emit(event, **fields)
            return
        round_ = self.last_round or self.new_round()
        attempt = round_.last_attempt or Attempt(round_, 0, 0, "logical")
        attempt.emit(event, **fields)

    def _backend(self, payload: dict, warn: bool):
        try:
            method = getattr(self.logger, "warning" if warn else "info", None)
            if callable(method):
                method("[MultiChat·模型请求] " + json.dumps(payload, ensure_ascii=False, allow_nan=False))
        except Exception:
            pass

    def _emit(self, attempt, event, fields):
        try:
            if event not in _EVENTS:
                event = "error"
            base = {"task": self.id, "round": attempt.round.number, "attempt": attempt.number,
                    "limit": attempt.limit, "mode": attempt.mode, "category": self.category,
                    "event": event, "id": attempt.id}
            stack = fields.pop("stack", None)
            for key, value in fields.items():
                if key not in base:
                    base[key] = "[已隐藏]" if (_PAYLOAD_KEY.fullmatch(str(key)) or _CREDENTIAL_KEY.fullmatch(str(key))) else _value(value)
            warn = event in {"error", "timeout", "tool-followup-failed", "required-missing", "stack"}
            if event in _DYNAMIC and callable(getattr(self.events, "note", None)):
                try:
                    # note 同时写动态和后台；成功时不能再 _backend 一次。
                    self.events.note("模型请求", json.dumps(base, ensure_ascii=False, allow_nan=False),
                                     level="warn" if warn else "info")
                except Exception:
                    self._backend(base, warn)
            else:
                self._backend(base, warn)
            if stack:
                payload = {k: base[k] for k in ("task", "round", "attempt", "limit", "mode", "category", "id")}
                payload.update(event="stack", stack=safe_detail(stack))
                self._backend(payload, True)
        except Exception:
            # 诊断不可改变模型调用、取消、重试或业务结果。
            pass


class Round:
    def __init__(self, trace, number):
        self.trace, self.number = trace, number
        self.last_attempt = None

    @contextmanager
    def attempt(self, number: int, limit: int, mode: str):
        current = Attempt(self, number, limit, mode)
        self.last_attempt = current
        token = _attempt.set(current)
        try:
            yield current
        finally:
            _attempt.reset(token)


@dataclass
class Attempt:
    round: Round
    number: int
    limit: int
    mode: str

    @property
    def trace(self):
        return self.round.trace

    @property
    def id(self):
        return f"{self.trace.id}.r{self.round.number}.a{self.number}"

    def emit(self, event: str, **fields):
        self.trace._emit(self, event, fields)


def current_trace():
    return _trace.get()


def request_trace(fn):
    @wraps(fn)
    async def traced(self, *args, **kwargs):
        if _trace.get() is not None:
            return await fn(self, *args, **kwargs)
        trace = Trace(getattr(self, "log", None), getattr(self, "events", None))
        token = _trace.set(trace)
        attempt_token = _attempt.set(None)
        try:
            return await fn(self, *args, **kwargs)
        finally:
            _attempt.reset(attempt_token)
            _trace.reset(token)
    return traced


@contextmanager
def ensure_attempt():
    active = _attempt.get()
    if active is not None:
        yield active
        return
    trace = current_trace()
    if trace is None:
        # 正常入口由 request_trace 建立；独立诊断也不应因为缺上下文报错。
        trace = Trace(None)
    with trace.new_round().attempt(1, 1, "direct") as active:
        yield active
