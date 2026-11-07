"""按业务用途选择模型；作用域跟随异步任务，不修改共享 Llm 对象。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect


ROUTES = (
    {"id": "private_reply", "label": "私聊回复", "hint": "普通私聊及对新来话的回复；工具能力按所选模型决定。", "requires": ""},
    {"id": "group_reply", "label": "群聊回复", "hint": "被叫到、接话窗口和看手机决定开口后的正式回复。", "requires": ""},
    {"id": "catchup", "label": "上线补发／补看", "hint": "上线后回复私聊留言、补看群里的呼叫。", "requires": ""},
    {"id": "proactive", "label": "主动联系", "hint": "没有新来话时主动发起的私聊。", "requires": ""},
    {"id": "thought", "label": "念头／追问掂量", "hint": "到点决定继续等、放下或开口，需要工具调用。", "requires": "tool_use"},
    {"id": "farewell", "label": "独立道别", "hint": "临下线单独发起的道别；随正常回复带出的道别仍使用该轮模型。", "requires": ""},
    {"id": "closing", "label": "道别后最后应答", "hint": "已道别后，每会话最后一次简短回应。", "requires": ""},
    {"id": "offline_reply", "label": "离线短应答", "hint": "忙碌或休息时给主要联系人的简短通知；其他固定通知不调用模型。", "requires": ""},
    {"id": "window_judge", "label": "接话窗口判断", "hint": "结合本轮消息及前两轮记事，判断是否继续接话；需要工具调用。", "requires": "tool_use"},
    {"id": "look_judge", "label": "看手机兴趣判断", "hint": "主动阅读群消息时判断兴趣、多看或接话；需要工具调用。", "requires": "tool_use"},
    {"id": "summary", "label": "临时记事摘要", "hint": "把一段群交流写成角色口吻的临时记事，保留原有口吻提示。", "requires": ""},
    {"id": "memory", "label": "长期记忆整理与印象", "hint": "筛选长期事件、事实、悬事并更新对人的印象；需要工具调用。", "requires": "tool_use"},
    {"id": "memory_haze", "label": "模糊记忆合并", "hint": "把符合条件的旧记忆合成整体印象；不要求工具调用。", "requires": ""},
    {"id": "memory_tags", "label": "旧记忆话题补全", "hint": "为缺少话题标签的旧记忆补齐检索标签；需要工具调用。", "requires": "tool_use"},
    {"id": "caption", "label": "图片转述", "hint": "当消费该内容的模型或流程只读文字时补充图片描述；需要看图能力。", "requires": "image"},
)
ROUTE_IDS = frozenset(row["id"] for row in ROUTES)


@dataclass
class _Scope:
    purpose: str
    providers: dict = field(default_factory=dict)


_scope = ContextVar("multichat_request_model", default=None)


def current_purpose() -> str:
    scope = _scope.get()
    return scope.purpose if scope is not None else ""


@contextmanager
def model_scope(purpose: str):
    """新业务请求总是新建；工具续传/重试不重新进入。退出或取消都恢复父上下文。"""
    if purpose not in ROUTE_IDS:
        raise ValueError("Unknown model purpose")
    token = _scope.set(_Scope(purpose))
    try:
        yield
    finally:
        _scope.reset(token)


def pin_provider(key, getter):
    """key 由宿主传 (宿主标识, 会话, 用途)，缓存 None 也不在同一轮偷偷换模型。"""
    scope = _scope.get()
    if scope is None:
        return getter()
    if key not in scope.providers:
        scope.providers[key] = getter()
    return scope.providers[key]


def request_model(purpose):
    """str 或接收原函数全部参数的同步 callable；可装饰同步调度及 async 业务入口。"""
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapped(*args, **kwargs):
                selected = purpose(*args, **kwargs) if callable(purpose) else purpose
                with model_scope(selected):
                    return await fn(*args, **kwargs)
        else:
            @wraps(fn)
            def wrapped(*args, **kwargs):
                selected = purpose(*args, **kwargs) if callable(purpose) else purpose
                with model_scope(selected):
                    return fn(*args, **kwargs)
        return wrapped
    return decorate
