"""
私聊：一轮回复 + 后台心跳（原版 ChatScreen.tsx 的 runTurn、App.tsx 的那几个 effect、catchup.ts、proactive.ts）

账号 里只有一条路：原版对话页开着时走 ChatScreen、不在时走 App 层；这里统一成
「回他的话走 run_turn，其余（补发、道别、到点掂量、主动找他、整理记忆）都在 30 秒的心跳里」。
⭐ 她说话（回话、补发、道别、掂量、主动）同一时间只有一处在进行（in_flight）；
  这期间他又说话了，就记下来（more_came），等她这一轮说完再排下一轮。

这边新加的（原版没有）：
  - peer：不是主要联系人的私聊（群友）。身份那一行换掉、另带亲密度认知、不留念头、不主动找、精力消耗多些；她自己的情绪全局共用
  - 表情包工具、主要联系人那边的时间、开头提醒、撤回、分段开关、动态日志
  - 「不想回」工具：群友私聊一直有，主要联系人看设置
  - 2026-10-29 原版更新：多想几步（想一想 + 接着回想，步数跟精力走）、回他的方式（群友不给「索取」）
"""

from __future__ import annotations

from . import profile

import asyncio
import copy
import inspect
import math
import os
import random
import re
from contextlib import asynccontextmanager

from . import activity, farewell, identity, offline, persona, winext
from .caption import image_note
from .quotes import normalize_quote, render_quote, resolve_quote
from .energy import (
    PEER_DELTA, PEER_REAL_BONUS, TOO_TIRED_TO_ASK, apply_round, current_energy, energy_factor, take_energy_note, think_steps,
)
from .heat import chat_heat, plant_chance
from .events import redact
from .jsutil import HOUR, MIN, day_of, message_time, now_ms
from .llm import Llm, b64_of, classify_exception, error_diagnostic, image_block
from .mood import mood_instruction, on_emotion, settle
from .model_routes import model_scope, request_model
from .pacing import schedule as pace, seen_delay
from .sanitize import detect_modern_slang, to_messages
from .schedule import describe_activity, offline_after, online_since, state_at
from .thought import (
    ago_text, available_tiers, curious_thought, due_time, is_alive, note_delivered, plan_thought, plant_thought,
    postpone, user_spoke_since,
)
from .tools import LET_GO, ends_turn, intent_from_calls, reads_result, result_for, tools_for
from .usage import measure_async
from .ways import ASKING_STREAK, asking_streak, available_ways, note_way, peer_ways

MIN_BATCH_MS = 5000
MAX_BATCH_MS = 15000
FAREWELL_ON_REPLY_MIN = 12
FAREWELL_WINDOW_MIN = 10
RECENT_ACTIVITY_MIN = 3
PROACTIVE_MAX_PER_DAY = 1
MIN_QUIET_HOURS = 5
NEGLECT_HOURS = 48
NIGHT_CHANCE = 0.55
DAY_CHANCE = 0.3
CATCHUP_RETRY_MS = 5 * MIN
_SENTENCE = re.compile(r"(?<=[。！？!?…])")


def split_sentences(parts: list[str]) -> list[str]:
    """「一行里的几句也拆开」：按句号、问号、叹号拆，保留全部句子。"""
    out = []
    for p in parts:
        out += [s for s in (x.strip() for x in _SENTENCE.split(p)) if s]
    return out


def history_tail(history: list[dict]) -> list[dict]:
    """最后一条她的话之后，他说的那几条"""
    out = []
    for m in reversed(history):
        if m.get("system"):
            continue
        if m["role"] != "user":
            break
        out.append(m)
    return out


def ends_turn_or_sticker(call: dict) -> bool:
    """只发了个表情、一句话没说，或者不想回：这轮就这样了，不用回传再让她开口"""
    return ends_turn(call) or call["name"] in ("send_sticker", "no_reply")


WAIT_MS = 20_000  # 一轮里等图片转述最多等多久（caption.py 的 WAIT_S）
PREP_BUDGET_MS = 30_000  # 一批消息因为「准备时又变了」最多重来多久，之后先按现在的记录回


class Engine:
    def __init__(self, store, llm: Llm, memory, knowledge, people, style, sender, logger,
                 failure_notice: bool = True, peer: dict | None = None, events=None, stickers=None, intimacy=None,
                 options=None, scope: str = "", captions=None):
        self.store = store
        self.llm = llm
        self.memory = memory
        self.knowledge = knowledge
        self.people = people
        self.style = style
        self.sender = sender
        self.log = logger
        self.failure_notice = failure_notice
        self.peer = peer  # None = 主要联系人；{"qq", "name"} = 群友
        self.events = events
        self.stickers = stickers
        self.intimacy = intimacy
        self.options = options or (lambda: {})
        self.scope = scope
        self.captions = captions  # 看不了图时先转述（caption.py）：async (msgs, saved)
        self.recent_context = None  # 主插件注入的同步共享背景；只进本轮系统提示，不进本地消息或整理材料。

        self.in_flight = False
        self.more_came = False
        self.batch_task: asyncio.Task | None = None
        self.batch_start = 0
        self.req_task: asyncio.Task | None = None
        self.restarted = False
        # 只在内存里、重启就清的
        self.last_meaningful = False
        self.farewell_said = False
        self._farewell_edge = None  # 本轮道别预计的下线点；首条送出才持久化锁。
        self._farewell_grace = False  # 仅一次模型生成的收尾可越过发送门禁；新话不掐掉它。
        self.proactive_tried_hour = -1
        self._proactive_sending = None  # 主动开场首条送出便持久化，取消也不会丢。
        self.catchup_failed_at = 0
        self.mem_busy = False
        self.catching_up = False
        self._beat_busy = False
        self._running: set[asyncio.Task] = set()  # 正在跑的那一轮、补发、整理记忆（卸载插件时一起停）
        self.rev = 0  # 聊天记录的版本：他每说一句、每撤回一条 +1（准备的这阵子变了没有，看它）
        self.closed = False  # 卸载了：什么都不再接、不再排、不再记
        self.catchup_again = False  # 补发被打断了（准备的时候他又说了 / 撤回了）：做完马上再补一次
        self.catchup_saying = False  # 补发已经在一条条发了（这时他说的算新的话，不再并进补发）
        self._asked: list[str] | None = None  # 这次交给模型的他的那几条（第一条发出去时算回过）
        self._recent_input: dict[str, dict] = {}  # 真实入模内容快照；seen 也包含没进工作集的旧话，不能拿来共享。
        self._recent_complete = False
        self._recent_sent = False
        self._recent_guard = None
        self.round_ids: set[str] = set()  # 这一轮要回的他的那几条（回话：开始时还没看到的；补发：那几条留言）
        self.recall_cut = False  # 这一轮要回的被他撤回了：直接打断，不重来、不补（用户 2026-10-30 定）
        self.prep_since = 0  # 这一批从什么时候开始准备（一直有新消息、一直重来，超过 PREP_BUDGET_MS 就先按现在的回）
        self._pending_offline_notice: dict | None = None
        self._offline_notice_task: asyncio.Task | None = None
        self._offline_notice_request: asyncio.Task | None = None
        self._offline_notice_ids: set[str] = set()  # 独立于正式回话目标，撤回通知不清正式留言。

        t = store.thought
        if t and not is_alive(t, now_ms()):
            store.thought = None
        store.mood = settle(store.mood)
        self._migrate_flags()
        store.save_state()

    def _migrate_flags(self) -> None:
        """（这边新加的）看过 / 回过记在每条消息上（seen / answered），不再靠「看到哪一条为止」那一个 id（那条撤回了就找不到）。
        以前的记录升级一次：
        · 记过「看到哪一条为止」（seenUpto）：那一条及以前的算看过，其中后面有她的话的算回过；之后的还没看到（照样会回）
        · 没记过：她最后一句之前的都算看过、回过，之后的还没看到"""
        x = self.store.extra
        if x.get("flags") == 1:
            return
        hist = self.history
        her_at = [i for i, m in enumerate(hist) if m["role"] == "her" and not m.get("system")]
        last_her = her_at[-1] if her_at else -1
        upto = next((i for i, m in enumerate(hist) if m["id"] == x.get("seenUpto")), None)
        for i, m in enumerate(hist):
            if m["role"] != "user" or m.get("system"):
                continue
            if upto is not None:
                if i <= upto:
                    m["seen"] = True
                    if any(j > i for j in her_at):
                        m["answered"] = True
            elif i < last_her:
                m["seen"] = m["answered"] = True
        x.pop("seenUpto", None)
        x["flags"] = 1
        self.store.save_messages()

    @property
    def history(self) -> list[dict]:
        return self.store.messages

    def _track(self, task: asyncio.Task | None) -> None:
        if task is not None:
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    def _spawn(self, coro) -> asyncio.Task | None:
        if self.closed:
            coro.close()
            return None
        task = asyncio.get_running_loop().create_task(coro)
        self._track(task)
        return task

    @staticmethod
    def _parent_cancelled() -> bool:
        """整个任务在被取消（卸载插件、心跳停了），不是撤回、他又说话掐掉的那次请求"""
        t = asyncio.current_task()
        return bool(t and t.cancelling())

    def _note(self, kind: str, text: str, level: str = "info") -> None:
        if self.events:
            self.events.note(kind, text, self.scope, level)
        else:
            (self.log.warning if level == "warn" else self.log.info)(f"[MultiChat·{kind}] {redact(text)}")

    def _count(self, key: str) -> None:
        if self.events:
            self.events.count(key)

    def _leaving(self) -> dict | None:
        now = now_ms()
        before = self.store.extra.get("farewellLock")
        state = farewell.active(self.store, now, state_at(now).online)
        if before and state is None:
            self.farewell_said = False
        return state

    def _commit_farewell(self) -> None:
        if self._farewell_edge is None:
            return
        farewell.begin(self.store, now_ms(), self._farewell_edge)
        self.farewell_said = True
        # 道别生成/发送期间才来的话也留到下次上线，不能被心跳当普通回复接着聊。
        for m in self._pending():
            m["undelivered"] = True
            m["arrivedOnline"] = True
        self.store.save_messages()

    @request_model("closing")
    async def _farewell_reply(self) -> None:
        state = self._leaving()
        if self.closed or self.in_flight or not state or state.get("used") or not state_at(now_ms()).online:
            return
        selected = next((m for m in self.history if m.get("farewellFollowup") and m.get("undelivered")
                         and not m.get("farewellTried")), None)
        if selected is None:
            return
        selected_msgs = [m for m in self.history if m is selected or (selected.get("qid")
                         and m.get("qid") == selected["qid"] and m.get("role") == "user")]
        async with self._speaking():
            self.round_ids = {m["id"] for m in selected_msgs}
            self._asked = [m["id"] for m in selected_msgs]
            self._farewell_grace = True
            try:
                previous = [m for m in self.memory.working_set(
                                self.history[:self.history.index(selected)], private=True)
                            if m.get("role") == "her" or m.get("seen")]
                identity = (winext.peer_identity(self.peer["name"]) if self.peer else
                            f"你正在和主要联系人{profile.primary_name()}私聊，关系以角色设定和实际互动为准。")
                relationship = (self.intimacy.brief(self.peer["qq"], self.peer["name"])
                                if self.peer and self.intimacy else
                                self.intimacy.trailblazer_note() if self.intimacy else "")
                for m in selected_msgs:
                    m["farewellTried"] = True
                self.store.save_messages()
                # 轻量收尾最多交每条前600字；不能把被截掉的长文整条发布成「确实看过」。
                self._capture_recent_read([
                    m for m in [*previous[-6:], *selected_msgs]
                    if len(image_note(m.get("attachment")) + (m.get("text") or "")
                           + (f"［发了一个表情：{m['sticker']}］" if m.get("sticker") else "")) <= 600
                    and len((normalize_quote(m.get("quote")) or {}).get("text", "")) <= 400
                ])
                self._count("模型调用")
                recent_memory, _ = self.memory.recent_private_context(now_ms())
                followup_system = "\n\n".join(x for x in [
                    farewell.followup_system(identity, relationship, now=now_ms(), state_fn=state_at), recent_memory] if x)
                self.req_task = asyncio.create_task(asyncio.wait_for(
                    self.llm.chat(followup_system,
                                  farewell.followup_turns(previous, selected_msgs)),
                    timeout=farewell.FOLLOWUP_TIMEOUT_S))
                try:
                    result = await self.req_task
                except asyncio.CancelledError:
                    if self._parent_cancelled():
                        raise
                    if self.recall_cut:
                        self._drop_round()
                    return
                except Exception as exc:
                    self._count("模型失败")
                    self._note("出错", f"道别收尾没生成：{error_diagnostic(classify_exception(exc))}；这条不自动重试", "warn")
                    return
                finally:
                    self.req_task = None
                if self.recall_cut:
                    self._drop_round()
                    return
                if not result.ok:
                    self._count("模型失败")
                    self._note("道别", f"收尾没有生成可用的话：{error_diagnostic(result)}；这条不自动重试", "warn")
                    return
                text = farewell.followup_text(result.text)
                if (not text or self.closed or not state_at(now_ms()).online
                        or self._leaving() is not state or state.get("used")):
                    return
                self._recent_complete = True
                self._recent_guard = lambda: not self.closed and state_at(now_ms()).online and self._leaving() is state
                sent = await self.say([text], guard=lambda: not self.closed and selected in self.history
                                      and state_at(now_ms()).online and self._leaving() is state and not state.get("used"))
                if self.recall_cut:
                    self._drop_round()
            finally:
                self._farewell_grace = False
            if sent:
                for m in selected_msgs:
                    m["seen"] = True
                    m.pop("undelivered", None)
                    m.pop("farewellFollowup", None)
                self.store.save_messages()
                self.store.save_state()
                self._count("回话")
                self._note("道别", "又简短回了一句；本会话等下次上线再聊")

    # ============================================================
    #  他发来消息
    # ============================================================

    async def on_user_message(self, text: str, attachments: list[dict], qid: str | None = None,
                              quote: dict | None = None) -> None:
        if self.closed:
            return
        now = now_ms()
        her = state_at(now)
        leaving = self._leaving()
        msgs = [self.store.new_msg("user", text if i == 0 else "", attachment=a, ts=now)
                for i, a in enumerate(attachments)] or [self.store.new_msg("user", text, ts=now)]
        quoted = resolve_quote(quote, self.history)
        if quoted:
            msgs[0]["quote"] = quoted  # 多图拆分仍是一条平台来话：引用只放第一条。
        for m in msgs:
            if qid:
                m["qid"] = qid  # 一条 账号 消息带几张图会拆成几条：撤回时一起拿掉
            if self.peer:
                m["uid"], m["name"] = self.peer["qq"], self.peer["name"]

        # ⭐ 她正在回信，而他又说了话：直接掐掉这次请求，带着完整上下文重来（每轮只掐一次）
        if self.req_task and not self.req_task.done() and not self.restarted and not self._farewell_grace:
            self.restarted = True
            self.req_task.cancel()

        backlog = any(m.get("undelivered") for m in self.history)
        # ⭐ 她不在的时候，消息留作留言；主要联系人可由模型写一句通知，不进入正常聊天。
        #   她刚上线、还有没看的：这条也算在补看里（补看已经在一条条发了就不算，那是新的话，补看完接着回）
        lie = bool(leaving) or not her.online or (backlog and not (self.catching_up and self.catchup_saying))
        for m in msgs:
            if lie:
                m["undelivered"] = True
                if her.online:
                    m["arrivedOnline"] = True  # 她在线时才来的，只是并进补发（不是留言）
            if leaving and her.online:
                m["farewellFollowup"] = True
            self.history.append(m)
        opening = self.store.extra.get("proactiveOpening")
        if isinstance(opening, dict) and opening.get("segment") == online_since(now // MIN * MIN) and her.online:
            opening["replies"] = list(dict.fromkeys([*opening.get("replies", []), *[m["id"] for m in msgs]]))
            self.store.save_state()
        self.rev += 1
        self.store.save_messages()
        self._note("收到", f"{text[:40] or '［图片］'}{'（她不在，先躺着）' if not her.online else ''}")
        if not her.online:
            if self.batch_task is not None:
                self.batch_task.cancel()
                self.batch_task = None
                self._lie_down()
            self._queue_offline_notice(msgs, now)
            return
        if leaving:
            if not leaving.get("used"):
                self.schedule_turn()
            return
        if lie:
            # 她刚上线、心跳还没轮到：现在就补看
            self._spawn(self._catchup_now())
            return
        self.schedule_turn()

    def _queue_offline_notice(self, msgs: list[dict], now: int) -> None:
        # 插件 options 明确提供默认 True；独立 Engine 没配置此功能时不擅自发状态通知。
        if not self.options().get("offline_reply", False):
            return
        key = offline.segment_key(now, state_at)
        if key is None or offline.matched(self.store.extra.get("offlineNotice"), key):
            return
        ids = [m["id"] for m in msgs]
        if self._pending_offline_notice and self._pending_offline_notice["key"] == key:
            ids = list(dict.fromkeys([*self._pending_offline_notice["ids"], *ids]))
        self._pending_offline_notice = {"key": key, "ids": ids}
        self._start_offline_notice()

    def _start_offline_notice(self) -> None:
        if (self.closed or self._busy() or not self._pending_offline_notice
                or (self._offline_notice_task is not None and not self._offline_notice_task.done())):
            return
        self._offline_notice_task = self._spawn(self._flush_offline_notice())

    @measure_async("离线应答")
    @request_model("offline_reply")
    async def _flush_offline_notice(self) -> None:
        """离线通知与正常发送串行；主联系人用模型短应答，不清留言、不算一次回话。"""
        try:
            while not self.closed and self._pending_offline_notice and not self._busy():
                pending, self._pending_offline_notice = self._pending_offline_notice, None
                now = now_ms()
                state_kind = offline.kind(state_at(now))
                key = offline.segment_key(now, state_at)
                if (key != pending["key"] or state_kind is None
                        or offline.matched(self.store.extra.get("offlineNotice"), key)
                        or not self.options().get("offline_reply", False)
                        or not any(m["id"] in pending["ids"] and m.get("undelivered") for m in self.history)):
                    continue
                async with self._speaking():
                    targets = [m for m in self.history if m["id"] in pending["ids"] and m.get("undelivered")]
                    def valid():
                        return (not self.closed and offline.segment_key(now_ms(), state_at) == key
                                and self.options().get("offline_reply", False)
                                and bool(targets)
                                and all(m in self.history and m.get("undelivered") and not m.get("recalled") for m in targets))

                    text = None
                    if self.peer is None and valid():
                        system = winext.offline_ack_prompt(now, state_kind,
                            mood_instruction(settle(self.store.mood, now)["value"]), state_fn=state_at,
                            cognition=self.intimacy.trailblazer_note() if self.intimacy else "")
                        turns = [{"role": "user", "content": "\n".join(
                            render_quote(m) + (m.get("text") or "") + image_note(m.get("attachment")) for m in targets)
                            or "【通知触发】对方发来一条空消息；仅作离线短通知，不猜测消息内容。"}]
                        def track_notice(task):
                            self._offline_notice_request = task
                            self._offline_notice_ids = {m["id"] for m in targets}
                            self._track(task)

                        try:
                            text = await offline.short_reply(self.llm, system, turns, valid=valid,
                                                            count=self._count, note=self._note, track=track_notice)
                        except asyncio.CancelledError:
                            if self._parent_cancelled():
                                raise
                            continue  # 目标撤回只取消短通知，不改剩余留言；后来来话继续排队。
                        finally:
                            self._offline_notice_request = None
                            self._offline_notice_ids.clear()
                    text = text or offline.text(self.options(), state_kind, trailblazer=self.peer is None)
                    if not text or not valid():
                        continue
                    # 实际离线时允许状态通知；在线时道别后的聊天封口规则不变。
                    try:
                        sent = await self.sender.send(text)
                    except Exception as exc:
                        self._note("出错", f"离线状态通知没发出去：{type(exc).__name__}", "warn")
                        continue
                    if sent:
                        self.store.extra["offlineNotice"] = key
                        if self.peer is None:
                            self.store.extra["offlineNoticeText"] = {"text": text, "ts": now_ms(), "segment": key}
                        self.store.save_state()
                        self._count("自动回复")
                        self._note("自动回复", text)
                    else:
                        self._note("出错", "离线状态通知没发出去，下次收到新消息再试", "warn")
        finally:
            self._offline_notice_task = None
            self._start_offline_notice()


    async def on_recall(self, qid: str) -> None:
        """他撤回了一条：本轮目标直接打断；已经稳定读过的保留同步记录，没读完的拿掉。
        「回过」= 这条在她真发出去的那一轮里（answered，记在消息上）"""
        if self.closed:
            return
        matched = [m for m in self.history if m.get("qid") == qid and m["role"] == "user"]
        if not matched:
            return
        gone = [m for m in matched if not m.get("answered")]
        if not gone:
            self._note("撤回", "他撤回了一条，她已经回过了，不动")
            return
        keep = set()
        for m in gone:
            snapshot = m.get("readForSharingSnapshot")
            if (m.get("readForSharing") and isinstance(snapshot, dict)
                    and snapshot.get("id") == m["id"] and snapshot.get("role") == "user"):
                # 以前正常读完的内容仍在同步记录中；这一轮未读完不能凭 seen 获得这个资格。
                m["recalled"] = True
                m["seen"] = True
                m.pop("undelivered", None)
                keep.add(m["id"])
        removed = {m["id"] for m in gone} - keep
        self.history[:] = [m for m in self.history if m["id"] not in removed]
        self.rev += 1
        self.store.save_messages()
        action = "已读的保留同步记录，没读完的拿掉" if keep else "她还没回，拿掉"
        self._note("撤回", f"他撤回了「{gone[0]['text'][:20] or '［图片］'}」{f'（{len(gone)} 条）' if len(gone) > 1 else ''}，{action}")
        # 她正在回的就是这几条：这一轮直接打断（正在问模型的也掐掉），不重来、这一轮的不再补
        if (any(m["id"] in self._offline_notice_ids for m in gone)
                and self._offline_notice_request is not None and not self._offline_notice_request.done()):
            self._offline_notice_request.cancel()
        if self.in_flight and any(m["id"] in self.round_ids for m in gone):
            self.recall_cut = True
            if self.req_task and not self.req_task.done():
                self.restarted = True
                self.req_task.cancel()

    def schedule_turn(self) -> None:
        """排一轮回复。窗口内继续发消息会顺延，直到他停下来（最多等 15 秒）"""
        if self.closed or not state_at(now_ms()).online:
            return
        leaving = self._leaving()
        if leaving and (leaving.get("used") or not state_at(now_ms()).online):
            return
        if self.in_flight:
            self.more_came = True
            return
        now = now_ms()
        if self.batch_task is None:
            self.batch_start = now
        else:
            self.batch_task.cancel()
        window = max(seen_delay(True), MIN_BATCH_MS)
        remaining = MAX_BATCH_MS - (now - self.batch_start)
        wait = max(300, min(window, remaining))
        self.batch_task = asyncio.get_running_loop().create_task(self._fire(wait))

    async def _fire(self, wait_ms: int) -> None:
        try:
            await asyncio.sleep(wait_ms / 1000)
        except asyncio.CancelledError:
            return
        # ⚠️ 直接在这个任务里跑：中间没有让出，别处不会趁机再排一轮
        self.batch_task = None
        self._track(asyncio.current_task())
        await self.run_turn()

    @asynccontextmanager
    async def _speaking(self):
        self.in_flight = True
        self._recent_input = {}
        self._recent_complete = False
        self._recent_sent = False
        self._recent_guard = None
        try:
            yield
        except BaseException:
            self._recent_complete = False  # 整个任务被取消／抛错：尚未送出的阅读不发布。
            raise
        finally:
            try:
                if self._recent_sent or (self._recent_complete and not self.recall_cut
                                         and (self._recent_guard is None or self._recent_guard())):
                    self._publish_recent_read()
            except Exception as exc:
                self._note("记忆", f"近期阅读标记暂未写盘：{type(exc).__name__}", "warn")
            self._recent_input = {}
            self._recent_complete = False
            self._recent_sent = False
            self._recent_guard = None
            self.in_flight = False
            self._farewell_edge = None
            self.restarted = False
            self._asked = None
            self.recall_cut = False
            self.round_ids = set()
            if not self.closed:
                if self.more_came:
                    self.more_came = False
                    # 只为她还没看到的再排一轮（准备时来的已经并进这轮看过了：不想回、没问成，都不再多排一轮）
                    if self._pending() or (self._leaving() and any(m.get("farewellFollowup")
                                                                 and not m.get("farewellTried") for m in self.history)):
                        self.schedule_turn()
                if self.catchup_again:
                    self.catchup_again = False
                    self._spawn(self._catchup_now())
                self._start_offline_notice()

    def _busy(self) -> bool:
        return self.in_flight or self.batch_task is not None

    def _last_is_user(self) -> bool:
        last = next((m for m in reversed(self.history) if not m.get("system")), None)
        return bool(last and last["role"] == "user" and not last.get("undelivered"))

    def _mark_seen(self) -> list[str]:
        """要发请求了：这次交给模型的（和 build_turns 一样的那几条）她都看到了（seen，记在每条消息上：撤回了别的也不影响）。
        这几条记下来（_asked）：她第一条真发出去时，就算回过了（say 里记）。返回这次新记上「看过」的"""
        working = self.memory.working_set(self.history, private=True, keep_ids=self.round_ids)
        self._capture_recent_read(working)
        asked = [m["id"] for m in working if m["role"] == "user" and not m.get("system")]
        # 「看过」记在记录里所有还没看过的上（太早、这次带不进请求的也算：她翻过去了。不然整理记忆关着、积压超过 50 条时，
        #  最早那几条永远「没看到」，心跳会一直拿它们来回）；「回过」只算真交给模型的那几条（_asked）
        new = []
        for m in self.history:
            if m["role"] == "user" and not m.get("system") and not m.get("seen"):
                m["seen"] = True
                new.append(m["id"])
        self._asked = asked
        self.prep_since = 0  # 真问了：这一批不再算「一直在重来」
        self.store.save_messages()
        return new

    @staticmethod
    def _recent_message(m: dict) -> dict:
        """只留实际交谈内容，不能把图片路径／URL／缓存或旧共享快照嵌套进去。"""
        row = {key: copy.deepcopy(m[key]) for key in ("id", "role", "text", "ts", "uid", "name", "sticker") if key in m}
        quote = normalize_quote(m.get("quote"))
        if quote:
            row["quote"] = quote
        attachment = m.get("attachment")
        if isinstance(attachment, dict):
            row["attachment"] = {key: copy.deepcopy(attachment[key]) for key in ("kind", "name", "caption") if key in attachment}
        return row

    def _capture_recent_read(self, messages) -> None:
        self._recent_input = {m["id"]: self._recent_message(m) for m in messages
                              if not m.get("system") and m.get("role") in ("user", "her")}

    def _publish_recent_read(self) -> None:
        """正常读完或已真实送出：只发布本轮入模快照；失败不会擦掉以前有效的阅读。"""
        changed = False
        for m in self.history:
            snapshot = self._recent_input.get(m["id"])
            if (snapshot is None or m.get("system") or m.get("skipped")
                    or (m.get("recalled") and not m.get("answered"))):
                continue
            if not m.get("readForSharing") or m.get("readForSharingSnapshot") != snapshot:
                m["readForSharing"] = True
                m["readForSharingSnapshot"] = copy.deepcopy(snapshot)
                changed = True
        if changed:
            try:
                self.store.save_messages()
            except OSError as exc:
                self._note("记忆", f"近期阅读标记暂未写盘：{type(exc).__name__}", "warn")

    def recent_snapshot(self) -> dict:
        """共享视图只读：未整理且确实读过的本地内容，删除／整理消费立刻生效。"""
        rows = []
        uid = str((self.peer or {}).get("qq") or (self.scope[2:] if self.scope.startswith("p:") else "")
                  or self.options().get("trailblazer") or "")
        name = str((self.peer or {}).get("name") or winext.TRAILBLAZER_NAME)
        # 补发已经读过／部分送出时仍可能保留 undelivered 等重试。只在视图里除去它，
        # 用整理游标划边界；真正读过必须由下方独立的输入快照证明，绝不修改源留言状态。
        view = [{key: value for key, value in m.items() if key != "undelivered"} for m in self.history]
        for m in self.memory.unconsolidated(view):
            snapshot = m.get("readForSharingSnapshot")
            if (not m.get("readForSharing") or not isinstance(snapshot, dict) or m.get("system")
                    or m.get("skipped")):
                continue
            row = copy.deepcopy(snapshot)
            if row.get("id") != m.get("id") or row.get("role") not in ("user", "her"):
                continue
            for flag in ("answered", "recalled", "declined"):
                if m.get(flag):
                    row[flag] = True
            if m.get("declined") and m.get("noReplyKind") in ("unnecessary", "unwilling"):
                row["noReplyKind"] = m["noReplyKind"]
            if row["role"] == "user":
                row["uid"] = row.get("uid") or uid
                row["name"] = row.get("name") or name
            rows.append(row)
        return {"rows": rows, "packs": []}

    def _recent_note(self, query: str) -> str:
        callback = self.recent_context
        if not callable(callback):
            return ""
        uid = str((self.peer or {}).get("qq") or (self.scope[2:] if self.scope.startswith("p:") else "")
                  or self.options().get("trailblazer") or "")
        try:
            note = callback(query, speaker_uid=uid)
            return note if isinstance(note, str) else ""
        except Exception as exc:  # 共享背景不可用不应挡住眼前的回复。
            self._note("记忆", f"近期共享背景暂不可用：{type(exc).__name__}", "warn")
            return ""

    def _unsee(self, ids: list[str]) -> None:
        """这次请求被掐掉了（他又说了、撤回了）：她其实没看到，刚记上的「看过」拿掉，下一轮照样回"""
        gone = set(ids)
        for m in self.history:
            if m["id"] in gone:
                m.pop("seen", None)
        self.store.save_messages()

    def _mark_answered(self) -> None:
        """她第一条真发出去了：这次交给模型的那几条都算回过了（answered）——之后撤回不再拿掉"""
        asked, self._asked = set(self._asked or ()), None
        for m in self.history:
            if m["id"] in asked:
                m["answered"] = True
        self.store.save_messages()
        opening = self.store.extra.get("proactiveOpening")
        if isinstance(opening, dict) and asked.intersection(opening.get("replies", [])):
            self.store.extra.pop("proactiveOpening", None)
            self.store.save_state()

    def _pending(self) -> list[dict]:
        """（这边新加的）她还没看到的：最近一次请求之后他才说的（她一条条发回复的时候他又说了）"""
        return [m for m in self.history
                if m["role"] == "user" and not m.get("system") and not m.get("undelivered") and not m.get("seen")]

    # ============================================================
    #  拼给模型的东西
    # ============================================================

    def build_turns(self, history: list[dict], *, background: bool = False) -> list[dict]:
        """只带尚未整理的原话；日期只进请求。本轮目标不会因并行整理丢失。"""
        see_images = self.llm.supports("image")
        out = []
        working = self.memory.working_set(history, private=True, keep_ids=self.round_ids)
        visible = [m for m in working if not m.get("system")]
        for m in working:
            if m.get("system"):
                continue
            role = "user" if m["role"] == "user" else "assistant"
            stamp = message_time(m.get("ts")) + " " + render_quote(m, visible)
            if m.get("sticker"):
                out.append({"role": role, "content": f"{stamp}［发了一个表情：{m['sticker']}］{m['text']}"})
                continue
            a = m.get("attachment")
            if not a:
                out.append({"role": role, "content": stamp + m["text"]})
                continue
            path = os.path.join(self.store.images, a["file"]) if a.get("file") else None
            if see_images and path and os.path.isfile(path):
                blocks = [{"type": "text", "text": stamp + m["text"] if m["text"] else stamp.rstrip()},
                          image_block(a.get("mediaType", "image/jpeg"), b64_of(path))]
                out.append({"role": role, "content": blocks})
            else:
                out.append({"role": role, "content": f"{stamp}{image_note(a)}{m['text']}"})  # 看不了图：有转述就带上
        if out and out[0]["role"] == "assistant":
            # 已整理边界可能落在她回复前；用明确的前文标记保住未整理的开头，
            # 避免 normalize_turns 因必须 user 起头而丢掉这些真实回复。
            who = self.peer["name"] if self.peer else winext.TRAILBLAZER_NAME
            out.insert(0, {"role": "user", "content":
                f"【未整理的私聊前文】下面从你之前的回复接起；这条前文标记不是{who}新发的消息。"})
        elif background and not out:
            # 原话已全部整理（或只剩她的回复）时，承接本轮后台动作，不能让标准化丢成空请求。
            # 这是明确的程序上下文，不冒充对方来话，不写入历史，也不是失败后的降级重试。
            who = self.peer["name"] if self.peer else winext.TRAILBLAZER_NAME
            out.insert(0, {"role": "user", "content":
                f"【本轮后台情形】按上面的安排与记忆决定是否开口。这是程序提供的背景说明，不是{who}新发的消息。"})
        return out

    def _unanswered(self) -> str:
        """这一批他还没被回的消息（从最后往前，到她上一条为止）。
        她发回复的时候他又说了（最后一条是她的）：就是那几条"""
        pend = self._pending()
        if pend and not self._last_is_user():
            return " ".join(m["text"] for m in pend)
        out = []
        for x in reversed(self.history):
            if x.get("system"):
                continue
            if x["role"] != "user":
                break
            out.insert(0, x["text"])
        return " ".join(out)

    async def _see_images(self, msgs: list[dict], deadline: int | None = None, *, force_text: bool = False) -> None:
        """（这边新加的）她的模型看不了图：要看的图先转述好，等它们转完（caption.py；deadline 是这一轮等到什么时候为止）"""
        if self.captions:
            # 旧宿主/测试回调仍可只接受三个参数；不能吞掉回调内部真正的 TypeError。
            kwargs = {}
            if force_text:
                try:
                    parameters = inspect.signature(self.captions).parameters
                    if "force_text" in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
                        kwargs["force_text"] = True
                except (TypeError, ValueError):
                    pass
            await self.captions(msgs, self.store.save_messages, deadline, **kwargs)

    def _lie_down(self) -> None:
        """她下线了：只把尚未处理的新话留到上线，不重新打开已看过的失败／拒答旧轮。"""
        for m in self._pending():
            m["undelivered"] = True
        self.store.save_messages()
        self._note("收到", "她刚下线，这几条等她上线再看")

    def _drop_round(self, catchup: bool = False) -> None:
        """（用户 2026-10-30 定）他撤回了她正要回的：这一轮打断，不重来、不补——
        这一轮的那几条（还在的）算她看过了，也不再是留言（不管是补发，还是这阵子她下线被留成的），不会再自动回；之后才来的照样回"""
        for m in self.history:
            if m["id"] in self.round_ids:
                m["seen"] = True
                m.pop("undelivered", None)
        self.store.save_messages()
        self._note("撤回", "他撤回了她正要回的话，这一轮打断，不回了")

    def _prep_over(self) -> bool:
        """这一批已经准备（重来）了多久：第一次开始准备时记下，真问了模型清零；超过 PREP_BUDGET_MS 就不再为新变化重来"""
        now = now_ms()
        if not self.prep_since:
            self.prep_since = now
        return now - self.prep_since >= PREP_BUDGET_MS

    async def _ready_to_ask(self, deadline: int) -> bool:
        """（这边新加的）开口前：等这阵子的图转述完（又来了新的也等，同一个截止时间）；
        等完了再看一眼——她下线了 → 躺着等补发；他的话都撤回了 → 不说。返回能不能接着问模型"""
        while True:
            rev = self.rev
            await self._see_images(self.memory.working_set(self.history, private=True, keep_ids=self.round_ids), deadline)
            if self.rev == rev or now_ms() >= deadline:
                break
        if self.recall_cut:  # 要回的被撤回了：这一轮打断（先于「下线了躺着」——不然同一轮的会被留成留言、上线又补）
            return False
        if not state_at(now_ms()).online:
            self._lie_down()
            return False
        return self._last_is_user() or bool(self._pending())

    def _use_tools(self) -> bool:
        return self.llm.supports("tool_use")

    def _base(self, channel: str = "plain", mood: bool = True, *, energy_note: str | None = None,
              identity_tools: bool | None = None) -> str:
        """基础提示词：所有私聊带她自己的情绪；群友另换身份、带独立的人际认知。"""
        current = mood_instruction(settle(self.store.mood, now_ms())["value"]) if mood else None
        if self.peer:
            return persona.build_system_prompt(current, channel, identity_line=winext.peer_identity(self.peer["name"]),
                                               energy_note=energy_note, identity_tools=identity_tools)
        return persona.build_system_prompt(current, channel, energy_note=energy_note, identity_tools=identity_tools)

    def _return_context(self, minutes: float, activity: str) -> str:
        """主要联系人沿用原版逐字文本；群友补发只替换该段中的对话人身份。"""
        text = persona.build_return_context(minutes, activity)
        return text.replace(profile.primary_name(), self.peer["name"]) if self.peer else text

    async def _prepare_background(self, rev: int) -> bool:
        """后台开口同样等实际工作集里的图；新话／撤回来了便让给下一轮。"""
        await self._see_images(self.memory.working_set(self.history, private=True, keep_ids=self.round_ids), now_ms() + WAIT_MS)
        return not self.closed and self.rev == rev and state_at(now_ms()).online and not self._leaving()

    def _who(self) -> str | None:
        if not self.peer:
            return self.intimacy.trailblazer_note() if self.intimacy else None
        closeness = self.intimacy.cognition(self.peer["qq"], self.peer["name"]) if self.intimacy else ""
        return winext.peer_block(self.peer["name"], self.peer["qq"], closeness, self.store.her_never_replied())

    def _tail(self, now: int, *, allow_schedule: bool = False, schedule_msgs=()) -> list[str]:
        """当前时段常驻；本轮没有回想工具时，只为明确的日程问题补充资料。"""
        working = self.memory.working_set(self.history, private=True, keep_ids=self.round_ids)
        her_texts = [m["text"] for m in working if m["role"] == "her" and not m.get("system") and m["text"]]
        recent_memory, _ = self.memory.recent_private_context(now)
        return [x for x in [recent_memory, winext.time_block(now),
                           activity.current_context(now, state_fn=state_at, allow_query=allow_schedule),
                           activity.messages_query(schedule_msgs, now, state_fn=state_at) if schedule_msgs else None,
                           identity.messages_query(schedule_msgs, private=self.peer is None) if schedule_msgs else None,
                           offline.notice_context(self.store, now), winext.opener_note(her_texts)] if x]

    async def _recall_all(self, about: str, history: list[dict]) -> str:
        """「回想」工具：你们之间的记忆和以前的事一起找"""
        now = now_ms()
        mem = await self.memory.recall(about, history, now)
        past = await self.knowledge.recall(about) if self.knowledge else None
        if not past:
            return mem
        return past if mem == "想不起来。" else f"{mem}\n{past}"

    def _tool_result(self, call: dict, history: list[dict], last: bool = False):
        if call["name"] == "recall":
            identity_result = identity.call_result(call, private=self.peer is None)
            if identity_result is not None:
                return identity_result
            schedule_result = activity.call_result(call, now_ms(), state_fn=state_at)
            if schedule_result is not None:
                return schedule_result
            return self._recall_all(str(call["input"].get("about") or ""), history)
        if call["name"] == "send_sticker":
            return "表情发出去了。"
        if call["name"] == "no_reply":
            return "好。"
        return result_for(call, last)

    def _record_no_reply(self, choice: dict, ids) -> None:
        selected = set(ids)
        for message in self.history:
            if message["id"] in selected and message["role"] == "user" and not message.get("system") and not message.get("answered"):
                message["declined"] = True
                message["noReplyKind"] = choice["kind"]
        if choice["kind"] == "unnecessary":
            self.last_meaningful = False
        self.store.save_messages()
        self.store.save_state()
        label = "无需回复" if choice["kind"] == "unnecessary" else "不想回"
        self._count(label)
        self._note("不回", f"看过了，{label}（{choice['reason'] or '没说为什么'}）")

    def _may_decline(self) -> bool:
        """「不想回」：群友一直有；主要联系人看设置（原版没有）"""
        return bool(self.peer) or bool(self.options().get("decline_trailblazer"))

    # ============================================================
    #  一轮回复（她回他的话）
    # ============================================================

    @measure_async("私聊回复")
    @request_model("private_reply")
    async def run_turn(self) -> None:
        if self.in_flight:
            # 她正在说别的（补发、掂量……）→ 等她说完再回
            self.more_came = True
            return
        if self._leaving():
            await self._farewell_reply()
            return
        if not self._last_is_user() and not self._pending():
            return  # 他的话撤回了，没有要回的了
        async with self._speaking():
            self.round_ids = {m["id"] for m in self._pending()}  # 这一轮要回的：她还没看到的那几条
            now = now_ms()
            her = state_at(now)
            online = her.online
            if not online:
                # ⭐ 等他说完的这几秒里她下线了：这几条躺着，上线后补发（这边新加的；原版收消息时才分流）
                self._lie_down()
                return
            deadline = now + WAIT_MS
            if not await self._ready_to_ask(deadline) or self.recall_cut:
                if self.recall_cut:
                    self._drop_round()
                return
            rev0 = self.rev  # 下面查记忆、挑样例都按这时的记录来
            store = self.store
            history = self.history
            store.mood = settle(store.mood, now)
            mood_v = store.mood["value"]

            # ⭐ 她快下线了就顺便道个别——道别的这轮不留念头
            extra = None
            says_farewell = False
            if not self.farewell_said:
                until = offline_after(now)
                minutes_left = (until - now) / MIN if until else math.inf
                if minutes_left <= FAREWELL_ON_REPLY_MIN:
                    extra = persona.build_farewell_context(minutes_left)
                    self.farewell_said = True
                    says_farewell = True

            # ⭐ 他先开口了，念头不作废——带进这次回复
            carried = store.thought
            carry_note = persona.build_carry_thought_context(carried["topic"]) if carried and carried["topic"] else None

            heat_before = chat_heat(history, self.last_meaningful, mood_v, now)
            energy_before = current_energy(store.energy, now)["value"]
            # 群友：不留念头（追问可以）
            tiers = available_tiers(store.thought_ledger, mood_v, heat_before, energy_before, now) \
                if not says_farewell and online and not self.peer else []
            note, store.energy = take_energy_note(store.energy, now)
            store.save_state()
            use_tools = self._use_tools()
            # ⭐ 多想几步（2026-10-29）：开口前她可以「想一想」、可以接着「回想」——想不想她自己决定，
            #   最多几步跟精力走（有精神 3、有点乏 2、累了 1）
            steps = think_steps(energy_before)
            # 回他的方式：低落不是关系疏远，主要联系人保留全部方式（群友仍不索取）。
            ways = peer_ways() if self.peer else available_ways(mood_v)
            streak = asking_streak(store, now)
            streak_note = persona.build_asking_streak_context(streak) if streak >= ASKING_STREAK else None
            tools = tools_for(True, tiers, think=True, ways=ways) if use_tools else []
            if use_tools and self.stickers and self.stickers.can_send():
                tools.append(self.stickers.tool())
            if use_tools:
                tools.append(winext.no_reply_tool(self._may_decline()))

            # ⭐ 风格库：用他最近 3 条消息当查询，按意思挑
            recent = " ".join(m["text"] for m in [x for x in history if not x.get("system") and x["role"] == "user"][-3:])
            style_note = persona.build_style_context(await self.style.samples_for(recent, 4)) if self.style else None
            voice_note = persona.build_voice_ref_context(await self.style.voice_for(recent, 2)) if self.style else None

            memory_note = self.memory.facts_note()
            first_contact = store.her_never_replied()
            first_note = persona.build_first_contact_context() if first_contact and not self.peer else None
            gap = self.memory.gap_note(history, now) if not first_contact else None

            said = self._unanswered()
            recollection = await self.memory.associate(said, history, now, private=True)
            past_note = await self.knowledge.lookup(said, now) if self.knowledge else None
            people_note = await self.people.lookup(said) if self.people else None

            system = "\n\n".join(x for x in [
                self._base("tools" if tools else "plain", energy_note=note), self._who(),
                style_note, voice_note, memory_note, recollection, past_note, people_note, first_note,
                streak_note, carry_note, gap, self._recent_note(said),
                *self._tail(now, allow_schedule=use_tools, schedule_msgs=self._pending() if not use_tools else ()),
            ] if x)
            full = f"{system}\n\n{extra}" if extra else system
            # 开口前再看一眼（和下面拼请求之间不再让出）：准备的这阵子他又说了、撤回了 → 带着现在的记录重来；她下线了 → 躺着等补发
            # （他一直在说、这一批已经重来了 PREP_BUDGET_MS 还没问成：先按现在的记录回，之后来的下一轮再说）
            if self.recall_cut:  # 准备的时候他把要回的撤回了：这一轮不回了
                if says_farewell:
                    self.farewell_said = False
                self._drop_round()
                return
            if not state_at(now_ms()).online or (self.rev != rev0 and not self._prep_over()):
                if says_farewell:
                    self.farewell_said = False
                if state_at(now_ms()).online:
                    self.more_came = True
                else:
                    self._lie_down()
                return
            turns = self.build_turns(history)
            seen_now = self._mark_seen()
            request_rev = self.rev

            def request_valid():
                return (not self.closed and not self.recall_cut and self.rev == request_rev
                        and state_at(now_ms()).online and not self._leaving())

            if tools:
                coro = self.llm.chat_with_tools(full, turns, tools, lambda c, last: self._tool_result(c, history, last),
                                                ends_turn=ends_turn_or_sticker, reads_result=reads_result, max_reads=steps,
                                                final=lambda c: c["name"] == "no_reply", request_valid=request_valid)
            else:
                coro = self.llm.chat_with_retry(full, turns, request_valid=request_valid)
            self._count("模型调用")
            self.req_task = asyncio.get_running_loop().create_task(coro)
            try:
                result = await self.req_task
            except asyncio.CancelledError:
                if self.req_task.cancelled() and not self._parent_cancelled():
                    if says_farewell:
                        self.farewell_said = False
                    if self.recall_cut:  # 他把要回的撤回了：这一轮打断，不重来
                        self._drop_round()
                        return
                    # 被新消息打断了 → 这次的结果作废，等下一轮带着完整上下文重来（还有没回的才排）
                    self._unsee(seen_now)
                    self.more_came = True
                    self._note("重来", "他又说话了（或撤回了），这轮重来")
                    if says_farewell:
                        self.farewell_said = False
                    return
                raise
            finally:
                self.req_task = None

            if self.closed:
                return
            if self.recall_cut:  # 撤回优先于失败处理，不能把作废的请求再记成一条错误回复。
                if says_farewell:
                    self.farewell_said = False
                self._drop_round()
                return
            if not result.ok:
                if not request_valid():
                    if says_farewell:
                        self.farewell_said = False
                    self._unsee(seen_now)
                    if state_at(now_ms()).online and not self._leaving():
                        self.more_came = True
                    else:
                        self._lie_down()
                    return
                await self._failure(result)
                return
            self._recent_complete = True

            snap = copy.deepcopy({"thought": store.thought,
                                  "ledger": store.thought_ledger, "ways": store.extra.get("ways"), "meaningful": self.last_meaningful})
            intent = intent_from_calls(result.calls)
            if intent.get("thoughts"):  # 想一想的内容只给她自己看：只记日志
                self._note("想一想", "｜".join(intent["thoughts"])[:200])
            sticker = next((c["input"].get("emotion") for c in result.calls if c["name"] == "send_sticker"), None)
            self.log.info(f"[MultiChat·工具调用] {'、'.join(c['name'] for c in result.calls) or '（没调用）'}"
                          + (f" · 念头:{intent['thought']['tier']}「{intent['thought']['topic']}」" if intent.get("thought") else "")
                          + (f" · 聊得{'实在' if intent.get('meaningful') else '随口'}" if use_tools else ""))

            # 带进来的念头她已经看过了——说没说是她的事，用过就清掉
            if carried and store.thought and store.thought["id"] == carried["id"]:
                store.thought = None

            choice = winext.no_reply_choice(result.calls, self._may_decline())
            if choice is not None:
                # 只标实际交给这次模型的消息，不碰请求期间后来到的新消息。
                self._record_no_reply(choice, set(self._asked or []) & self.round_ids)
                return

            parts = to_messages(result.text)
            if parts and self.options().get("split_sentences"):
                parts = split_sentences(parts)
            if not parts and not sticker:
                # 这轮没说出口（比如后面那次请求失败了）：她留下的念头照样算数（原版 2026-10-29「前面的动作照样算数」）
                th = intent.get("thought")
                if th and th["tier"] in tiers and store.thought is None:
                    store.thought = plan_thought(store.thought_ledger, th["topic"], th["tier"], mood_v, now)
                    self._note("念头", f"这轮没说出口，留下了「{th['topic']}」")
                store.save_state()
                return

            # 先算这轮以后的精力，再看要不要追问 / 补一句。同一时间只挂一个念头：追问 → 她自己留的 → 替她留的
            # ⚠️ 精力是私聊群聊共用的：真发出去了才记上（这边新加的；发不出去不用退回，也不会盖掉别的会话刚记的）
            def settle_energy(e, t):
                if self.peer:
                    return apply_round(e, intent.get("feel"), t, PEER_DELTA, PEER_REAL_BONUS if intent.get("meaningful") else 0)
                return apply_round(e, intent.get("feel"), t)
            energy_now = settle_energy(store.energy, now)["value"]
            pending = store.thought is not None and (carried is None or store.thought["id"] != carried["id"])
            can_follow = use_tools and online and not says_farewell and not pending
            curious_t = None
            if can_follow and intent.get("curious"):
                if energy_now < TOO_TIRED_TO_ASK:
                    self.log.info(f"[MultiChat·追问] 「{intent['curious']}」，但她太累了，不追")
                else:
                    curious_t, store.thought_ledger = curious_thought(store.thought_ledger, intent["curious"], now)
                    if curious_t:
                        store.thought = curious_t
                        self._note("追问", f"「{intent['curious']}」，{round((curious_t['dueAt'] - now) / 1000)} 秒后掂量")
            if curious_t:
                pass
            elif intent.get("thought"):
                th = intent["thought"]
                if th["tier"] in tiers:
                    store.thought = plan_thought(store.thought_ledger, th["topic"], th["tier"], mood_v, now)
                    self._note("念头", f"留下了「{th['topic']}」，{round((store.thought['dueAt'] - now) / 1000)} 秒后掂量")
            elif can_follow and not self.peer:
                heat = chat_heat(history, bool(intent.get("meaningful")), mood_v, now)
                chance = plant_chance(heat) * energy_factor(energy_now)
                if "soon" in tiers and random.random() < chance:
                    store.thought = plant_thought(heat, now)
                    self._note("念头", f"意犹未尽，{round((store.thought['dueAt'] - now) / 1000)} 秒后掂量")
            self.last_meaningful = bool(intent.get("meaningful"))
            note_way(store, intent.get("way"), now)  # 她这轮怎么回的，记下来（算「连着几轮在问他」用）
            store.save_state()

            slang = [w for p in parts for w in detect_modern_slang(p)]
            if slang:
                self.log.warning(f"[MultiChat·禁则] 命中网络用语：{slang}")
            def undo() -> None:
                # 一条都没发出去：只退本会话暂存的效果；共享的心情、精力都尚未提交。
                store.thought = snap["thought"]
                store.thought_ledger, self.last_meaningful = snap["ledger"], snap["meaningful"]
                if snap["ways"] is None:
                    store.extra.pop("ways", None)
                else:
                    store.extra["ways"] = snap["ways"]
                store.save_state()

            mark = len(self.history)
            if says_farewell:
                self._farewell_edge = until
            try:
                sent = await self.say(parts, sticker=sticker)
            except asyncio.CancelledError:
                # 发到一半整个停了（卸载插件）：一条都没发出去就退回；发出去了就照样记上
                if any(m["role"] == "her" for m in self.history[mark:]):
                    store.mood = on_emotion(store.mood, intent.get("emotion"), bool(intent.get("meaningful")), now_ms())
                    store.energy = settle_energy(store.energy, now_ms())
                    store.save_state()
                else:
                    undo()
                raise
            if not sent:
                undo()
                if self.recall_cut:  # 发第一条之前他把要回的撤回了
                    self._drop_round()
                else:
                    self._note("出错", "消息一条都没发出去，这轮不算（平台发送失败）", "warn")
                return
            store.mood = on_emotion(store.mood, intent.get("emotion"), bool(intent.get("meaningful")), now_ms())
            store.energy = settle_energy(store.energy, now_ms())
            store.save_state()
            if self.peer and self.intimacy:
                self.intimacy.touch(self.peer["qq"], self.peer["name"])
            self._count("回话")
            self._note("回话", " / ".join(parts)[:60] + (f"［表情：{sticker}］" if sticker else ""))

    async def say(self, parts: list[str], lead: bool = False, sticker: str | None = None, guard=None) -> int:
        """按节拍发：输入中 → 消息 → 停 → 输入中 → 消息。返回发出去了几条。"""
        if self._leaving() and self._farewell_edge is None and not self._farewell_grace:
            return 0
        sent = 0
        for kind, val in pace(parts):
            if kind in ("typing", "false-start"):
                await self.sender.typing()
                await asyncio.sleep(val / 1000)
            elif kind == "wait":
                await asyncio.sleep(val / 1000)
            else:
                if sent == 0 and (self.recall_cut or (guard is not None and not guard())):
                    return 0  # 打着字的时候他把要回的撤回了：一条都还没发，这一轮就不发了
                if not await self.sender.send(val):
                    self._note("出错", "消息没发出去，这一轮剩下的不发了", "warn")
                    return sent
                if self._farewell_grace:
                    try:
                        farewell.consume(self.store, now_ms(), PEER_DELTA if self.peer else None)
                    except OSError:
                        self._note("出错", "收尾已送出，状态暂未写盘；当前会话仍保持封口", "warn")
                m = self.store.new_msg("her", val)
                if lead and sent == 0:
                    m["lead"] = True
                m["readForSharing"] = True
                m["readForSharingSnapshot"] = self._recent_message(m)
                self.history.append(m)
                self._recent_sent = True
                self.store.save_messages()
                self._commit_proactive_opening(m)
                self._commit_farewell()
                sent += 1
                if self._asked is not None:
                    self._mark_answered()  # 第一条发出去了：这次交给模型的都算回过
                if sent == 1:
                    self._publish_recent_read()
        if sticker and self.stickers:
            path = self.stickers.pick(sticker)
            if path:
                await asyncio.sleep(0.6 + random.random() * 0.8)
                if sent == 0 and (self.recall_cut or (guard is not None and not guard())):
                    return 0
                if await self.sender.send_image(path):
                    m = {**self.store.new_msg("her", ""), "sticker": sticker, "readForSharing": True}
                    m["readForSharingSnapshot"] = self._recent_message(m)
                    self.history.append(m)
                    self._recent_sent = True
                    self.store.save_messages()
                    self._commit_proactive_opening(m)
                    self._commit_farewell()
                    self._count("表情")
                    sent += 1
                    if self._asked is not None:
                        self._mark_answered()
                    if sent == 1:
                        self._publish_recent_read()
        return sent

    async def _failure(self, r) -> None:
        # ⚠️ 空回复 / 模型拒绝 → 让她沉默。绝不显示错误。
        if r.kind == "empty":
            return
        self._count("模型失败")
        self._note("出错", error_diagnostic(r), "warn")
        if r.kind == "no-key":
            self.log.warning("[MultiChat] 此会话没有可用模型，请检查宿主的 provider_for 配置")
        self.history.append(self.store.new_msg("her", r.narrative, system=True))
        self.store.save_messages()
        if self.failure_notice and r.narrative and not self._leaving():
            await self.sender.send(f"（{r.narrative}）")

    # ============================================================
    #  后台心跳（每 30 秒）
    # ============================================================

    async def heartbeat(self) -> None:
        if self._beat_busy or self.closed:
            return
        self._beat_busy = True
        try:
            now = now_ms()
            her = state_at(now)
            store = self.store
            store.mood = settle(store.mood, now)
            if not her.online:
                self.farewell_said = False
            t = store.thought
            if t and (not is_alive(t, now) or (not her.online and t["tier"] != "later")):
                store.thought = None
            store.save_state()

            if her.online and not self._busy() and not self._leaving():
                if any(m.get("undelivered") for m in self.history):
                    await self._catchup_now()
                elif self._pending():
                    self.schedule_turn()  # 她发回复的时候他又说了、还没来得及回就重启了（这边新加的）
                elif not await self._maybe_farewell(now):
                    if not await self._maybe_recheck(now) and not self.peer:
                        await self._maybe_reach_out(now)
            if not self.mem_busy:
                self._spawn(self.run_memory())
        finally:
            self._beat_busy = False

    # ---------- 补发：她上线看到留言 ----------

    async def _catchup_now(self) -> None:
        if (not state_at(now_ms()).online or self._leaving() or self.in_flight
                or now_ms() - self.catchup_failed_at < CATCHUP_RETRY_MS):
            return
        self.catching_up = True
        try:
            await self._catchup()
        finally:
            self.catching_up = False

    @measure_async("私聊回复")
    @request_model("catchup")
    async def _catchup(self) -> None:
        if self._leaving():
            return
        async with self._speaking():
            history = self.history
            backlog = [m for m in history if m.get("undelivered")]
            if not backlog:
                return
            self.round_ids = {m["id"] for m in backlog}  # 这一轮要补的留言
            rev0 = self.rev  # 从这里起他又说了，这次补发就作废重来（等图的时候来的新图也一起等）
            deadline = now_ms() + WAIT_MS
            await self._see_images(self.memory.working_set(history, private=True, keep_ids=self.round_ids), deadline)
            now = now_ms()
            # 留言 = 她不在的时候他发的；她在线以后才来、并进这次补发的（arrivedOnline）不算，不用「你刚上线」那段说
            away = [m for m in backlog if not m.get("arrivedOnline")]
            since = min(m["ts"] for m in (away or backlog))
            came = online_since(now)
            replied_at = max((came if came is not None else now) + 30_000, since + 30_000)
            away_minutes = (min(replied_at, now) - since) / MIN
            midpoint = int((since + min(replied_at, now)) / 2)
            carried = self.store.thought
            note = "\n\n".join(x for x in [
                self._return_context(away_minutes, "离线期间其中一段的活动（不代表全程，也不是当前状态）：" +
                                     describe_activity(state_at(midpoint))) if away else None,
                winext.CATCHUP_NEW_ONLINE if away and len(away) < len(backlog) else None,
                persona.build_carry_thought_context(carried["topic"]) if carried and carried["topic"] else None,
                persona.build_first_contact_context() if self.store.her_never_replied() and not self.peer else None,
            ] if x)
            said = " ".join(m["text"] for m in backlog)
            recollection = await self.memory.associate(said, history, now, private=True)
            past_note = await self.knowledge.lookup(said, now) if self.knowledge else None
            reply_tools = [winext.no_reply_tool(self._may_decline())] if self._use_tools() else None
            no_reply_note = ("【这轮是否开口】\n上面的补发开场和说话安排只在你决定回复时适用。"
                             "这批留言如果已经无需回应，可以用 no_reply 自然停下，不必为了交代刚上线而另发一句。") if reply_tools else None
            system = "\n\n".join(x for x in [self._base("tools" if reply_tools else "plain", identity_tools=False), self._who(),
                                             self.memory.facts_note(), recollection, self._recent_note(said),
                                             past_note, *self._tail(now, schedule_msgs=backlog), note, no_reply_note] if x)
            # 准备的这阵子（查记忆）：他又说了、撤回了 → 这次作废，马上带着现在的留言重来；她下线了就留着，上线再补（这边新加的）
            # （一直在变、重来超过 PREP_BUDGET_MS：先按现在的留言补）
            if self.recall_cut:  # 他撤回了要补的留言：这次补发打断，这几条不再补
                self._drop_round(catchup=True)
                self.catchup_again = any(m.get("undelivered") for m in history)
                return
            if self.rev != rev0 and not self._prep_over():
                self.catchup_again = any(m.get("undelivered") for m in history)
                return
            backlog = [m for m in history if m.get("undelivered")]
            if not backlog:
                return
            if not state_at(now_ms()).online:
                return
            self._count("模型调用")
            turns = self.build_turns(history)
            seen_now = self._mark_seen()
            request_rev = self.rev

            def request_valid():
                return (not self.closed and not self.recall_cut and self.rev == request_rev
                        and state_at(now_ms()).online and not self._leaving()
                        and any(m.get("undelivered") for m in self.history))

            # 补发也能自然不回复；这里只提供不回复工具，不额外启用回想/多步循环。
            self.req_task = asyncio.get_running_loop().create_task(self.llm.chat_with_retry(
                system, turns, tools=reply_tools, request_valid=request_valid))
            try:
                result = await self.req_task
            except asyncio.CancelledError:
                if self.req_task.cancelled() and not self._parent_cancelled():
                    if self.recall_cut:  # 他撤回了要补的留言：打断，这几条不再补
                        self._drop_round(catchup=True)
                        self.catchup_again = any(m.get("undelivered") for m in history)
                        return
                    # 他又说话了：这次作废，带着现在的留言马上重来
                    self._note("补发", "他又说话了，这次补发重来")
                    self._unsee(seen_now)
                    self.catchup_again = any(m.get("undelivered") for m in history)
                    return
                raise
            finally:
                self.req_task = None
            if self.closed:
                return
            if self.recall_cut:
                self._drop_round(catchup=True)
                self.catchup_again = any(m.get("undelivered") for m in history)
                return
            if not result.ok and not request_valid():
                self.catchup_again = (state_at(now_ms()).online and not self._leaving()
                                      and any(m.get("undelivered") for m in history))
                return
            if not result.ok and result.kind != "empty":
                # ⚠️ 失败了不清掉「没看」的标记，过一阵再试
                self.catchup_failed_at = now_ms()
                self._count("模型失败")
                self._note("补发", f"失败：{error_diagnostic(result)}", "warn")
                return
            self._recent_complete = bool(result.ok)
            ids = {m["id"] for m in backlog}

            def delivered() -> None:
                for m in history:
                    if m["id"] in ids:
                        m.pop("undelivered", None)
                self.store.save_messages()

            choice = winext.no_reply_choice(result.calls, self._may_decline()) if result.ok else None
            if choice is not None:
                delivered()  # 明确处理完这批留言，后续心跳不再次补回。
                if carried and self.store.thought and self.store.thought["id"] == carried["id"]:
                    self.store.thought = None
                self._record_no_reply(choice, ids & set(self._asked or []))
                return

            parts = to_messages(result.text) if result.ok else []
            if parts and self.options().get("split_sentences"):
                parts = split_sentences(parts)
            if not parts:
                delivered()  # 她看了，没说话
                return
            self._note("补发", f"她上线看到了 {len(backlog)} 条留言")
            self.catchup_saying = True
            try:
                said_ok = await self.say(parts)
            finally:
                self.catchup_saying = False
            if not said_ok and self.recall_cut:  # 发第一条之前他把要补的留言撤回了：打断，这几条不再补
                self._drop_round(catchup=True)
                self.catchup_again = any(m.get("undelivered") for m in history)
                return
            if not said_ok:
                # 账号 那边一条都没发出去：「没看」的标记也留着，过一阵再试（这边新加的：原版发送不会失败）
                self.catchup_failed_at = now_ms()
                self._note("补发", "消息没发出去，留言留着，5 分钟后再试", "warn")
                return
            delivered()
            if carried and self.store.thought and self.store.thought["id"] == carried["id"]:
                self.store.thought = None
                self.store.save_state()

    def _commit_proactive_opening(self, message: dict) -> None:
        if self._proactive_sending is None:
            return
        before, self._proactive_sending = self._proactive_sending, None
        replies = [m["id"] for m in self.history if m["role"] == "user" and m["id"] not in before and not m.get("recalled")]
        self.store.extra["proactiveOpening"] = {
            "segment": online_since(now_ms() // MIN * MIN), "message": message["id"], "replies": replies,
        }
        self.store.save_state()

    def _awaiting_proactive_response(self, now: int) -> bool:
        opening = self.store.extra.get("proactiveOpening")
        if not isinstance(opening, dict):
            return False
        if not state_at(now).online or opening.get("segment") != online_since(now // MIN * MIN):
            self.store.extra.pop("proactiveOpening", None)
            self.store.save_state()
            return False
        replies = set(opening.get("replies", []))
        return not any(m["id"] in replies and m["role"] == "user" and not m.get("recalled") for m in self.history)

    # ---------- 道别 ----------

    @measure_async("私聊回复")
    @request_model("farewell")
    async def _maybe_farewell(self, now: int) -> bool:
        if self._leaving() or self.farewell_said or self._awaiting_proactive_response(now):
            return False
        until = offline_after(now)
        if not until:
            return False
        minutes_left = (until - now) / MIN
        if not (0 < minutes_left <= FAREWELL_WINDOW_MIN):
            return False
        last = next((m for m in reversed(self.history) if not m.get("system")), None)
        if not last or (now - last["ts"]) / MIN > RECENT_ACTIVITY_MIN:
            return False
        self.farewell_said = True
        async with self._speaking():
            rev0 = self.rev
            if not await self._prepare_background(rev0):
                self.farewell_said = False
                return True
            system = "\n\n".join(x for x in [self._base(), self._who(), self.memory.facts_note(), self._recent_note(last["text"]), *self._tail(now),
                                             persona.build_standalone_farewell_context(minutes_left)] if x)
            self._count("模型调用")
            turns = self.build_turns(self.history, background=True)
            self._mark_seen()

            def still_current():
                return not self.closed and self.rev == rev0 and state_at(now_ms()).online and not self._leaving()

            r = await self.llm.chat_with_retry(
                system, turns, request_valid=still_current)
            if not still_current():
                self.farewell_said = False  # 让新来的那轮顺带道别；不发送已过时的独立道别。
                return True
            if not r.ok:
                self._count("模型失败")
                self._note("道别", f"失败：{error_diagnostic(r)}", "warn")
                return True
            self._recent_complete = True
            self._recent_guard = still_current
            parts = to_messages(r.text)
            if parts:
                self._note("道别", "她要下线了，说一声")
                self._farewell_edge = until
                sent = await self.say(parts, lead=True, guard=still_current)
                if not sent and not still_current():
                    self.farewell_said = False
        return True

    # ---------- 到点掂量 ----------

    @measure_async("私聊回复")
    @request_model("thought")
    async def _maybe_recheck(self, now: int) -> bool:
        if self._leaving():
            return False
        t = self.store.thought
        if not t or not self._use_tools():
            return False
        if any(m.get("undelivered") for m in self.history) or user_spoke_since(t, self.history):
            return False
        since = online_since(now)
        if now < due_time(t, since):
            return False
        async with self._speaking():
            rev0 = self.rev
            if not await self._prepare_background(rev0):
                return True
            her = state_at(now)
            until = offline_after(now)
            note = persona.build_recheck_context(
                topic=t["topic"], planted=bool(t.get("planted")), curious=bool(t.get("curious")),
                ago=ago_text(now - t["createdAt"]), activity=describe_activity(her),
                minutes_left=(until - now) / MIN if until else None,
            )
            system = "\n\n".join(x for x in [self._base("tools", identity_tools=False), self._who(), self.memory.facts_note(), self._recent_note(t["topic"]),
                                             *self._tail(now), note] if x)
            tools = tools_for(False, [], recheck_can_wait=not t["waited"])
            self._count("模型调用")
            turns = self.build_turns(self.history, background=True)
            self._mark_seen()

            def still_current():
                return (not self.closed and self.rev == rev0 and state_at(now_ms()).online
                        and not self._leaving() and not user_spoke_since(t, self.history))

            def still_waiting():
                cur = self.store.thought
                return still_current() and bool(cur and cur["id"] == t["id"])

            r = await self.llm.chat_with_tools(
                system, turns, tools, result_for, ends_turn=ends_turn,
                request_valid=still_waiting)
            # 她掂量的时候又有变化：念头留给当前会话处理，不发送过时的回复，也不当作她主动放下。
            if not still_waiting():
                return True
            if not r.ok:
                self._count("模型失败")
                self._note("念头", f"掂量失败：{error_diagnostic(r)}；本次念头结束，不自动重试", "warn")
                self.store.thought = None
                self.store.save_state()
                return True
            self._recent_complete = True
            self._recent_guard = still_current
            outcome = "drop"
            texts: list[str] = []
            intent = intent_from_calls(r.calls)
            if intent.get("letGo"):
                outcome = "drop"
            elif intent.get("wait") and not t["waited"]:
                outcome = "wait"
            else:
                texts = to_messages(r.text)
                outcome = "say" if texts else "drop"
            if outcome == "wait":
                self.store.thought = postpone(t, now_ms())
                self._note("念头", "她想了想，再等等")
                self.store.save_state()
                return True
            self.store.thought = None
            if outcome == "drop":
                self._note("念头", "她想了想，算了")
                self.store.save_state()
                return True
            self.store.save_state()
            mark = len(self.history)
            try:
                await self.say(texts, lead=True, guard=still_current)
            finally:
                if any(m["role"] == "her" and not m.get("system") for m in self.history[mark:]):
                    self.store.thought_ledger = note_delivered(self.store.thought_ledger, t["tier"], now_ms())
                    self.store.save_state()
                    self._note("念头", "她又说了一句")
        return True

    # ---------- 主动找他（只找主要联系人） ----------

    def _today_count(self, now: int) -> dict:
        day = day_of(now)
        c = self.store.proactive_day
        return c if c.get("day") == day else {"day": day, "n": 0}

    def _last_contact_at(self) -> int:
        last = next((m for m in reversed(self.history) if not m.get("system")), None)
        return max(last["ts"] if last else 0, self.store.extra.get("groupTalkAt", 0) or 0)

    def _should_reach_out(self, now: int) -> bool:
        if self._leaving():
            return False
        her = state_at(now)
        if not her.online or self.store.thought is not None:
            return False
        if self.store.her_never_replied():
            return False  # 06：第一次接通之前她不主动找他
        if self._today_count(now)["n"] >= PROACTIVE_MAX_PER_DAY:
            return False
        hour_index = math.floor(now / HOUR)
        if hour_index == self.proactive_tried_hour:
            return False
        last = next((m for m in reversed(self.history) if not m.get("system")), None)
        if not last:
            return False
        if any(m.get("undelivered") for m in self.history):
            return False
        if (now - self._last_contact_at()) / HOUR < MIN_QUIET_HOURS:
            return False
        chance = NIGHT_CHANCE if her.activity == "night" else DAY_CHANCE
        roll = (math.fmod(math.sin(hour_index * 12.9898) * 43758.5453, 1) + 1) % 1
        return roll < chance

    @measure_async("私聊回复")
    @request_model("proactive")
    async def _maybe_reach_out(self, now: int) -> bool:
        if not self._should_reach_out(now):
            return False
        async with self._speaking():
            her = state_at(now)
            self.proactive_tried_hour = math.floor(now / HOUR)
            rev0 = self.rev
            group_at = self.store.extra.get("groupTalkAt", 0)
            if not await self._prepare_background(rev0) or self.store.extra.get("groupTalkAt", 0) != group_at:
                return True
            hours = (now - self._last_contact_at()) / HOUR
            was_neglect = hours >= NEGLECT_HOURS
            note = persona.build_proactive_context(describe_activity(her), her.activity == "night", hours, was_neglect)
            use_tools = self._use_tools()
            system = "\n\n".join(x for x in [self._base("tools" if use_tools else "plain", identity_tools=False), self._who(), self.memory.facts_note(), self._recent_note(""),
                                             *self._tail(now), note] if x)

            def last_user_id():
                return next((m["id"] for m in reversed(self.history) if not m.get("system") and m["role"] == "user"), None)

            before = last_user_id()

            def still_quiet():
                return (not self.closed and self.rev == rev0 and last_user_id() == before
                        and self.store.extra.get("groupTalkAt", 0) == group_at
                        and state_at(now_ms()).online and not self._leaving())

            turns = self.build_turns(self.history, background=True)
            self._mark_seen()
            self._count("模型调用")
            if use_tools:
                r = await self.llm.chat_with_tools(system, turns, [LET_GO], lambda c, last: "好。", ends_turn=ends_turn,
                                                   request_valid=still_quiet)
            else:
                r = await self.llm.chat_with_retry(system, turns, request_valid=still_quiet)
            # 请求失败、或者她写的时候他开口了：不算
            if not r.ok or not still_quiet():
                if not r.ok and still_quiet():
                    self._count("模型失败")
                    self._note("主动", f"失败：{error_diagnostic(r)}", "warn")
                return True
            self._recent_complete = True
            self._recent_guard = still_quiet
            let_go = any(c["name"] == "let_go" for c in r.calls)
            parts = [] if let_go else to_messages(r.text)
            if not let_go and not parts:
                return True
            if let_go:
                c = self._today_count(now)
                self.store.proactive_day = {"day": c["day"], "n": c["n"] + 1}
                self._note("主动", "她想起他，想了想，算了")
                self.store.save_state()
                return True
            mark = len(self.history)
            self._proactive_sending = {m["id"] for m in self.history if m["role"] == "user"}
            try:
                await self.say(parts, lead=True, guard=still_quiet)
            finally:
                self._proactive_sending = None
                if any(m["role"] == "her" and not m.get("system") for m in self.history[mark:]):
                    delivered_at = now_ms()
                    c = self._today_count(delivered_at)
                    self.store.proactive_day = {"day": c["day"], "n": c["n"] + 1}
                    self.store.save_state()
                    self._note("主动", "她想起他了")
        return True

    # ---------- 整理记忆 ----------

    @measure_async("长期记忆")
    async def _mem_chat(self, system, messages, tools=None, tool_choice="auto"):
        self._count("模型调用")
        return await self.llm.chat_with_retry(system, messages, tools, tool_choice)

    async def run_memory(self, force: bool = False) -> str | None:
        """她下线时（一直在线就 3 小时没人说话）把聊的收成记忆；没有就并底噪；再没有就给旧记忆补话题。
        这边新加的：这个会话「整理记忆」关着就什么都不做（变模糊照常）。force = 仪表盘上的「现在整理」"""
        if self.mem_busy:
            return None
        if self.in_flight:
            return "正在回话，请稍后再整理"
        now = now_ms()
        self.memory.fade(now)  # 变模糊不用模型：照常
        if not self.memory.enabled() and not force:
            return None
        online = state_at(now).online and not force
        session = self.memory.pending_session(self.history, online, now) if (force or self.memory.can_try(now)) else None
        haze = self.memory.pending_haze() if not session and (force or self.memory.can_try_haze(now)) else None
        tags = not session and not haze and (force or self.memory.can_try_tags(now)) and bool(self.memory.pending_tags())
        if not session and not haze and not tags:
            return "没有要整理的"
        purpose = "memory" if session else "memory_haze" if haze else "memory_tags"
        with model_scope(purpose):
            if self.llm.provider() is None or (purpose != "memory_haze" and not self._use_tools()):
                return None
            return await self._run_memory_selected(session, haze, now)

    async def _run_memory_selected(self, session, haze, now) -> str | None:
        """选好用途后再校验模型；准备、工具补交与整理全程沿用同一次路由。"""
        self.mem_busy = True
        try:
            if session:
                await self._see_images(session, now_ms() + WAIT_MS, force_text=True)
                if self.closed:
                    return None
                if self.peer:
                    closeness = await self.memory.consolidate_peer(self._mem_chat, session, now, f"和{self.peer['name']}的私聊里",
                                                                   self.options().get("trailblazer", ""),
                                                                   self.intimacy.roster_line if self.intimacy else None,
                                                                   (self.peer["qq"], self.peer["name"]))
                    ok = closeness is not None
                    for c in closeness or []:
                        if self.intimacy:
                            d = self.intimacy.apply(c["qq"], c["delta"], c["reason"], c["name"], c.get("impression", ""),
                                                    observed_at=c.get("observedAt"))
                            self._note("亲密度", f"{c['name']} {d:+d}（{c['reason']}）")
                else:
                    if self.intimacy and self.intimacy.trailblazer:
                        tb = self.intimacy.trailblazer
                        def update_impression(text, observed_at):
                            before = self.intimacy.impression(tb)
                            self.intimacy.apply(tb, 0, "", impression=text, observed_at=observed_at)
                            if self.intimacy.impression(tb) != before:
                                self._note("印象", "整理这段私聊时，更新了对主要联系人的印象")
                        result = await self.memory.consolidate(
                            self._mem_chat, session, now,
                            trailblazer_impression=self.intimacy.impression(tb),
                            on_trailblazer_impression=update_impression)
                    else:
                        result = await self.memory.consolidate(self._mem_chat, session, now)
                    ok = result != "failed"
                self._note("记忆", f"整理了 {len(session)} 条" if ok else "整理失败，10 分钟后再试", "info" if ok else "warn")
            elif haze:
                if self.peer:
                    ok = await self.memory.merge_haze(self._mem_chat, now, identity=winext.peer_identity(self.peer["name"]))
                else:
                    ok = await self.memory.merge_haze(self._mem_chat, now)
            else:
                ok = await self.memory.tag_old(self._mem_chat, now)
            await self.memory.refresh_vectors()
            return "整理好了" if ok else "整理失败，稍后可以再试"
        except Exception as e:  # noqa: BLE001
            self._note("记忆", f"出错：{e!r}", "warn")
            return redact(f"出错：{e!r}")
        finally:
            self.mem_busy = False

    def shutdown(self) -> None:
        """卸载插件：先关上（什么都不再接、不再排、不再记），再停掉正在跑的"""
        self.closed = True
        for t in (self.batch_task, self.req_task, *self._running):
            if t and not t.done():
                t.cancel()
