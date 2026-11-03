"""Platform-neutral model orchestration: retries, tool loops and diagnostics.

The host supplies ModelProvider.complete(). SDK conversions belong in adapters.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import time
from dataclasses import dataclass, field

from .model_port import ModelRequest, ModelResponse, ToolCall, ToolResult, ToolRound
from .diagnostics import current_trace, ensure_attempt, request_trace, safe_detail, safe_stack
from .usage import (input_chars, record_request, record_tool_result, retry_attempt,
                    track_tool_models, observe_tool_provider, record_tool_outcome,
                    record_tool_detail, record_tool_model_details)

NARRATIVE = {
    "no-key": ("you", "联络器还没有校准。"),
    "auth": ("you", "联络器的凭证不对，信号接不上。"),
    "bad-endpoint": ("you", "这个频段上没有信号。"),
    "bad-model": ("you", "联络器的频段设置有问题。"),
    "no-network": ("you", "这边的信号断了。"),
    "quota": ("you", "通讯服务的额度不足。"),
    "timeout": ("her", "那边没有回应。"),
    "rate-limit": ("her", "线路有些拥塞。"),
    "server": ("her", "那边没有回应。"),
    "empty": ("her", ""),
    "unknown": ("her", "信号不太稳定。"),
}
RETRYABLE = {"timeout", "rate-limit", "server", "empty"}
TIMEOUT_S = 120


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    calls: list[dict] = field(default_factory=list)
    resp: ModelResponse | None = None  # 通用响应；工具续轮只在本次请求中保留
    kind: str = ""
    detail: str = ""
    exception_type: str = ""
    status_code: int | None = None
    request_id: str = ""  # 仅诊断关联，不进入提示词或聊天记录

    @property
    def narrative(self) -> str:
        return NARRATIVE.get(self.kind, NARRATIVE["unknown"])[1]


def fail(kind: str, detail: str = "", *, exception_type: str = "", status_code: int | None = None) -> ChatResult:
    return ChatResult(ok=False, kind=kind, detail=detail, exception_type=exception_type, status_code=status_code)


def error_diagnostic(result: ChatResult) -> str:
    """日志专用：先完整脱敏再限长；保留行边界以便外层再次打码。"""
    fields = [str(result.kind or "unknown")]
    if getattr(result, "request_id", ""):
        fields.append(f"请求 {result.request_id}")
    if getattr(result, "exception_type", ""):
        fields.append(str(result.exception_type))
    if getattr(result, "status_code", None) is not None:
        fields.append(f"HTTP {result.status_code}")
    detail = str(result.detail or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    if detail:
        fields.append(detail)
    safe = safe_detail(" | ".join(fields))
    return safe[:600] + ("…" if len(safe) > 600 else "")


def _classify_http(status: int, body: str) -> ChatResult:
    err_type, err_msg = "", body
    try:
        j = json.loads(body)
        err_type = (j.get("error") or {}).get("type", "") or ""
        err_msg = (j.get("error") or {}).get("message", body) or body
    except (ValueError, AttributeError):
        pass
    blob = f"{err_type} {err_msg}"
    if re.search(r"quota|billing|insufficient|balance|credit|欠费|余额", blob, re.I):
        return fail("quota", blob)
    if status in (401, 403) or re.search(r"authentication_error|permission_error", err_type):
        return fail("auth", blob)
    if status == 404:
        return fail("bad-model", blob) if re.search(r"model", blob, re.I) else fail("bad-endpoint", blob)
    if status == 402:
        return fail("quota", blob)
    if status == 429 or err_type == "rate_limit_error":
        return fail("rate-limit", blob)
    if status >= 500 or re.search(r"overloaded_error|api_error", err_type):
        return fail("server", blob)
    if re.search(r"model", blob, re.I) and status == 400:
        return fail("bad-model", blob)
    return fail("unknown", f"{status} {blob}")


def classify_exception(e: BaseException) -> ChatResult:
    name = type(e).__name__
    msg = str(e)
    if name == "EmptyModelOutputError":
        return fail("empty", msg, exception_type=name)
    status = getattr(e, "status_code", None)
    if status is None and getattr(e, "response", None) is not None:
        status = getattr(e.response, "status_code", None)
    if isinstance(status, int):
        body = msg
        resp = getattr(e, "response", None)
        try:
            body = resp.text if resp is not None and resp.text else msg
        except Exception:  # noqa: BLE001
            pass
        result = _classify_http(status, body)
        result.exception_type, result.status_code = name, status
        return result
    if isinstance(e, asyncio.TimeoutError) or re.search(r"timeout|timed out|abort", f"{name} {msg}", re.I):
        return fail("timeout", msg, exception_type=name)
    if re.search(r"network|fetch|connect|dns|resolve|ssl|tls", f"{name} {msg}", re.I):
        return fail("no-network", msg, exception_type=name)
    return fail("unknown", f"{name}: {msg}", exception_type=name)


def normalize_turns(messages: list[dict]) -> list[dict]:
    """⚠️ 要以 user 开头、且 user/assistant 交替：相邻的同角色消息合并。"""
    out: list[dict] = []

    def as_blocks(c):
        return ([{"type": "text", "text": c}] if c else []) if isinstance(c, str) else list(c)

    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            last = out[-1]
            if isinstance(last["content"], str) and isinstance(m["content"], str):
                last["content"] = f"{last['content']}\n{m['content']}" if last["content"] and m["content"] else last["content"] or m["content"]
            else:
                last["content"] = as_blocks(last["content"]) + as_blocks(m["content"])
            continue
        out.append(dict(m))
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out




class Llm:
    """一个端点。provider_getter 每次现取当前通用 ModelProvider。"""

    def __init__(self, provider_getter, logger, events=None):
        self._get = provider_getter
        self.log = logger
        self.events = events

    def provider(self):
        return self._get()

    def supports(self, modality: str) -> bool:
        """端点声明了哪些能力；未声明时按支持处理"""
        p = self.provider()
        if p is None:
            return False
        mods = (getattr(p, "provider_config", None) or {}).get("modalities") or []
        return not mods or modality in mods

    @request_trace
    async def chat(self, system: str, messages: list[dict], tools: list[dict] | None = None,
                   tool_choice: str = "auto", tool_result=None) -> ChatResult:
        with ensure_attempt() as attempt:
            started = time.perf_counter()
            resp, failed, cancelled, output_count = None, True, False, 0
            incoming, dispatched, logged_start = 0, False, False
            stage, provider_name = "prepare", "unavailable"

            def failure(result, exc=None):
                result.request_id = attempt.id
                fields = dict(status="failed", kind=result.kind, exceptionType=result.exception_type,
                              elapsedMs=round((time.perf_counter() - started) * 1000), timeoutS=TIMEOUT_S,
                              stage=stage, provider=provider_name, dispatched=dispatched,
                              detail=error_diagnostic(result))
                if result.status_code is not None:
                    fields["http"] = result.status_code
                if exc is not None:
                    fields["stack"] = safe_stack(exc)
                attempt.emit("timeout" if result.kind == "timeout" else "error", **fields)
                return result

            try:
                p = self.provider()
                if p is not None:
                    provider_name = f"{type(p).__module__}.{type(p).__name__}"
                attempt.emit("start", provider=provider_name, timeoutS=TIMEOUT_S)
                logged_start = True
                if p is None:
                    return failure(fail("no-key", "没有可用的模型端点"))
                turns = normalize_turns(messages)
                if not turns:
                    return failure(fail("unknown", "没有可发送的消息"))
                history = list(tool_result or [])
                request = ModelRequest(system=system, messages=turns, tools=list(tools or []),
                                       tool_choice=tool_choice, tool_history=history)
                incoming = input_chars(system, turns, tools, [r.text_payload() for r in history])
                observe_tool_provider(p)
                stage, dispatched = "provider", True
                resp = await asyncio.wait_for(p.complete(request), timeout=TIMEOUT_S)
                stage = "response"
                text = resp.text or ""
                calls = [call.as_dict() for call in resp.calls]
                output_count = input_chars(text, calls)
                # ⚠️ 只调用了工具、没说话，不算空回复——动作照样要执行
                if not text.strip() and not calls:
                    return failure(fail("empty"))
                failed = False
                attempt.emit("success", status="ok", elapsedMs=round((time.perf_counter() - started) * 1000))
                return ChatResult(ok=True, text=text, calls=calls, resp=resp, request_id=attempt.id)
            except asyncio.CancelledError:
                cancelled, failed = True, False
                attempt.emit("cancel", status="cancelled", stage=stage,
                             elapsedMs=round((time.perf_counter() - started) * 1000))
                raise
            except BaseException as e:  # noqa: BLE001
                if not logged_start:
                    attempt.emit("start", provider=provider_name, timeoutS=TIMEOUT_S)
                result = failure(classify_exception(e), e)
                if stage == "provider":
                    return result
                raise  # 准备／解析异常仍遵守原传播规则，只补上诊断
            finally:
                if dispatched:
                    record_request(response=resp, elapsed_ms=(time.perf_counter() - started) * 1000,
                                   failed=failed, cancelled=cancelled, input_count=incoming,
                                   output_count=output_count, events=self.events)

    @request_trace
    async def chat_with_retry(self, system: str, messages: list[dict], tools: list[dict] | None = None,
                              tool_choice: str = "auto", tool_result=None, *, request_valid=None) -> ChatResult:
        """原样请求；瞬态错误静默重试，最多三次，不追加后台触发说明。"""
        if tool_choice == "required":
            return await self._required_result(system, messages, tools, tool_result)
        return await self._chat_with_retry(system, list(messages), tools, tool_choice, tool_result,
                                           request_valid=request_valid)

    async def _chat_with_retry(self, system, messages, tools=None, tool_choice="auto", tool_result=None, *,
                               request_valid=None) -> ChatResult:
        # 重试沿用本次请求的消息；工具结果单独续传，不凭空添加一条用户消息。
        last = fail("unknown")
        trace_round = current_trace().new_round()
        mode = "tool-followup" if tool_result is not None else "original"
        previous = None
        for attempt in range(3):
            if attempt and request_valid is not None and not request_valid():
                previous.emit("stop", reason="invalidated", nextAttempt=attempt + 1)
                return last
            with trace_round.attempt(attempt + 1, 3, mode) as diagnostic:
                previous = diagnostic
                with retry_attempt(attempt > 0):
                    last = await self.chat(system, messages, tools, tool_choice, tool_result)
                if last.ok:
                    return last
                if last.kind not in RETRYABLE or attempt == 2:
                    diagnostic.emit("stop", reason="exhausted" if attempt == 2 else "not-retryable", kind=last.kind)
                    return last
                delay = 0.8 * 2 ** attempt
                diagnostic.emit("retry", reason="transient", nextAttempt=attempt + 2, delayS=delay, kind=last.kind)
                await self._retry_wait(diagnostic, delay)
        return last

    @staticmethod
    async def _retry_wait(diagnostic, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            diagnostic.emit("cancel", status="cancelled", stage="backoff", reason="retry-wait-cancelled")
            raise

    async def _required_result(self, system: str, messages: list[dict], tools: list[dict] | None,
                               tool_result=None) -> ChatResult:
        # 同一个逻辑任务只记一次显式终态。旧分类阶段计数保持不变；并发身份用 ContextVar。
        with track_tool_models() as models:
            state = {"outcome": "failed", "details": [],
                     "tools": sorted({t["name"] for t in (tools or [])
                                      if isinstance(t, dict) and isinstance(t.get("name"), str)})}
            try:
                return await self._required_result_measured(system, messages, tools, tool_result, state)
            except asyncio.CancelledError:
                state["outcome"] = "cancelled"
                raise
            except Exception as error:
                # 准备或解析异常仍向上抛；补上终态明细，不把它伪装成没交工具。
                if not any(item["field"] == "failed" for item in state["details"]):
                    failure = classify_exception(error)
                    self._required_count(state, "failed", failure.kind, failure)
                raise
            finally:
                record_tool_outcome(state["outcome"], models, events=self.events)
                record_tool_model_details(state["outcome"], models, state["details"], events=self.events)

    def _required_count(self, state: dict, field: str, reason: str = "unknown", result=None) -> dict:
        detail = {"reason": reason, "tools": list(state["tools"])}
        if result is not None:
            if getattr(result, "exception_type", ""):
                detail["exception"] = result.exception_type
            if getattr(result, "status_code", None) is not None:
                detail["http"] = result.status_code
        state["details"].append({"field": field, **detail})
        record_tool_result(field, events=self.events)
        record_tool_detail(field, detail, events=self.events)
        return detail

    async def _required_result_measured(self, system: str, messages: list[dict], tools: list[dict] | None,
                                        tool_result, state: dict) -> ChatResult:
        """内部结构化任务：必须交工具结果，但不在 API 上强制 required。

        部分模型或宿主只支持 auto 工具。这里始终保留工具并用 auto，
        自己检查是否交了本次允许的结果；纯文字/调错工具最多补交一次。
        补交与网络重试共用最多三次 complete 的预算，不形成两层重试。
        这里只检查工具名；参数与业务条件仍由记忆、判断等各入口校验。
        """
        def count(field, reason="unknown", result=None):
            return self._required_count(state, field, reason, result)

        count("tasks")
        allowed = {t.get("name") for t in (tools or []) if isinstance(t, dict) and t.get("name")}
        if not allowed:
            current_trace().emit("stop", reason="no-result-tools", kind="missing-tool", dispatched=False)
            count("failed", "no-result-tools")
            return fail("missing-tool", "本次没有可用的结果提交工具")
        names = "、".join(sorted(allowed))
        prompt = system + ("\n\n【本次结果的提交方式】\n"
                           f"请调用本次提供的工具（{names}）提交结果，并填写工具要求的参数。"
                           "只写说明文字或 JSON 正文不算提交结果。")
        repaired = False
        missing_reason = "unknown"
        last = fail("unknown")
        trace_round = current_trace().new_round()
        try:
            for attempt in range(3):
                with trace_round.attempt(attempt + 1, 3, "required-repair" if repaired else "required") as diagnostic:
                    with retry_attempt(attempt > 0):
                        last = await self.chat(prompt, messages, tools, "auto", tool_result)
                    if last.ok:
                        if any(c.get("name") in allowed for c in last.calls if isinstance(c, dict)):
                            if repaired:
                                count("recovered", missing_reason)
                            state["outcome"] = "recovered" if repaired else "firstSuccess"
                            return last
                        diagnostic.emit("required-missing", kind="missing-tool")
                        reason = "wrong-tool" if last.calls else "text-only"
                        count("missingResponses", reason)
                        if not repaired:
                            missing_reason = reason
                            count("firstMissing", reason)
                        if repaired or attempt == 2:
                            last = fail("missing-tool", "模型没有通过本次提供的工具提交结果")
                            last.request_id = diagnostic.id
                            diagnostic.emit("stop", reason="missing-tool", kind=last.kind)
                            missing_reason = reason  # 最终失败展示最后一次实际缺失的类型。
                            break
                        repaired = True
                        count("repairs", reason)
                        # 不把失败答文/思考过程带回，也不写进会话；原始任务与工具原样保留。
                        prompt += ("\n上一次没有提交所需的工具结果。请重新完成上面的任务，"
                                   "这次直接调用相应工具提交，不能只回复说明或 JSON 正文。")
                        diagnostic.emit("retry", reason="required-repair", nextAttempt=attempt + 2, delayS=0)
                        continue
                    if last.kind not in RETRYABLE or attempt == 2:
                        diagnostic.emit("stop", reason="exhausted" if attempt == 2 else "not-retryable", kind=last.kind)
                        break
                    delay = 0.8 * 2 ** attempt
                    diagnostic.emit("retry", reason="transient", nextAttempt=attempt + 2, delayS=delay, kind=last.kind)
                    await self._retry_wait(diagnostic, delay)
        except asyncio.CancelledError:
            count("cancelled", "cancelled")
            raise
        count("failed", missing_reason if last.kind == "missing-tool" else last.kind, last)
        return last

    @request_trace
    async def chat_with_tools(self, system: str, messages: list[dict], tools: list[dict], result_for,
                              ends_turn=None, reads_result=None, max_reads: int = 1, final=None, *,
                              request_valid=None) -> ChatResult:
        """
        带工具的一轮：有限的读取、思考与工具结果回传。
        ⭐ 通常她会先说话、再顺手调用工具，一次请求就够了。
        ⚠️ 她只调用了工具、一句话没说 → 把结果回传给她，让她接着说——只多走这一轮。
        ⭐ 要先拿到结果才能开口的工具（「回想」「想一想」，reads_result）：不管写没写字都回传，这一次写的字不发；
          拿到结果她还可以接着回想、接着想，最多 max_reads 步。走到最后一步把这几个工具收走，她就得开口。
          已经想够了还在调用（个别模型硬调）→ 不再当成「想」，按「只调用工具没写字」处理一次就停。
          一轮最多请求 max_reads + 2 次。
        result_for(call, last)：回传给她的结果（可以是协程）；last = 这是能想的最后一步了。
        ends_turn：调用了就算这轮结束的工具（比如「算了」）——不回传、不让她再开口。
        final：调用了就立刻结束、连「回想」「想一想」也不再走的工具（这边的「不想回」）。
        """
        max_reads = max(1, max_reads)
        request_messages = list(messages)
        res = await self._chat_with_retry(system, request_messages, tools, request_valid=request_valid)
        if not res.ok:
            return res

        calls = list(res.calls)
        rounds: list[ToolRound] = []  # 通用工具历史，按顺序接在本次请求后面
        tools_now = tools
        reads = 0
        nudged = False
        while res.calls:
            if final is not None and any(final(c) for c in res.calls):
                break
            reading = reads_result is not None and reads < max_reads and any(reads_result(c) for c in res.calls)
            if not reading:
                if res.text.strip():
                    break
                if ends_turn is not None and any(ends_turn(c) for c in res.calls):
                    break
                if nudged:
                    break  # 只调用了工具、没说话：只多走这一轮
                nudged = True
            else:
                reads += 1
            last = reading and reads >= max_reads
            rounds.append(await self._tool_round(res, reading, last, result_for))
            # 想够了：把要先拿结果的那几个收走，她就得开口
            if last:
                tools_now = [t for t in tools_now if not reads_result(t)]
            if request_valid is not None and not request_valid():
                current_trace().emit("stop", reason="invalidated", stage="tool-followup")
                return ChatResult(ok=True, text="", calls=calls, resp=res.resp, request_id=res.request_id)
            nxt = await self.chat_with_retry(system, request_messages, tools_now, tool_result=list(rounds),
                                              request_valid=request_valid)
            # 后面哪一次没成功 → 前面的动作照样算数，她只是没说话
            if not nxt.ok:
                current_trace().emit("tool-followup-failed", kind=nxt.kind, request=nxt.request_id,
                                     detail="保留先前动作，本轮无正文")
                return ChatResult(ok=True, text="", calls=calls, resp=res.resp, request_id=nxt.request_id)
            calls += nxt.calls
            res = nxt
        return ChatResult(ok=True, text=res.text, calls=calls, resp=res.resp, request_id=res.request_id)

    @staticmethod
    async def _tool_round(res: ChatResult, reading: bool, last: bool, result_for):
        """这一次的工具调用 + 回传给她的结果（只存在于这次请求里，不进聊天记录）"""
        response = res.resp or ModelResponse(text=res.text,
                                            calls=[ToolCall(c["id"], c["name"], c.get("input") or {}) for c in res.calls])
        results = []
        for call in res.calls:
            result = result_for(call, last)
            if inspect.isawaitable(result):
                result = await result
            results.append(ToolResult(call_id=call["id"], content=result))
        return ToolRound(response=response, results=results, include_text=not reading)



def image_block(media_type: str, b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def b64_of(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()
