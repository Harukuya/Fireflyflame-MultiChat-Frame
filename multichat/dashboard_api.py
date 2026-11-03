"""Host-neutral dashboard dispatch. Authentication and HTTP belong to the host.

Success: {"ok": True, ...view fields}. Failure: {"ok": False,
"error": {"code": str, "message": str}}. Never return raw provider exceptions.
"""
from __future__ import annotations

import asyncio
import base64
import os

from . import dashboard
from .validation import entity_id


class DashboardAPIError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DashboardService:
    """One instance per Runtime; hosts must authorize every management request."""

    GET = {"overview", "sessions", "memory", "intimacy", "sticker"}
    POST = {"sessions/add", "sessions/update", "sessions/remove", "memory/run",
            "memory/retire", "reload"}

    def __init__(self, runtime):
        self.runtime = runtime
        self._changes = asyncio.Lock()

    async def handle(self, method, path, params=None, body=None) -> dict:
        try:
            if not isinstance(method, str) or not isinstance(path, str):
                raise DashboardAPIError("invalid_request", "需要有效的方法和接口路径")
            method, path = method.upper(), path.strip("/")
            if path not in self.GET | self.POST:
                raise DashboardAPIError("not_found", "没有这个仪表盘接口")
            if path not in (self.GET if method == "GET" else self.POST if method == "POST" else set()):
                raise DashboardAPIError("method_not_allowed", "这个接口不支持该请求方法")
            data = params if method == "GET" else body
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise DashboardAPIError("invalid_request", "请求内容必须是对象")
            if method == "POST" and (getattr(self.runtime, "closed", False) or getattr(self.runtime, "_stopped", False)):
                raise DashboardAPIError("closed", "框架已停止，不能再安排操作")
            with self.runtime.profile_scope():
                if method == "GET":
                    result = await self._get(path, data)
                elif path == "memory/run":
                    # Do not hold the CRUD lock while waiting for the model:
                    # disabling this session must be able to cancel its memory task.
                    result = await self._memory_run(data)
                else:
                    async with self._changes:
                        result = await self._post(path, data)
            return {"ok": True, **result}
        except DashboardAPIError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not expose model/transport exceptions, credentials, or data text.
            busy = isinstance(exc, RuntimeError) and str(exc) == "Runtime is busy"
            try:
                self.runtime.events.note("仪表盘", "操作失败：" + type(exc).__name__, level="warn")
            except Exception:
                pass
            return {"ok": False, "error": {
                "code": "busy" if busy else "operation_failed",
                "message": "当前有会话任务正在运行，请稍后重试" if busy else "操作未完成，请检查宿主配置和脱敏日志后重试",
            }}

    @staticmethod
    def _session(data: dict) -> tuple[str, str]:
        kind, sid = data.get("kind"), entity_id(data.get("id"))
        if kind not in ("private", "group") or not sid:
            raise DashboardAPIError("invalid_session", "请填写 private/group 会话类型和有效账号或群聊 ID")
        return kind, sid

    @staticmethod
    def _scope(value) -> str:
        if not isinstance(value, str):
            raise DashboardAPIError("invalid_scope", "会话范围必须是 p:账号ID 或 g:群聊ID")
        kind, sep, sid = value.partition(":")
        if not sep or kind not in ("p", "g") or not entity_id(sid):
            raise DashboardAPIError("invalid_scope", "会话范围必须是 p:账号ID 或 g:群聊ID")
        return kind + ":" + entity_id(sid)

    def _owner(self, scope):
        kind, sid = scope.split(":", 1)
        return (self.runtime.engines.get(sid) if kind == "p"
                else self.runtime.hub.chats.get(sid) if self.runtime.hub else None)

    async def _get(self, path: str, data: dict) -> dict:
        p = self.runtime
        if path == "overview":
            return dashboard.overview(p)
        if path == "sessions":
            return {"rows": dashboard.sessions_view(p), "whitelist": False,
                    "note": "会话名单由框架管理；停用或移出名单会中止该会话正在运行的任务。平台权限由宿主负责。"}
        if path == "intimacy":
            return {"rows": dashboard.intimacy_view(p)}
        if path == "memory":
            raw = data.get("scope")
            if raw is None or raw == "":
                primary = f"p:{p.trailblazer}" if p.trailblazer else ""
                raw = primary if primary in p.memories else next(iter(p.memories), "")
            return dashboard.memory_view(p, self._scope(raw) if raw else "")
        return await asyncio.to_thread(self._stickers)

    async def _post(self, path: str, data: dict) -> dict:
        p = self.runtime
        if path.startswith("sessions/"):
            kind, sid = self._session(data)
            if path == "sessions/add":
                name = data.get("name", "")
                memory, proactive = data.get("memory", True), data.get("proactive", False)
                if not isinstance(name, str) or len(name) > 200 or not isinstance(memory, bool) or not isinstance(proactive, bool):
                    raise DashboardAPIError("invalid_value", "名字需为 200 字以内文本，memory/proactive 需为布尔值")
                p.register(kind, sid, name=name, memory=memory, proactive=proactive)
                # Re-adding a previously disabled record creates a fresh session.
                await p.update_session(kind, sid, "enabled", True)
                return {}
            row = p.sessions.book(kind).get(sid)
            if row is None or not row.get("listed"):
                raise DashboardAPIError("not_found", "会话不在名单里")
            if path == "sessions/remove":
                await p.remove_session(kind, sid)
                return {}
            field, value = data.get("field"), data.get("value")
            if field not in {"enabled", "memory", "proactive", "name"}:
                raise DashboardAPIError("invalid_field", "不支持修改这个会话项目")
            if (field == "name" and (not isinstance(value, str) or len(value) > 200)) or (field != "name" and not isinstance(value, bool)):
                raise DashboardAPIError("invalid_value", "名字需为 200 字以内文本，开关需为 true/false")
            if kind == "private" and field == "proactive":
                raise DashboardAPIError("invalid_field", "主动看群开关只适用于群聊")
            if await p.update_session(kind, sid, field, value) is False:
                raise DashboardAPIError("not_found", "会话或项目不存在")
            return {}
        if path == "reload":
            await p.reload_data()
            return {"data": p.data_dir()}
        scope = self._scope(data.get("scope"))
        if scope not in p.memories:
            raise DashboardAPIError("not_found", "这个会话还没有记忆")
        owner = self._owner(scope)
        if owner is not None and getattr(owner, "mem_busy", False):
            raise DashboardAPIError("busy", "这个会话正在整理记忆，请整理结束后再修改")
        item_id, kind = data.get("id"), data.get("kind")
        if (isinstance(item_id, bool) or not isinstance(item_id, int) or not 0 <= item_id <= 10**20
                or kind not in {"episode", "word", "fact", "thread"}):
            raise DashboardAPIError("invalid_memory", "需要有效的记忆类型和非负整数编号")
        if not dashboard.retire(p, scope, kind, item_id):
            raise DashboardAPIError("not_found", "没有找到这条记忆")
        return {}

    async def _memory_run(self, data: dict) -> dict:
        p = self.runtime
        scope = self._scope(data.get("scope"))
        kind, sid = scope.split(":", 1)
        enabled = p.sessions.private_on(sid) if kind == "p" else p.sessions.group_on(sid)
        if not enabled or scope not in p.memories:
            raise DashboardAPIError("not_found", "需要先启用已有聊天记录的会话")
        task = p.spawn_memory(scope, force=True)
        if task is None:
            raise DashboardAPIError("busy", "现在不能整理，请稍后重试")
        try:
            message = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise DashboardAPIError("cancelled", "会话已停用或任务被中止，整理未完成") from None
        return {"message": message or "现在不能整理（模型不支持工具调用，或者正在整理）"}

    def _stickers(self) -> dict:
        stickers = self.runtime.stickers
        if stickers is None:
            return {"library": {}, "dir": ""}
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
        library = {}
        for label, files in stickers.catalog().items():
            items = []
            for name, path in files.items():
                content_type = mime.get(os.path.splitext(path)[1].lower())
                if not content_type:
                    continue
                try:
                    with open(path, "rb") as image:
                        encoded = base64.b64encode(image.read()).decode("ascii")
                    items.append({"name": name, "src": f"data:{content_type};base64,{encoded}"})
                except OSError:
                    continue
            library[label] = items
        return {"library": library, "dir": stickers.root}
