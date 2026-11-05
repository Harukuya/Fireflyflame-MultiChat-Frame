"""
群聊（这边新加的，原版没有；做法写在「群聊是怎么做的.md」）

一个群一个 GroupChat；GroupHub 管所有群和「看手机」。
⭐ 什么时候说话：
  ① 被叫到（@她、回她的消息、叫她的词）：在线就等一个合并窗口再回；不在线就自动回复一次（每人每段离线一次），记成留言，上线后补看
  ② 看手机：各群独立平滑随机等待（Gamma形状2，基础速率每小时1.5；每块最多2次，另至少隔10分钟），
     「主动看」开着的群各自读最近没看过的 15～30 条，判断模型决定要不要接；各群不争抢同一次机会
  ③ 接着聊：她说完话开一个 3～8 分钟的接话窗口，窗口里的新消息交给判断模型
⭐ 她只记得她看过的：被叫到、看手机、接话窗口里读到的，和她自己说的（read=True 的那些）。
⭐ 判断：接话窗口只看要不要接着说（短提示词、不查背景）；看手机要看她真感不感兴趣（她的完整提示词 + 联想记忆、
  以前的事、提到的人），背景一批只查一次，回话那轮直接用。查到的等她真开口了才记账（想起过、刚聊过哪件）。
  只有表情、图片、几个字的不判断、也不查。
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import re
from contextlib import asynccontextmanager

from . import activity, farewell, offline, persona, winext
from .caption import image_note
from . import look_wait
from .energy import (
    DELTA, PEER_DELTA, PEER_REAL_BONUS, TOO_TIRED_TO_ASK, TIRED, apply_round, current_energy, energy_band, think_steps,
)
from .judge import judge
from .history import History
from .rounds import RoundMemory
from .usage import measure
from .jsutil import HOUR, MIN, message_time, now_ms, strip_ps
from .llm import b64_of, error_diagnostic, image_block
from .memory import open_threads, thread_line
from .mood import mood_instruction, on_emotion, settle
from .pacing import schedule as pace
from .sanitize import to_messages
from .schedule import describe_activity, offline_after, online_since, state_at
from .tools import RECALL, intent_from_calls, reads_result, result_for, round_feeling
from .ways import ASKING_STREAK, GROUP_WAYS, asking_streak, group_way_note, note_way

BATCH_MS = (5000, 15000)
WINDOW_DEBOUNCE_MS = (6000, 10000)
WINDOW_MIN = (3, 8)
LOOK_PER_HOUR = 1.5     # 归一化等待的推进速率；Gamma预算均值1，默认额外等待均值40分钟。
LOOK_BLOCK_MIN = 60     # 分块：每块多少分钟
LOOK_BLOCK_MAX = 2      # 每块最多看几次
LOOK_MIN_GAP = 10 * MIN  # 两次之间至少隔这么久
LOOK_TICK_CAP = 2 * MIN  # 连续可看的单步进度上限；重启／暂停恢复的首步不补算。
READ_FIRST = (15, 30)
TURN_CONTEXT = 30
AROUND = 30         # 她要接的那条更早：前面最多 14 条、连同后文一共 30 条
WAIT_MS = 20_000    # 一轮里等图片转述最多等多久（caption.py 的 WAIT_S）
BUFFER_MAX = 400
FAREWELL_WINDOW_MIN = 10
QUERY_MAX = 12      # 判断前查背景：最多拿最近 12 条去查
QUOTE_AFTER = 5     # 她接的那条后面已经有 5 条以上 → 用 账号 的引用
CATCHUP_RETRY_MS = 5 * MIN  # 补看没成：过这么久再试
MISSION_ACK = "我在忙，等空下来再回你。"  # 任务中回主要联系人那句，模型不可用时用
ENERGY_BAND_NAME = {"fresh": "有精神", "low": "有点乏", "tired": "累了"}
_PLACEHOLDER = re.compile(r"［发了(一张图片|一个表情)］")
_FILLER = re.compile(r"^(哈|h|呵|嘿|嘻|草|6|2|3|w|啊|哦|噢|嗯|额|呃)+$", re.I)


def _gap(text: str) -> dict:
    """上下文里隔开的地方（只在这一次请求里，不进聊天记录）"""
    return {"id": f"gap-{now_ms()}-{random.random()}", "role": "gap", "text": text}


def _seen(m: dict) -> bool:
    """她看过的（细看过的）：她自己说的、读到的；翻过去没细看的不算"""
    return m["role"] == "her" or (m.get("read") and not m.get("skipped"))


def look_chance(dt_ms: float, per_hour: float, tired: bool) -> float:
    """有效等待的 Gamma(2) CDF；实际调度只抽一次预算，不每心跳重抽。"""
    return look_wait.chance(dt_ms, per_hour, tired)


def _trivial(m: dict) -> bool:
    """只挡空消息和明显机械刷屏；简短答复、图片也可能有新意思。"""
    if m.get("attachment"):
        return False
    t = strip_ps(_PLACEHOLDER.sub("", m.get("text") or ""))
    return not t or (len(t) >= 3 and len(set(t.lower())) == 1 and bool(_FILLER.fullmatch(t)))


def _next_online_at(now: int) -> int:
    """这一段不在线到什么时候结束：下次上线的那一分钟（毫秒）——认「这一段离线 / 这一段任务」用。
    从下一个整分钟起一分钟一分钟往后找（最多 48 小时）：不会跳过不到一小时的在线，同一段离线里任何时刻算出来都一样。"""
    t = now // MIN * MIN + MIN
    for _ in range(48 * 60):
        if state_at(t).online:
            return t
        t += MIN
    return -1


class GroupChat:
    def __init__(self, gid: str, hub: "GroupHub", store, memory):
        self.gid = gid
        self.hub = hub
        self.store = store
        self.memory = memory
        # ⭐ 这个群同一时间只有一件事在进行（判断、回话、补看、道别都算）；这期间来的消息按种类记下，做完再排
        self.in_flight = False
        self.more_came = False    # 期间有人叫她
        self.more_window = False  # 期间有普通消息（接话窗口里的）
        self.batch_task: asyncio.Task | None = None
        self.batch_start = 0
        self.batch_passive = False
        self.window_task: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()  # 正在跑的那一轮、整理记忆（卸载插件时一起停）
        self.mem_busy = False
        self.phase = ""  # 仪表盘：前台正在判断、任务中应答、回复，还是发送
        self._pending_ack: tuple[list[str], str] | None = None  # 候选消息 ID、收到时的离线状态段
        self._pending_auto: dict[str, tuple[list[str | None], str]] = {}  # 每人候选目标 ID、离线状态段
        self._tick_task: asyncio.Task | None = None
        self._look_task: asyncio.Task | None = None
        self.last_tick = 0
        self._look_active = False  # 不持久化墙钟：重启／暂停后的首 tick 不补算过去时间。
        self.force_look = False
        self._memory_task: asyncio.Task | None = None
        self._history_task: asyncio.Task | None = None
        self._history_timer: asyncio.TimerHandle | None = None
        self._archive_before: set[str] = set()
        self._archive_saved_before: set[str] = set()
        self._archive_context: list[dict] = []
        self._round_read: dict[str, dict] = {}
        self._round_sent: list[dict] = []
        self._round_remember = True
        self._round_reference = ""
        self._round_speaker = ""
        self._round_images: set[str] = set()
        self._round_selection_key = ""
        self._round_shared_inputs: dict[str, dict] = {}
        self._round_shared_ok: dict[str, dict] = {}
        self._last_local_note = ""
        self.closed = False  # 卸载了：什么都不再接、不再排、不再记
        self.recalls = 0  # 撤回了几次（准备回话的这阵子有没有人撤回，看它）
        self._recalled_ids: set[str] = set()  # 区分被收件缓存裁掉和真的被撤回的活动对象。
        self._asked: list[str] | None = None  # 这一轮交给模型的群友消息：她第一条真发出去时算回过（say 里记）
        self._farewell_edge = None
        self._farewell_grace = False
        self._window_exchange_pending = None  # 首条实际送出才提交是否形成往来。
        self._window_exchange_sending = False
        x = store.extra
        x.setdefault("windowUntil", 0)
        x.setdefault("spoken", False)
        x.setdefault("autoReplied", {})
        x.setdefault("lastLookAt", 0)
        if "look" not in x:
            # 旧版所有群共用配额；升级时各群各继承一次，避免升级/重启刷新本块次数。
            x["look"] = dict(getattr(hub.her, "look", {}) or {})
            store.save_state()
        if x.get("flags", 0) < 2:
            # 记录升级一次（之后都记在每条消息上）：
            # 1）「她回话时看到过」（shown）：看过的都算看到过；2）「回过」（answered）：后面有她的话的算回过
            if x.get("flags", 0) < 1:
                for m in self.msgs:
                    if m["role"] == "user" and m.get("read"):
                        m["shown"] = True
                x.pop("shownUpto", None)
            her_seen = False
            for m in reversed(self.msgs):
                if m["role"] == "her":
                    her_seen = True
                elif her_seen:
                    m["answered"] = True
            x["flags"] = 2
            store.save_messages()  # 升级完马上存：别等下一次回话成功才顺带存（中间失败、重启会再升级一次）
            store.save_state()
        self.history_archive = History(store, source=f"群「{self.name}」")
        self._archive_read()  # 首次接入时迁移旧缓存中确实读过的部分，之后按 ID 幂等更新。
        self.round_memory = RoundMemory(store, self.history_archive, getattr(hub, "embedder", None))
        self._sync_round_segment()

    @property
    def name(self) -> str:
        return self.hub.group_name(self.gid)

    @property
    def scope(self) -> str:
        return f"g:{self.gid}"

    @property
    def msgs(self) -> list[dict]:
        return self.store.messages

    @property
    def llm(self):
        return self.hub.llm_for(self.gid) if getattr(self.hub, "llm_for", None) else self.hub.llm

    @property
    def judge_llm(self):
        return self.hub.judge_for(self.gid) if getattr(self.hub, "judge_for", None) else self.hub.judge_llm

    def _mood_note(self, now: int | None = None) -> str:
        return mood_instruction(settle(self.hub.her.mood, now)["value"])

    def _archive_action(self, action, *args, **kwargs) -> bool:
        try:
            action(*args, **kwargs)
            return True
        except (OSError, ValueError) as error:
            self._note("历史", f"阅读档案暂时无法保存，保留缓存稍后再试（{type(error).__name__}）", "warn")
            return False

    def _archive_read(self) -> bool:
        """档案只收稳定的已读原文。准备中的新 read 先留到本轮结束，避免撤回打断后漏入摘要。"""
        candidates = [*self.msgs, *self._archive_context]
        return self._archive_action(self.history_archive.sync,
                                    [m for m in candidates if not m.get("system") and m.get("role") in ("user", "her")
                                     and _seen(m) and (not self.in_flight or m["role"] == "her" or m["id"] in self._archive_before)])

    def _read_history(self, *, include_skipped: bool = False) -> list[dict]:
        """已读档案加当前缓存；同 ID 以缓存为准，旧原文不会因 400 条收件缓存裁剪而消失。"""
        merged = {m["id"]: {**m, "read": True} for m in self.history_archive.messages}
        for m in [*self._archive_context, *self.msgs]:
            if m.get("skipped") and m.get("id") in merged:
                continue  # 本轮略过不会抹掉以前确实读过的原文。
            if not m.get("system") and m.get("role") in ("user", "her") and (m["role"] == "her" or m.get("read")):
                merged[m["id"]] = m
            elif m.get("id") in merged:
                merged.pop(m["id"])
        raw_order = {m["id"]: i for i, m in enumerate(self.msgs)}
        saved_order = {key: i for i, key in enumerate(merged)}
        return sorted((m for m in merged.values() if include_skipped or _seen(m)),
                      key=lambda m: (m.get("ts", 0), m["id"] in raw_order,
                                     raw_order.get(m["id"], saved_order[m["id"]])))

    def _protect_context(self, msgs: list[dict]) -> None:
        if not self.in_flight:
            return
        known = {m["id"] for m in self._archive_context}
        self._archive_context.extend(m for m in msgs if m["id"] not in known and m["id"] not in self._recalled_ids)

    async def _history_note(self, query: str, ctx: list[dict], selection_key: str = "", speaker_uid: str | None = None) -> str:
        try:
            if self.in_flight:
                self._round_selection_key = self._round_selection_key or selection_key
                selection_key = self._round_selection_key
            speakers = {m.get("uid") for m in ctx if m.get("role") == "user" and m.get("uid")}
            pending = self.round_memory.claimed_ids() - self.round_memory.covered_ids()
            uid = speaker_uid if speaker_uid is not None else next(iter(speakers)) if len(speakers) == 1 else ""
            shared = getattr(self.hub, "recent_context", None)
            if callable(shared):
                # 共享池统一选五包；自己的新记事只能引用本群前文，不能再次归档外来内容。
                local = self.round_memory._note_for(self.round_memory._display_records()[-2:])
                note = shared(self.scope, query, speaker_uid=uid)
            else:
                local = note = await self.round_memory.context_note(query, speaker_uid=uid,
                                                                    selection_key=selection_key)
            self._last_local_note = local
            if self.in_flight and note:
                self._round_reference = local[-1600:]
            if self.in_flight:
                previous = [message_time(m.get('ts')) + f" {m.get('name') or ('我' if m.get('role') == 'her' else '某人')}："
                            + (image_note(m.get('attachment')) + m.get('text', ''))[-160:]
                            for m in ctx if m.get("id") in pending][-5:]
                if previous:
                    self._round_reference = (local[-700:] + "\n理解指代用的前文（不要重新记一遍）：\n" + "\n".join(previous))[-1600:]
            source = self.history_archive.source_note(ctx)
            return "\n\n".join(x for x in (note, source if source and source not in note else "") if x)
        except (OSError, ValueError) as error:
            self._note("历史", f"暂时取不到旧提要，继续看这次原话（{type(error).__name__}）", "warn")
            return ""

    def _request_context(self, ctx: list[dict], fresh: set[str], reopen: set[str] | None = None) -> list[dict]:
        """本轮新读的消息，加当前片段/失败待写的有限前文；已成包的旧话只带记事。"""
        covered = self.round_memory.covered_ids()
        reopen = reopen or set()
        current = {m["id"]: m for m in ctx}
        out = {m["id"]: current.get(m["id"], m)
               for m in self.round_memory.fallback_messages(max_messages=30, max_chars=6000)
               if m["id"] not in covered}
        for i, m in enumerate(ctx):
            if m.get("role") == "gap":
                before = next((x.get("ts", 0) for x in reversed(ctx[:i]) if x.get("role") != "gap"), 0)
                out[m["id"]] = {**m, "ts": before + .5}
                continue
            if m.get("role") != "gap" and (m["id"] in reopen or (m["id"] in fresh and m["id"] not in covered)):
                out[m["id"]] = m
        return sorted(out.values(), key=lambda m: m.get("ts", 0))

    def _window_context(self, ctx: list[dict], fresh: set[str]) -> list[dict]:
        """窗口判断用本轮原话；只读本群当前片段，不借跨会话池或恢复已成包历史。"""
        current = {m["id"]: m for m in ctx}
        previous = self.round_memory.window_messages([m for m in ctx if m["id"] not in fresh and _seen(m)])
        # 过长旧话只用裁剪后的显示视图；不能映射回完整 text，绕过 6000 字前文预算。
        out = {m["id"]: current[m["id"]] if m["id"] in current and m.get("text") == current[m["id"]].get("text")
               else {**m, "read": True} for m in previous}
        out.update((m["id"], m) for m in ctx if m["id"] in fresh)
        return sorted(out.values(), key=lambda m: m.get("ts", 0))

    def _record_read(self, msgs: list[dict]) -> None:
        self._round_read.update((m["id"], m) for m in msgs if m.get("role") in ("user", "her"))

    def _capture_shared(self, msgs: list[dict]) -> None:
        """实际请求前冻结正文/转述；准备中的 read 标记不是共享资格。"""
        for m in msgs:
            if m.get("role") in ("user", "her") and not m.get("system"):
                row = self.round_memory.shared_row({**m, "read": True})
                if row:
                    self._round_shared_inputs[row["id"]] = row

    def _confirm_shared(self, msgs: list[dict]) -> None:
        self._round_shared_ok.update((m["id"], self._round_shared_inputs[m["id"]])
                                     for m in msgs if m.get("id") in self._round_shared_inputs)

    def _publish_shared(self, msgs: list[dict]) -> None:
        if not self.memory.enabled() and not state_at(now_ms()).online:
            return
        snapshots = [self._round_shared_ok[m["id"]] for m in msgs
                     if m.get("id") in self._round_shared_ok and _seen(m) and not m.get("skipped")
                     and (m["id"] not in self._recalled_ids or m.get("answered"))]
        if snapshots:
            self._archive_action(self.round_memory.remember_shared, snapshots)

    def recent_snapshot(self) -> dict:
        """只读共享池入口：不用收件缓存或准备中的临时 read，也不读图片文件。"""
        history = self.history_archive.messages
        pending = {m["id"] for m in self.memory.unconsolidated(history)}
        invalid = {m["id"] for m in history if m.get("recalled") and not m.get("answered")}
        invalid.update(self._recalled_ids)
        return self.round_memory.recent_snapshot(pending, invalid)

    def _sync_round_segment(self) -> None:
        now = now_ms()
        if not state_at(now).online:
            self._close_window()
        segment = online_since(now) if state_at(now).online else None
        if state_at(now).online and segment is None:
            # 自定义作息/测试时的在线判定可能没有对应的固定时刻；仍不能误作下线。
            segment = self.store.extra.get("roundOnlineSegment") or now // (24 * HOUR) * (24 * HOUR)
        old = self.store.extra.get("roundOnlineSegment")
        if ("roundOnlineSegment" in self.store.extra and old != segment) or segment is None:
            self._round_offline()
        if old != segment:
            self.store.extra["roundOnlineSegment"] = segment
            self.store.save_state()

    def _finish_round(self, read: list[dict], sent: list[dict], remember: bool = True, reference: str = "",
                      image_ids: set[str] | None = None) -> None:
        """同一阅读轮只入队一次；实际发出的文字/表情才加入记事。"""
        self._sync_round_segment()
        msgs = list({m["id"]: m for m in [*read, *sent]
                     if (m.get("role") == "her" or (_seen(m) and not m.get("skipped")))
                     and (m["id"] not in self._recalled_ids or m["id"] in self._archive_saved_before)}.values())
        msgs.sort(key=lambda m: m.get("ts", 0))
        if msgs:
            self._archive_action(self.round_memory.enqueue, msgs, remember=bool(sent) or remember,
                                 segment=self.store.extra.get("roundOnlineSegment"), reference=reference[-1600:],
                                 image_ids=image_ids or (), batch=True)
            if not state_at(now_ms()).online:
                self._round_offline()
            self._publish_shared(msgs)

    def _round_offline(self) -> None:
        """上一段的记事退出随机池；这个群不整理长期记忆的话，记事没人再用，直接扔掉（用户 2026-10-31 定）。"""
        self._archive_action(self.round_memory.mark_offline)
        if not self.memory.enabled():
            self._archive_action(self.round_memory.drop_cold)
            self._archive_action(self.round_memory.clear_shared)

    async def _mem_chat(self, system, messages, tools=None, tool_choice="auto"):
        self.hub.events.count("模型调用")
        with measure(self.hub.events, "长期记忆"):
            return await self.llm.chat_with_retry(system, messages, tools, tool_choice)

    def _note(self, kind: str, text: str, level: str = "info") -> None:
        self.hub.events.note(kind, text, self.scope, level)

    def _leaving(self) -> dict | None:
        now = now_ms()
        return farewell.active(self.store, now, state_at(now).online)

    def _commit_farewell(self) -> None:
        if self._farewell_edge is None:
            return
        farewell.begin(self.store, now_ms(), self._farewell_edge)
        self._mark_farewell(now_ms())
        self.store.extra["windowUntil"] = 0
        self.store.extra.pop("windowExchange", None)
        for m in self.msgs:
            if m["role"] == "user" and m.get("called") and not m.get("read"):
                m["left"] = True
        self.store.save_messages()
        self.store.save_state()

    async def _farewell_reply(self) -> bool:
        state = self._leaving()
        if self.closed or not state or state.get("used") or not state_at(now_ms()).online or self.in_flight:
            return False
        target = next((m for m in self.msgs if m.get("farewellFollowup") and m.get("left") and m.get("called")
                       and not m.get("farewellTried")), None)
        if target is None:
            return False
        async with self._hold("道别收尾", summarize=False):
            self._asked = [target["id"]]
            self._farewell_grace = True
            try:
                previous = [m for m in self.msgs[:self.msgs.index(target)] if _seen(m)]
                self._protect_context([target])
                relationship = (self.hub.intimacy.cognition(target.get("uid"), target.get("name"))
                                if self.hub.trailblazer and target.get("uid") == self.hub.trailblazer else
                                f"{target.get('name') or '这位群友'}不是主要联系人。" +
                                (self.hub.intimacy.brief(target.get("uid"), target.get("name"))
                                 if self.hub.intimacy else ""))
                target["farewellTried"] = True
                self.store.save_messages()
                self.hub.events.count("模型调用")
                try:
                    self._capture_shared([*previous[-6:], target])
                    with measure(self.hub.events, "群回复"):
                        result = await asyncio.wait_for(
                            self.llm.chat(farewell.followup_system(winext.GROUP_IDENTITY, relationship,
                                                                  now=now_ms(), state_fn=state_at),
                                          farewell.followup_turns(previous, [target], group=True)),
                            timeout=farewell.FOLLOWUP_TIMEOUT_S)
                except Exception as exc:
                    self.hub.events.count("模型失败")
                    self._note("出错", f"道别收尾没生成：{type(exc).__name__}；这条不自动重试", "warn")
                    return False
                if not result.ok:
                    self.hub.events.count("模型失败")
                    self._note("道别", "收尾没有生成可用的话；这条不自动重试")
                    return False
                self._confirm_shared([*previous[-6:], target])
                text = farewell.followup_text(result.text)
                if (not text or self.closed or self._focus_recalled([target["id"]])
                        or not state_at(now_ms()).online or self._leaving() is not state or state.get("used")):
                    return False
                sent = await self.say([text], target=target,
                                      cut=lambda: self.closed or self._focus_recalled([target["id"]])
                                      or not state_at(now_ms()).online or self._leaving() is not state or state.get("used"))
            finally:
                self._farewell_grace = False
                self._asked = None
            if sent:
                target["read"] = target["shown"] = True
                target.pop("left", None)
                target.pop("farewellFollowup", None)
                self._record_read([target])
                self.store.save_messages()
                self.hub.her.save()
                self.hub.events.count("群里说话")
                self._note("道别", "又简短回了一人；本群等下次上线再聊")
            return sent

    # ---------- 收消息 ----------

    async def on_message(self, uid: str, name: str, text: str, attachments: list[dict], qid: str | None, called: bool) -> None:
        if self.closed:
            return
        now = now_ms()
        her = state_at(now)
        m = {**self.store.new_msg("user", text, attachment=attachments[0] if attachments else None, ts=now),
             "uid": uid, "name": name, "called": called, "read": False}
        if qid:
            m["qid"] = qid
        self.msgs.append(m)
        if self._archive_read():  # 先存已读部分，再裁未读收件缓存；写盘失败保留原文重试。
            active_ids = {x["id"] for x in self._archive_context}
            protected = [x for x in self.msgs[:-BUFFER_MAX] if x["id"] in active_ids]
            self.msgs[:] = [*protected, *self.msgs[-BUFFER_MAX:]]
        self.store.save_messages()
        # 实际离线时发的是状态应答；上一段在线里的道别锁不吞掉任务／休息通知。
        if not her.online:
            self._close_window()
            if called:
                m["left"] = True  # 留言：她上线后补看；状态应答不算正式回答。
                self.store.save_messages()
                if self.hub.trailblazer and uid == self.hub.trailblazer:
                    await self._mission_ack(m, her, now)
                else:
                    await self._auto_reply(uid, her, now, m["id"])
            return
        leaving = self._leaving()
        if leaving:
            if called:
                m["left"] = True
                if her.online:
                    m["farewellFollowup"] = True
                self.store.save_messages()
                if her.online and not leaving.get("used"):
                    self.schedule(passive=True)
            return  # 在线道别后，普通群消息不占最后一次收尾。
        if called:
            self._note("被叫到", f"{name}：{text[:30]}")
            self.schedule(passive=True)
        elif (self.window_open(now) or self.in_flight) and self.hub.proactive_on(self.gid) \
                and current_energy(self.hub.her.energy, now)["value"] >= TOO_TIRED_TO_ASK:
            # 接话窗口只在「主动看」开着的群里接：关着 = 她一般不看这个群，只回叫到她的
            # 她正在回话（窗口要等她发出去才开）：先记下，说完再看
            self.schedule(passive=False)

    async def on_recall(self, qid: str) -> None:
        if self.closed:
            return
        # Platform messages containing several images share one qid. Process every
        # internal record, even when one image was already answered or archived.
        candidates = {m["id"]: m for m in self.history_archive.lookup_qid_all(qid) if m["role"] == "user"}
        candidates.update({m["id"]: m for m in self._archive_context
                           if m.get("qid") == qid and m["role"] == "user"})
        candidates.update({m["id"]: m for m in self.msgs
                           if m.get("qid") == qid and m["role"] == "user"})
        if not candidates:
            return
        live = {m["id"]: m for m in self.msgs}
        removed = set()
        for mid, m in candidates.items():
            if m.get("answered"):
                if mid in live:
                    if _seen(m):
                        self._archive_action(self.history_archive.sync, [{**m, "recalled": False}])
                    m["recalled"] = True
                self._archive_action(self.history_archive.mark_recalled, [mid])
                continue
            removed.add(mid)
            self._recalled_ids.add(mid)
            self._archive_action(self.history_archive.mark_recalled, [mid])
        self.msgs[:] = [m for m in self.msgs if m["id"] not in removed]
        self._archive_context = [m for m in self._archive_context if m["id"] not in removed]
        if removed:
            self.recalls += 1
        self.store.save_messages()
        if self.hub.trailblazer and any(m.get("left") and m.get("uid") == self.hub.trailblazer for m in candidates.values()) and not any(
                m.get("left") and m.get("uid") == self.hub.trailblazer for m in self.msgs):
            self.store.extra.pop("promised", None)
            self.store.save_state()
        self._note("撤回", f"平台消息被撤回：移出本轮 {len(removed)} 条，已回复记录保留来源标记")

    async def _auto_reply(self, uid: str, her, now: int, target_id: str | None = None,
                          segment: str | None = None) -> None:
        """离线状态通知：每人每段任务／休息各一次，排队后不能迟发上一段的通知。"""
        if self.closed or her.online:
            return
        segment = segment or offline.segment_key(now, state_at)
        if segment is None:
            return
        self._close_window()
        if self.in_flight:
            previous = self._pending_auto.get(uid)
            targets = list(previous[0]) if previous and previous[1] == segment else []
            if target_id not in targets:
                targets.append(target_id)
            self._pending_auto[uid] = (targets, segment)
            return
        async with self._hold("自动回复"):
            # 在占用里面检查：失效的待办也走 _hold 的 finally，继续放行后面的人。
            current_now = now_ms() if target_id is not None else now
            current = state_at(current_now) if target_id is not None else her
            if current.online or offline.segment_key(current_now, state_at) != segment:
                return
            if target_id is not None and not any(m["id"] == target_id for m in self.msgs):
                return
            await self._auto_reply_turn(uid, current, current_now, segment)

    async def _auto_reply_turn(self, uid: str, her, now: int, segment: str | None = None) -> None:
        if her.online:
            return
        opts = self.hub.options()
        if not opts.get("offline_reply", True):
            return
        seg = segment or offline.segment_key(now, state_at)
        replied = self.store.extra["autoReplied"]
        if seg is None or offline.matched(replied.get(uid), seg):
            return
        text = (opts.get("offline_text_mission", "对方正在忙碌中") if offline.kind(her) == "mission"
                else opts.get("offline_text_rest", "对方正在休息中"))
        if not text:
            return
        if await self.hub.sender.send(self.gid, text):
            replied[uid] = seg  # 只有真正送达才占额度；失败或取消后下一条仍可以通知。
            self.store.save_state()
            self._note("自动回复", text)
        else:
            self._note("出错", "自动回复没发出去", "warn")

    async def _mission_ack(self, m: dict, her, now: int, segment: str | None = None) -> None:
        """任务／休息时主要联系人叫她，由主模型简短应答一次；仍留待上线正式补看。"""
        if self.closed or her.online or not self.hub.options().get("offline_reply", True):
            return
        segment = segment or offline.segment_key(now, state_at)
        if segment is None:
            return
        self._close_window()
        if self.in_flight:
            previous = self._pending_ack
            targets = list(previous[0]) if previous and previous[1] == segment else []
            if m["id"] not in targets:
                targets.append(m["id"])
            self._pending_ack = (targets, segment)
            return
        async with self._hold("任务中应答" if her.activity == "mission" else "休息中应答", summarize=False):
            if m not in self.msgs:
                return  # 刚放行就撤回了也走 finally，继续查看同段的其他候选请求。
            await self._mission_ack_turn(m, her, now, segment)


    async def _mission_ack_turn(self, m: dict, her, now: int, segment: str | None = None) -> None:
        hub, x = self.hub, self.store.extra
        seg = segment or offline.segment_key(now, state_at)
        current = state_at(now_ms())
        if current.online or offline.segment_key(now_ms(), state_at) != seg:
            return
        if (seg is None or offline.matched(x.get("missionAck"), seg)
                or offline.matched(x["autoReplied"].get(hub.trailblazer), seg)):
            return
        self._protect_context([m])  # 等模型时收件缓存可裁剪，真正撤回仍由 on_recall 移除。
        def valid():
            return (not self.closed and m in self.msgs and not m.get("recalled")
                    and offline.segment_key(now_ms(), state_at) == seg
                    and hub.options().get("offline_reply", True))

        system = winext.offline_ack_prompt(now, offline.kind(current), self._mood_note(now),
                                          group=True, state_fn=state_at, cognition=hub.intimacy.trailblazer_note())
        # 通知只读这次叫她的话，不重新展开历史／共享池，不引入回想或图片等待。
        turns = [{"role": "user", "content": f"{m.get('name') or '对方'}：{m.get('text') or ''}"
                  + image_note(m.get("attachment"))}]
        with measure(hub.events, "群回复"):
            text = await offline.short_reply(self.llm, system, turns, valid=valid,
                                             count=hub.events.count, note=self._note, track=self._track)
        text = text or offline.text(hub.options(), offline.kind(current), trailblazer=True)
        if not text or not valid():
            return
        cut = lambda: not valid()
        if not cut() and await self.say([text], target=m, cut=cut, offline_notice=True):
            # 只有实际通知送出后才归档本次目标；发送等待中已撤回的不能复活。
            if m in self.msgs and m["id"] not in self._recalled_ids and not m.get("recalled"):
                m["read"] = True
                self._record_read([m])
                self._capture_shared([m])
                self._confirm_shared([m])
                self.store.save_messages()
            x["missionAck"] = seg
            x["autoReplied"][hub.trailblazer] = seg
            if any(row.get("left") and row.get("uid") == hub.trailblazer for row in self.msgs):
                x["promised"] = True  # 保留尚待正式补看的留言；不恢复已撤回目标的承诺。
            else:
                x.pop("promised", None)
            self._note("离线应答", f"主要联系人叫她，她抽空回了一句：{text}")
        self.store.save_state()


    def _close_window(self) -> None:
        """实际下线就清掉接话窗口，任务中那句不延长也不复活旧窗口。"""
        x = self.store.extra
        if x.get("windowUntil") or x.get("windowMisses") or "windowExchange" in x:
            x["windowUntil"] = x["windowMisses"] = 0
            x.pop("windowExchange", None)
            self.store.save_state()
        self.more_window = False

    def window_open(self, now: int) -> bool:
        if not state_at(now).online:
            self._close_window()
            return False
        exchange = self.store.extra.get("windowExchange")
        if isinstance(exchange, dict) and exchange.get("segment") != online_since(now // MIN * MIN):
            self._close_window()
            return False
        if now >= self.store.extra["windowUntil"]:
            # 首条送出已登记来源，但整轮还没发完、窗口还未开；普通来话不能清掉它。
            if not self._window_exchange_sending:
                self._close_window()
            return False
        return not self._leaving()

    def _window_engaged(self, now: int) -> bool:
        """仅主动开场且尚无人接上的窗口，不需要临下线道别。旧窗口保留原行为。"""
        if not self.window_open(now):
            return False
        exchange = self.store.extra.get("windowExchange")
        return not isinstance(exchange, dict) or bool(exchange.get("engaged"))

    def _commit_window_exchange(self) -> None:
        if self._window_exchange_pending is None:
            return
        self.store.extra["windowExchange"] = self._window_exchange_pending
        self._window_exchange_pending = None
        self.store.save_state()

    # ---------- 排一轮 ----------

    def schedule(self, passive: bool) -> None:
        if self.closed:
            return
        leaving = self._leaving()
        if leaving and (not passive or leaving.get("used") or not state_at(now_ms()).online):
            return
        if self.in_flight:
            if passive:
                self.more_came = True
            else:
                self.more_window = True
            return
        now = now_ms()
        if self.batch_task is None:
            self.batch_start, self.batch_passive = now, passive
        else:
            self.batch_task.cancel()
            # 这一批里有人叫过她：后来的普通消息不会把它降成接话判断
            self.batch_passive = self.batch_passive or passive
        passive = self.batch_passive
        lo, hi = BATCH_MS if passive else WINDOW_DEBOUNCE_MS
        wait = max(300, min(random.randint(lo, hi), BATCH_MS[1] - (now - self.batch_start)))
        self.batch_task = asyncio.get_running_loop().create_task(self._fire(wait, passive))

    async def _fire(self, wait_ms: int, passive: bool) -> None:
        try:
            await asyncio.sleep(wait_ms / 1000)
        except asyncio.CancelledError:
            return
        self.batch_task = None
        self._track(asyncio.current_task())
        if not state_at(now_ms()).online:
            self._close_window()
            # 等的这几秒里她下线了：叫她的记成留言，上线后补看
            left = [m for m in self.msgs if m["role"] == "user" and not m.get("read") and m.get("called")]
            for m in left:
                m["left"] = True
            if left:
                self.store.save_messages()
                self._note("留言", f"她刚下线，{len(left)} 条叫她的等上线再看")
            return
        await (self.passive_turn() if passive else self.window_turn())

    @asynccontextmanager
    async def _hold(self, phase: str = "回复", *, summarize: bool = True):
        """占住这个群：判断、回话、补看、道别。做完再看期间来了什么"""
        self.in_flight = True
        self.phase = phase
        self._archive_before = {m["id"] for m in self.msgs if _seen(m)}
        self._archive_saved_before = {m["id"] for m in self.history_archive.messages}
        self._archive_context = []
        self._round_read, self._round_sent, self._round_remember = {}, [], True
        self._round_reference = ""
        self._round_speaker = ""
        self._round_images = set()
        self._round_selection_key = ""
        self._round_shared_inputs, self._round_shared_ok = {}, {}
        self._last_local_note = ""
        try:
            yield
        except BaseException:
            if not self._round_sent:
                self._round_shared_ok.clear()
            raise
        finally:
            self.in_flight = False
            self._farewell_edge = None
            self.phase = ""
            self._archive_read()
            if summarize:
                self._finish_round(list(self._round_read.values()), self._round_sent, self._round_remember,
                                   self._round_reference, self._round_images)
            else:
                self._publish_shared([*self._round_read.values(), *self._round_sent])
            self._archive_context = []
            self._archive_before.clear()
            self._archive_saved_before.clear()
            self._round_read, self._round_sent = {}, []
            self._round_shared_inputs, self._round_shared_ok = {}, {}
            called, window = self.more_came, self.more_window
            self.more_came = self.more_window = False
            if self.closed:
                return
            self.schedule_history()
            current = state_at(now_ms())
            if not current.online:
                self._close_window()
                key = offline.segment_key(now_ms(), state_at)
                pending, self._pending_ack = self._pending_ack, None
                by_id = {m["id"]: m for m in self.msgs}
                target = next((by_id[mid] for mid in pending[0] if mid in by_id), None) if pending else None
                self._pending_auto = {uid: item for uid, item in self._pending_auto.items()
                                      if item[1] == key and not offline.matched(self.store.extra["autoReplied"].get(uid), key)}
                if (target is not None and pending[1] == key
                        and not offline.matched(self.store.extra.get("missionAck"), key)):
                    remaining = [mid for mid in pending[0] if mid in by_id and mid != target["id"]]
                    if remaining:
                        self._pending_ack = (remaining, key)
                    self.spawn(self._mission_ack(target, current, now_ms(), key))
                elif self._pending_auto:
                    uid = next(iter(self._pending_auto))
                    targets, segment = self._pending_auto.pop(uid)
                    # 后来那条撤回，不得把同人同段尚有效的较早请求一起丢掉。
                    # 全撤回时仍走一次 _auto_reply 的失效目标分支，让其 finally 放行其他人。
                    target_id = next((mid for mid in targets if mid is None or mid in by_id), targets[0])
                    remaining = [mid for mid in targets if mid != target_id and (mid is None or mid in by_id)]
                    if remaining:
                        self._pending_auto[uid] = (remaining, segment)
                    self.spawn(self._auto_reply(uid, current, now_ms(), target_id, segment))
                left = [m for m in self.msgs if m["role"] == "user" and m.get("called") and not m.get("read")]
                if left:
                    for m in left:
                        m["left"] = True
                    self.store.save_messages()
            else:
                self._pending_ack = None
                self._pending_auto.clear()  # 已上线，旧状态通知不再迟发。
                leaving = self._leaving()
                unread = [m for m in self.msgs if m["role"] == "user" and not m.get("read")]
                if leaving:
                    if called and not leaving.get("used"):
                        self.schedule(passive=True)
                elif any(m.get("left") for m in self.msgs):
                    # 任务中一句尚未答完就上线了：占用释放后补正式回答。
                    self._schedule_tick(now_ms())
                elif called and any(m.get("called") for m in unread):
                    self.schedule(passive=True)
                elif (called or window) and unread and self.window_open(now_ms()) and self.hub.proactive_on(self.gid):
                    self.schedule(passive=False)

    def user_since_her(self) -> list[dict]:
        """她上一次说话之后，群友说的（看没看过都算）"""
        out = []
        for m in reversed(self.msgs):
            if m["role"] == "her":
                break
            out.insert(0, m)
        return out

    async def passive_turn(self) -> None:
        """被叫到：直接回（不问判断模型）"""
        if self.in_flight:
            self.more_came = True
            return
        if self._leaving():
            await self._farewell_reply()
            return
        unread = [m for m in self.msgs if not m.get("read") and m["role"] == "user"]
        if not unread:
            return
        focus = [m["id"] for m in unread if m.get("called")]
        if not focus:
            return  # 合并窗口里唯一叫她的撤回了，普通聊天不能冒充一次被叫到。
        await self.turn("called", focus)

    async def window_turn(self) -> None:
        """接话窗口里的新消息：先挡掉明显不相干的，再问判断模型"""
        if not state_at(now_ms()).online:
            self._close_window()
            return
        if self._leaving():
            return
        now = now_ms()
        if self.in_flight:
            self.more_window = True
            return
        if not self.window_open(now) or not self.hub.proactive_on(self.gid):
            return
        new = [m for m in self.msgs if m["role"] == "user" and not m.get("read")]
        if not new:
            return
        if all(_trivial(m) for m in new):
            return  # 空内容或机械刷屏留着和下一批一起看，简短答复和图片不在这里丢掉。
        if current_energy(self.hub.her.energy, now)["value"] < TOO_TIRED_TO_ASK:
            return  # 排队时还有精力，等的这几秒里别处把精力耗掉了：不判断（叫她的照样走「被叫到」）
        async with self._hold("接话判断"):
            pool = [m for m in self.msgs if _seen(m) or m in new]
            # 本轮尚未成包的原话（包含她的整段回应）与新话一起判断，不从最后一个气泡截断。
            d = await self.assess(pool, 0, "window", new)
            if d.get("skipped"):
                return  # 等图的时候精力被别处耗到 20 以下：不判断，这几条留着
            judged = [m for m in d["read"] if not m.get("called") and not m.get("read")]  # 这一轮判断时才翻到的
            for m in judged:  # 叫她的留给「被叫到」那条路
                m["read"] = True
            self._record_read(d["read"])
            self._round_remember = d.get("remember", True)
            self.store.save_messages()
            self._note("判断", f"接话窗口：{'接' if d['reply'] else '不接'}（{d['reason']}）")
            if not d["reply"]:
                self._miss()
                return
            await self._turn("window", d["focus"], None, d["bg"], d["to"], judged=judged)

    # ---------- 判断：她自己的完整提示词 + 她这边的背景 ----------

    def _threads(self, now: int) -> list[str]:
        return [thread_line(t, now) for t in open_threads(self.memory.m, now)]

    async def background(self, query: str, now: int) -> dict:
        """她这边的背景：联想记忆（跨来源）、以前的事、提到的人。判断和回话共用，一批只查一次。
        查到的先不记账，她真开口了才调 commit()（没接就当没想起过）。"""
        hub = self.hub
        rec, c1 = await self.memory.associate_deferred(query, self.memory_session(), now)
        past, c2 = await hub.knowledge.lookup_deferred(query, now) if hub.knowledge else (None, lambda: None)
        people = await hub.people.lookup(query) if hub.people else None

        def commit() -> None:
            c1()
            c2()

        return {"recollection": rec, "past": past, "people": people, "commit": commit}

    def _judge_system(self, mode: str, shown: list[dict], bg: dict, now: int) -> str:
        """和她回话那轮同一套（身份、群聊说明、在场的人、悬着的事、想起来的事、累不累、时间），
        不带只管怎么说话的（风格样例、口吻、接话方式、开头提醒），最后接上判断那一段"""
        hub = self.hub
        e = current_energy(hub.her.energy, now)["value"]
        return "\n\n".join(s for s in [
            persona.build_system_prompt(self._mood_note(now), "plain", identity_line=winext.GROUP_IDENTITY,
                                        energy_note="tired-still" if e < TIRED else None),
            winext.group_block(self.name, hub.trailblazer),
            winext.participants_block(self._participants(shown)),
            winext.threads_block(self._threads(now)),
            bg["recollection"], bg["past"], bg["people"],
            winext.time_block(now, group=True),
            activity.current_context(now, state_fn=state_at),
            winext.judge_block("", ENERGY_BAND_NAME[energy_band(e)], mode),
            self.history_archive.source_note(shown),
        ] if s)

    async def assess(self, pool: list[dict], start: int, mode: str, query_msgs: list[dict]) -> dict:
        """问判断模型要不要接。
        接话窗口：只看要不要接着说，短提示词、不查背景（真要接，回话那轮再查）。
        看手机：要看她真感不感兴趣，用她的完整提示词 + 背景；query_msgs 是拿去查背景的消息（最多最近 12 条）"""
        if self._leaving():
            return {"reply": False, "interest": 0, "reason": "已道别", "focus": [], "to": "", "read": [], "skipped": True}
        hub, now = self.hub, now_ms()
        deadline = now + WAIT_MS
        fresh = {m["id"] for m in pool if m.get("role") == "user" and not m.get("read")}
        # 显示视图可以裁剪旧长句；读状态、归档及共享快照仍使用先前实际读过的原对象。
        window_original = {m["id"]: m for m in [*self._read_history(), *pool]} if mode == "window" else {}
        def originals(msgs):
            return [window_original.get(m["id"], m) for m in msgs]
        if mode == "window":
            pool, start = self._window_context(pool, fresh), 0
        else:
            initial = {m["id"] for m in pool[start:]}
            pool = self._request_context(pool, fresh)
            start = next((i for i, m in enumerate(pool) if m["id"] in initial), max(0, len(pool) - 30))
        query = " ".join(m.get("text", "") for m in query_msgs[-QUERY_MAX:])
        query_speakers = {m.get("uid") for m in query_msgs if m.get("role") == "user" and m.get("uid")}
        selection_key = self._round_selection_key if self.in_flight and self._round_selection_key else f"{mode}:{pool[-1]['id'] if pool else now}"
        if mode == "window":
            memo = self.round_memory.window_note()
            self._last_local_note = memo
            if self.in_flight:
                self._round_selection_key = self._round_selection_key or selection_key
                self._round_reference = memo
        else:
            memo = await self._history_note(query, pool[start:], selection_key,
                                            next(iter(query_speakers)) if len(query_speakers) == 1 else "")
        self._protect_context(originals(pool[start:]))  # 判断尚未返回，原始收件不能被400条缓存裁掉。
        if mode == "window":
            bg = None
            e = current_energy(hub.her.energy, now)["value"]
            system = winext.window_judge_system(self.name, "", ENERGY_BAND_NAME[energy_band(e)],
                                                self._speakers_brief(pool[start:]))
            system += "\n\n" + winext.time_block(now, group=True)
            system += "\n\n" + activity.current_context(now, state_fn=state_at)
            system += "\n当前心情：" + self._mood_note(now)
            source = self.history_archive.source_note(pool[start:])
            if source:
                system += "\n" + source
            if memo:
                system += "\n\n" + memo
        else:
            query = " ".join(m["text"] for m in [x for x in query_msgs if x["role"] == "user" and not _trivial(x)][-QUERY_MAX:])
            bg = await self.background(query, now)
            system = self._judge_system(mode, pool[start:], bg, now)
            if memo:
                system += "\n\n" + memo
        # 每次交给判断模型之前（包括「多看几条」翻到更早的）：要看的图先转述好
        # 精力在问判断模型的那一刻再看一次（共用的精力，等图的时候可能被别处耗掉）
        async def prepare(msgs):
            self._protect_context(originals(msgs))  # 多看几条新增的实际阅读范围也保护，其他pool消息不提前算已读。
            await self._see_images(msgs, deadline)
            self._capture_shared(originals(msgs))
        with measure(hub.events, "群判断"):
            d = await judge(self.judge_llm, system, mode, pool, start, hub.trailblazer, lambda: hub.events.count("判断调用"),
                            prepare=prepare,
                            gate=lambda: current_energy(hub.her.energy, now_ms())["value"] >= TOO_TIRED_TO_ASK,
                            system_for=(lambda msgs: self._judge_system(mode, msgs, bg, now) + ("\n\n" + memo if memo else "")) if mode == "look" else None)
        if mode == "window":
            d["read"] = originals(d["read"])
        d["bg"] = bg
        if d.get("read_completed", True) and not d.get("skipped"):
            self._confirm_shared(d["read"])
        d["reference"] = (self._round_reference or self._last_local_note) \
            if callable(getattr(self.hub, "recent_context", None)) else memo
        d["selectionKey"] = selection_key
        return d

    async def _see_images(self, msgs: list[dict], deadline: int | None = None) -> None:
        """她的模型看不了图：要看的图先转述好，等它们转完（caption.py；deadline 是这一轮等到什么时候为止）"""
        if self.hub.captions:
            await self.hub.captions(self.gid, msgs, self.store.save_messages, deadline)

    def _miss(self) -> None:
        """这次没接（判断不接，或者她自己不想回）：接话窗口里连着两次就悄悄退出"""
        x = self.store.extra
        x["windowMisses"] = x.get("windowMisses", 0) + 1
        if x["windowMisses"] >= 2 and self.window_open(now_ms()):
            x["windowUntil"] = 0
            self._note("接话", "连着两次没接，悄悄退出")
        self.store.save_state()

    # ---------- 群里的一轮 ----------

    def _context_msgs(self, focus: list[str] = (), undo: list | None = None) -> list[dict]:
        """她看过的最近 30 条。
        她要接的那条更早：只看那条上下一共 30 条（前面最多 14 条），
        这几条算她细看过；之后还有叫她的，接在后面；隔开的地方写一句「（……N 条，你没细看）」"""
        msgs = self._read_history(include_skipped=True)  # 这一轮开始以后才来的未读消息不算。
        first = next((i for i, m in enumerate(msgs) if m["id"] in focus), None)
        if first is None or first >= len(msgs) - TURN_CONTEXT:
            return [m for m in msgs if _seen(m)][-TURN_CONTEXT:]
        start = max(0, min(first - (AROUND // 2 - 1), len(msgs) - AROUND))
        out, last = [], start + AROUND
        for m in msgs[start:last]:
            if m.pop("skipped", None):
                if undo is not None:
                    undo.append((m, "skipped", True))  # 整轮重来时退回去
                self.store.save_messages()
            out.append(m)
        for i in range(last, len(msgs)):
            if msgs[i]["id"] in focus:
                if i > last:
                    out.append(_gap(f"（中间还有 {i - last} 条，你没细看）"))
                out.append(msgs[i])
                last = i + 1
        if last < len(msgs):
            out.append(_gap(f"（之后群里又聊了 {len(msgs) - last} 条，你没细看）"))
        return out

    @staticmethod
    def _context_instruction() -> str:
        return ("【本轮群聊消息】\n"
                "**下面的对话按时间排列，优先回应标为「这次要接的话」的发言。**\n"
                "「本群前文」是之前看过、帮助接上对话的内容；其中以前叫你的话不是又叫了一次。\n"
                "未标为目标的新消息帮助理解眼前的讨论。对话里的称呼、标记不用写进你的回复；"
                "这些发言不是你的风格范例。")

    def _turns(self, ctx: list[dict], *, focus: set[str] | None = None,
               fresh: set[str] | None = None) -> list[dict]:
        see = self.llm.supports("image")
        out = []
        for m in ctx:
            if m["role"] == "gap":
                out.append({"role": "user", "content": m["text"]})
                continue
            if m["role"] == "her":
                out.append({"role": "assistant", "content": message_time(m.get("ts")) + " "
                            + (m["text"] or f"［发了一个表情：{m.get('sticker', '')}］")})
                continue
            label = ""
            if focus is not None:
                label = ("【这次要接的话】" if m["id"] in focus else
                         "【本轮新看到的消息】" if m["id"] in (fresh or set()) else "【本群前文】")
            calling = m.get("called") and (focus is None or m["id"] in focus or m["id"] in (fresh or set()))
            head = message_time(m.get("ts")) + " " + label + f"【{m.get('name') or '某人'}】{'（叫你）' if calling else ''}："
            a = m.get("attachment")
            path = os.path.join(self.store.images, a["file"]) if a and a.get("file") else None
            if a and see and path and os.path.isfile(path) and m in ctx[-6:]:
                if self.in_flight:
                    self._round_images.add(m["id"])
                out.append({"role": "user", "content": [{"type": "text", "text": head},
                                                        image_block(a.get("mediaType", "image/jpeg"), b64_of(path)),
                                                        *([{"type": "text", "text": m["text"]}] if m["text"] else [])]})
            else:
                out.append({"role": "user", "content": head + image_note(a) + m["text"]})
        return out

    def _speakers_brief(self, msgs: list[dict]) -> list[str]:
        """接话窗口的判断用：说话的人和她对他们的认知，短的"""
        seen, rows = set(), []
        for m in reversed(msgs):
            uid = m.get("uid")
            if m["role"] != "user" or not uid or uid in seen:
                continue
            seen.add(uid)
            name = m.get("name") or uid
            rows.append(f"{name}：{self.hub.intimacy.brief(uid, name)}")
            if len(rows) >= 6:
                break
        return list(reversed(rows))

    def _participants(self, ctx: list[dict]) -> list[str]:
        seen, rows = set(), []
        tb = self.hub.trailblazer
        for m in reversed(ctx):
            uid = m.get("uid")
            if m["role"] != "user" or not uid or uid in seen:
                continue
            seen.add(uid)
            name = m.get("name") or uid
            if uid == tb:
                rows.append(f"{name}：{self.hub.intimacy.cognition(uid, name)}")
            else:
                facts = self.memory.facts_about(uid)
                rows.append(f"{name}：{self.hub.intimacy.cognition(uid, name)}" + (f"你记得：{'；'.join(facts)}" if facts else ""))
            if len(rows) >= 6:
                break
        return list(reversed(rows))

    def _farewell_done(self, now: int) -> bool:
        """这一段在线里，这个群道过别了没（存在 state.json：重启了也算数）"""
        seg = online_since(now)
        return seg is not None and self.store.extra.get("farewellFor") == seg

    def _mark_farewell(self, now: int) -> None:
        self.store.extra["farewellFor"] = online_since(now)
        self.store.save_state()

    async def turn(self, mode: str, focus: list[str], extra: str | None = None, bg: dict | None = None,
                   to: str = "", judged: list | None = None, round_read: list | None = None,
                   remember: bool = True, selection_key: str = "") -> bool | None:
        """mode: called 被叫到 / window 接话窗口 / look 看手机 / catchup 上线补看 / farewell 道别。
        bg：判断时已经查好的背景（有就不再查）；to：判断说她接的是「某个人」还是「话题」。
        返回 True = 这轮处理完了（说了，或者她自己不说）；False = 没成（模型失败、消息没发出去）；None = 群里正忙"""
        if self.in_flight:
            return None
        if self._leaving():
            return await self._farewell_reply() if mode == "called" else None
        async with self._hold("补看" if mode == "catchup" else "道别" if mode == "farewell" else "回复"):
            self._round_selection_key = selection_key
            self._record_read(round_read or [])
            self._round_remember = remember
            return await self._turn(mode, focus, extra, bg, to, farewell=mode == "farewell", judged=judged)

    async def _turn(self, mode: str, focus: list[str], extra: str | None, bg: dict | None, to: str,
                    farewell: bool = False, judged: list | None = None) -> bool:
        """farewell：这轮带着道别（真发出去了才记「这一段道过别」）；judged：这一轮判断时才翻到的（被打断时也算翻过去）"""
        if self._leaving():
            return False
        hub, now = self.hub, now_ms()
        recalls0 = self.recalls
        self._archive_saved_before.difference_update(m["id"] for m in judged or [])
        # 这一轮改过的「看过 / 翻过去」（从判断那一步算起）：这一轮被打断时，这一轮翻到的都改成「翻过去」（不进记忆）
        undo: list = [(m, "read", None) for m in judged or []]
        if focus and self._focus_recalled(focus):
            return self._interrupted(undo)  # 判断的那阵子她要接的就被撤回了
        # 她拿起手机看这个群：还没看的都算看过了；这一轮没交给她的（太早的）只是翻过去，没细看——她要接的那几条除外
        unread = [m for m in self.msgs if m["role"] == "user" and not m.get("read")]
        # 判断的这阵子、或者别的事情的这阵子有人叫她：也一起接（不然就被算成看过、再也不回了）
        focus = [*focus, *[m["id"] for m in unread if m.get("called") and m["id"] not in focus]]
        for m in unread:
            undo.append((m, "read", m.get("read")))
            m["read"] = True
        logical_ctx = self._context_msgs(focus, undo)
        fresh = ({m["id"] for m in unread} | {m["id"] for m in judged or []} | set(self._round_read)
                 | {m["id"] for m, key, old in undo if key == "skipped" and old})
        # 任务中已看过/应答过的留言仍需正式回答，作为明确回查只重开这几条。
        reopen = {m["id"] for m in logical_ctx if mode == "catchup" and m.get("left") and m["id"] in focus}
        ctx = self._request_context(logical_ctx, fresh, reopen)
        self._protect_context(ctx)  # 保留本轮对象；收件缓存裁剪后仍可提交最终确实读过的部分。
        shown = {m["id"] for m in ctx}
        for m in unread:
            if m["id"] not in shown and m["id"] not in focus:
                undo.append((m, "skipped", m.get("skipped")))
                m["skipped"] = True
        self.store.save_messages()
        # 要看的图先转述好；等的这阵子她下线了、有人撤回了，再看一遍
        await self._see_images(ctx, now + WAIT_MS)
        if self.recalls != recalls0:
            # 有人撤回了：撤的是她要接的 → 这一轮打断，不重来（用户 2026-10-30 定）；撤的是别的 → 从上下文里拿掉接着回
            # （先看撤回、再看下线：不然同一轮的会被记成留言，上线又补）
            if self._focus_recalled(focus):
                return self._interrupted(undo)
            ctx = self._prune(ctx)
        if self._went_offline(focus):
            self._restore_unread_preparation(undo)
            return mode != "catchup"
        # 最后一条是她说的就不用接了——除非要接的那几条她回上一句时还没看到（比如她发回复的时候才来的）；
        # 道别、补看除外（补看时最后一条可能是她任务中回主要联系人的那句）
        # （判断说要接、但没指出接哪条的：看上下文里有没有她还没看到的）
        if not logical_ctx or (logical_ctx[-1]["role"] == "her" and mode not in ("farewell", "catchup")
                       and not self._unshown(focus or [m["id"] for m in logical_ctx if m["role"] == "user"])):
            return True
        focus_msgs = [m for m in logical_ctx if m["id"] in focus] or [m for m in ctx if m["role"] == "user"][-3:]
        said = " ".join(m["text"] for m in focus_msgs)
        recent = " ".join(m["text"] for m in [x for x in ctx if x["role"] == "user"][-3:])
        x = self.store.extra

        # 明确被叫到、窗口判断确认承接，或尚有效的既有往来，才需要道别。
        # look 准备期间并入的叫她消息也算；普通群消息到达本身不解锁。
        engaged = (self._window_engaged(now) or mode in ("called", "window", "catchup")
                   or any(m["id"] in focus and m.get("called") and not m.get("answered")
                          and not m.get("shown") for m in focus_msgs))
        # 道别：这一轮离下线 ≤ 12 分钟
        until = offline_after(now)
        minutes_left = (until - now) / MIN if until else math.inf
        if engaged and not extra and minutes_left <= 12 and not self._farewell_done(now) and mode != "catchup":
            extra = persona.build_farewell_context(minutes_left)
            farewell = True  # 真发出去了才记「这一段道过别」（没问成、没发出去、准备作废都不算）

        e = current_energy(hub.her.energy, now)
        speakers = {m.get("uid") for m in focus_msgs if m["role"] == "user"}
        self._round_speaker = next(iter(speakers)) if len(speakers) == 1 else ""
        with_tb = bool(hub.trailblazer and speakers == {hub.trailblazer})
        tb_calling = with_tb and any(m.get("called") for m in focus_msgs)
        llm = self.llm  # 这一轮固定同一个会话模型；下轮再读最新设置。
        use_tools = llm.supports("tool_use")
        # 多想几步（原版 2026-10-29）：被叫到、接话窗口、看手机这几轮可以「想一想」、接着「回想」，最多几步跟精力走；
        # 补看、道别不给（和原版补发、道别一样）
        think = mode in ("called", "window", "look")
        steps = think_steps(e["value"]) if think else 1
        # 怎么接由她自己选（回完在「这轮的感受」里报）；群里不索取
        tools = [*([winext.GROUP_THINK] if think else []), RECALL, round_feeling(GROUP_WAYS, group_way_note(GROUP_WAYS)),
                 winext.END_TOPIC] if use_tools else []
        if use_tools and hub.stickers and hub.stickers.can_send():
            tools.append(hub.stickers.tool())
        # 自然无需回复一直可选；主要联系人关闭拒答时只收起 unwilling。
        allow_unwilling = not tb_calling or bool(hub.options().get("decline_trailblazer"))
        if use_tools and mode != "farewell":
            tools.append(winext.no_reply_tool(allow_unwilling, group=True))
        her_texts = [m["text"] for m in self.msgs if m["role"] == "her" and m["text"]]

        style_note = persona.build_style_context(await hub.style.samples_for(recent, 4)) if hub.style else None
        voice_note = persona.build_voice_ref_context(await hub.style.voice_for(recent, 2)) if hub.style else None
        if bg is None:  # 被叫到、补看、道别：没经过判断，现在查
            bg = await self.background(said, now)
        energy_note = "tired-still" if e["value"] < TIRED else None
        streak = asking_streak(self.store, now)
        streak_note = winext.group_asking_streak_context(streak) if streak >= ASKING_STREAK else None

        system = "\n\n".join(s for s in [
            persona.build_system_prompt(self._mood_note(now), "tools" if tools else "plain", identity_line=winext.GROUP_IDENTITY,
                                        energy_note=energy_note),
            winext.group_block(self.name, hub.trailblazer),
            winext.participants_block(self._participants(ctx)),
            winext.threads_block(self._threads(now)),
            await self._history_note(said, ctx, f"{mode}:{focus[-1] if focus else now}", self._round_speaker),
            style_note, voice_note, bg["recollection"], bg["past"], bg["people"], streak_note,
            winext.opener_note(her_texts), winext.time_block(now, group=True),
            activity.current_context(now, state_fn=state_at, allow_query=use_tools),
            activity.messages_query(focus_msgs, now, state_fn=state_at) if not use_tools else None,
            self._context_instruction(), extra,
        ] if s)
        # 问模型前最后看一眼（查背景、挑样例的这阵子）：撤回了她要接的 → 打断；撤回了别的 → 拿掉；她下线了 → 叫她的记成留言
        if self.recalls != recalls0:
            if self._focus_recalled(focus):
                return self._interrupted(undo)
            ctx = self._prune(ctx)
        if self._went_offline(focus):
            self._restore_unread_preparation(undo)
            return mode != "catchup"
        # 原话/旧包沿用本轮准备时的快照。后台此刻成包也不临时删原话，避免包还没进入system就丢掉前文。
        turns = self._turns(ctx, focus={m["id"] for m in focus_msgs} if mode != "farewell" else set(), fresh=fresh)
        if not turns:
            turns = [{"role": "user", "content": "（现在可以按上面的情形接着说。）"}]
        self._record_read(ctx)
        self._capture_shared(ctx)
        hub.events.count("模型调用")
        recall_commits = []
        # 普通新群消息留给下一轮，目标被撤回／她下线了则不再为旧情形重试请求。
        def request_valid():
            return (not self.closed and state_at(now_ms()).online and not self._leaving()
                    and not self._focus_recalled(focus))

        with measure(hub.events, "群回复"):
            if tools:
                r = await llm.chat_with_tools(system, turns, tools, lambda c, last=False: self._tool_result(c, last, recall_commits),
                                                  ends_turn=lambda c: c["name"] in ("send_sticker", "no_reply"),
                                                  reads_result=reads_result, max_reads=steps,
                                                  final=lambda c: c["name"] == "no_reply",
                                                  request_valid=request_valid)
            else:
                r = await llm.chat_with_retry(system, turns, request_valid=request_valid)
        if self.recalls != recalls0 and self._focus_recalled(focus):
            return self._interrupted(undo)  # 模型答的时候她要接的被撤回了：这一轮不发（群里请求不掐，答完不发）
        if r.ok or r.kind == "empty":
            self._confirm_shared(ctx)
            # 她回话时看到过的（记在每条消息上；之后来的才算「还没看到」）
            for m in ctx:
                if m["role"] == "user":
                    m["shown"] = True
            self.store.save_messages()
        if not r.ok:
            if r.kind == "empty":
                if farewell:
                    self._mark_farewell(now_ms())  # 她选了不说：这一段的道别机会也算用过了
                return True  # 空回复 = 她不说话
            hub.events.count("模型失败")
            self._note("出错", error_diagnostic(r), "warn")  # 群里失败就沉默，不刷屏
            return False
        choice = winext.no_reply_choice(r.calls, allow_unwilling)
        if choice is not None:
            label = "无需回复" if choice["kind"] == "unnecessary" else "不想回"
            hub.events.count(label)
            for m in focus_msgs:
                if m["role"] == "user" and not m.get("answered"):
                    m["declined"] = True
                    m["noReplyKind"] = choice["kind"]
            self.store.save_messages()
            who = "、".join(dict.fromkeys(m.get("name") or "某人" for m in focus_msgs if m["role"] == "user"))
            self._note("不回", f"（{mode}）{who}：看过了，{label}（{choice['reason'] or '没说为什么'}）")
            if farewell:
                self._mark_farewell(now_ms())
            if any(c["name"] == "end_topic" for c in r.calls):
                self._close_window()  # 自然结束也可以沉默关窗，不要求先发一句话。
            else:
                self._miss()
            return True
        intent = intent_from_calls(r.calls)
        if intent.get("thoughts"):  # 想一想的内容只给她自己看：只记日志
            self._note("想一想", "｜".join(intent["thoughts"])[:200])
        sticker = next((c["input"].get("emotion") for c in r.calls if c["name"] == "send_sticker"), None)
        ended = any(c["name"] == "end_topic" for c in r.calls)
        parts = to_messages(r.text)
        if not parts and not sticker:
            if farewell:
                self._mark_farewell(now_ms())
            return True

        # 引用：她接的是某个人的话才考虑（接的是大家在聊的话题就不引用）；判断没说的，看要接的是不是同一个人
        target = None
        users = [m for m in focus_msgs if m["role"] == "user"]
        if mode != "farewell" and users and to != "话题" and len({m.get("uid") for m in users}) == 1:
            target = users[-1]
        # ⭐ 先发，真发出去了再结算（记账、方式、精力、心情、说过话、接话窗口、统计）：发不出去就当这轮没说
        self._asked = list({m["id"] for m in [*ctx, *focus_msgs] if m["role"] == "user"})
        cut = lambda: self.recalls != recalls0 and self._focus_recalled(focus)  # noqa: E731
        if farewell:
            self._farewell_edge = until
        self._window_exchange_pending = {"segment": online_since(now_ms() // MIN * MIN), "engaged": engaged}
        self._window_exchange_sending = True
        try:
            said = await self.say(parts, sticker, target, cut=cut)
        finally:
            self._window_exchange_sending = False
            self._window_exchange_pending = None
            self._asked = None
        if not said:
            return self._interrupted(undo) if cut() else False
        now = now_ms()
        if farewell:
            self._mark_farewell(now)  # 道别真发出去了：这一段在线不再道别
        bg["commit"]()  # 她开口了：这次想起的才算想起过
        for commit in recall_commits:
            commit()
        note_way(self.store, intent.get("way"), now)  # 她这轮怎么接的（算「连着几轮在问别人」用）

        # 精力：和主要联系人照原版，和群友消耗多些；聊得实在补一点
        hub.her.energy = apply_round(hub.her.energy, intent.get("feel"), now,
                                     DELTA if with_tb else PEER_DELTA,
                                     0 if with_tb or not intent.get("meaningful") else PEER_REAL_BONUS)
        hub.her.mood = on_emotion(hub.her.mood, intent.get("emotion"), bool(intent.get("meaningful")), now)
        hub.her.save()
        if hub.trailblazer and hub.trailblazer in speakers:
            hub.on_trailblazer_talk("real" if intent.get("meaningful") else "light")
        talked = {}
        for m in focus_msgs:
            if m["role"] == "user" and m.get("uid") and m["uid"] != hub.trailblazer:
                talked[m["uid"]] = m.get("name", "")
        for uid, name in talked.items():  # 一轮里一个人只算说过一次话
            hub.intimacy.touch(uid, name, self.gid)

        # 接话窗口：说完开一个，聊得越热越长；她说「先这样」就不开
        busy = sum(1 for m in self.msgs if m["role"] == "user" and 0 <= now - m["ts"] < 10 * MIN)
        span = WINDOW_MIN[0] + (WINDOW_MIN[1] - WINDOW_MIN[0]) * min(1, busy / 10)
        x["windowUntil"] = 0 if ended or self._leaving() or not state_at(now_ms()).online else now + span * MIN
        x["windowMisses"] = 0
        if not x["windowUntil"]:
            x.pop("windowExchange", None)
        x["spoken"] = True
        self.store.save_state()
        hub.events.count("群里说话")
        way = next((w["label"] for w in GROUP_WAYS if w["id"] == intent.get("way")), "照常回复")
        self._note("回话", f"（{mode}·{way}）" + " / ".join(parts)[:60] + (f"［表情：{sticker}］" if sticker else "")
                   + ("（收住话题）" if ended else ""))
        return True

    def _unshown(self, focus: list[str]) -> bool:
        """要接的那几条里，有没有她回话时还没看到过的（比如她发上一句的时候才来的）"""
        return any(m["id"] in focus and not m.get("shown") for m in self.msgs)

    def _went_offline(self, focus: list[str]) -> bool:
        """准备的这阵子她下线了：叫她的记成留言，上线补看"""
        if state_at(now_ms()).online:
            return False
        self._close_window()
        left = [m for m in self.msgs if m["id"] in focus and m["role"] == "user" and m.get("called")]
        for m in left:
            m["left"] = True
        self.store.save_messages()
        self._note("留言", f"准备回话的时候她下线了，{len(left)} 条叫她的等上线再看")
        return True

    def _restore_unread_preparation(self, undo: list) -> None:
        """等图/查背景时下线，尚未真正交给模型的准备标记退回；判断已读部分仍保留。"""
        for m, key, old in reversed(undo):
            if m["id"] in self._round_read:
                continue
            if old is None:
                m.pop(key, None)
            else:
                m[key] = old
        self.store.save_messages()

    def _mark_answered(self) -> None:
        """她第一条真发出去了：这一轮交给模型的群友消息都算回过（之后撤回不动）"""
        if not self._asked:
            return
        asked, self._asked = set(self._asked), None
        self._round_shared_ok.update((mid, self._round_shared_inputs[mid])
                                     for mid in asked.intersection(self._round_shared_inputs))
        for m in [*self.msgs, *self._archive_context]:
            if m["id"] in asked:
                m["answered"] = True
        self._archive_action(self.history_archive.mark_answered, asked)
        self.store.save_messages()

    def _focus_recalled(self, focus: list[str]) -> bool:
        """她要接的那几条里，有没有被撤回的"""
        alive = {m["id"] for m in [*self.msgs, *self.history_archive.messages, *self._archive_context]
                 if (not m.get("recalled") and m["id"] not in self._recalled_ids) or m.get("answered")}
        return any(f not in alive for f in focus)

    def _prune(self, ctx: list[dict]) -> list[dict]:
        """撤回的是别的（不是她要接的）：只从上下文里拿掉"""
        alive = {m["id"] for m in [*self.msgs, *self.history_archive.messages, *self._archive_context]
                 if (not m.get("recalled") and m["id"] not in self._recalled_ids) or m.get("answered")}
        return [m for m in ctx if m["role"] == "gap" or m["id"] in alive]

    def _interrupted(self, undo: list) -> bool:
        """（用户 2026-10-30 定）她要接的被撤回了：这一轮打断，不回、不重来、不补。
        这一轮翻到的（原来没看过的、被拉进细看的）都改成「翻过去」：算看过（不会再被叫她的那条路拿去回），不进记忆"""
        for m, key, old in undo:
            if key == "read":
                m["skipped"] = True
            elif key == "skipped" and old:
                m["skipped"] = True
        self._archive_action(self.history_archive.remove_ids,
                             [m["id"] for m, _, _ in undo if m.get("skipped") and m["id"] not in self._archive_saved_before],
                             tombstone=False)
        self.store.save_messages()
        self._note("撤回", "她要接的那条被撤回了，这一轮打断，不回了")
        return True

    async def _tool_result(self, c: dict, last: bool = False, commits: list | None = None):
        """群里一轮回传给她的工具结果"""
        name = c["name"]
        if name == "recall":
            schedule_result = activity.call_result(c, now_ms(), state_fn=state_at)
            if schedule_result is not None:
                return schedule_result
            about = str(c["input"].get("about") or "")
            result = await self.hub.recall_all(self.memory, about, self.memory_session())
            # 自动背景统一选五包；主动换关键词回想仍可查本群最多三份相关记事，不回查原话。
            memo = await self.round_memory.recall_note(about, speaker_uid=self._round_speaker)
            if isinstance(result, tuple):
                text, commit = result
                if commits is not None:
                    commits.append(commit)
                return "\n\n".join(x for x in (text, memo) if x)
            return "\n\n".join(x for x in (result, memo) if x)  # 兼容旧只读回调。
        if name == "think":
            return result_for(c, last)
        if name == "send_sticker":
            return "表情发出去了。"
        if name in ("end_topic", "no_reply"):
            return "好。"
        return result_for(c, last)

    def _after(self, m: dict) -> int:
        """这条后面群里又有了几条"""
        idx = next((i for i, x in enumerate(self.msgs) if x["id"] == m["id"]), -1)
        if idx >= 0:
            return len(self.msgs) - 1 - idx
        # 旧焦点可能只剩档案副本；引用距离包含期间未读/翻过的收件，不能只数已读历史。
        merged = {x["id"]: x for x in self.history_archive.messages}
        merged.update((x["id"], x) for x in self.msgs)
        saved_order = {key: i for i, key in enumerate(merged)}
        raw_order = {x["id"]: i for i, x in enumerate(self.msgs)}
        ordered = sorted(merged.values(), key=lambda x: (x.get("ts", 0), x["id"] in raw_order,
                                                          raw_order.get(x["id"], saved_order[x["id"]])))
        idx = next((i for i, x in enumerate(ordered) if x["id"] == m["id"]), -1)
        return len(ordered) - 1 - idx if idx >= 0 else 0

    async def say(self, parts: list[str], sticker: str | None = None, target: dict | None = None, cut=None,
                  offline_notice: bool = False) -> bool:
        """按节奏发。target：她接的那一条。真发的时候再数：那条在最近 5 条以内就不引用，再往前就用 账号 的引用。
        cut()：发第一条之前（打着字的时候）她要接的被撤回了 → 一条都不发。第一条真发出去，这一轮交给模型的就算回过了（_asked）。
        返回有没有发出去东西（一条都没发出去 = False）"""
        if self._leaving() and self._farewell_edge is None and not self._farewell_grace and not offline_notice:
            return False
        hub = self.hub
        self.phase = "发送" if self.in_flight else self.phase
        first = True
        sent = False
        reply_to = None
        for kind, val in pace(parts):
            if kind in ("typing", "false-start", "wait"):
                await asyncio.sleep(val / 1000)
                continue
            if not sent and cut is not None and cut():
                return False
            if first and target and target.get("qid"):
                archived = self.history_archive.lookup_qid(target["qid"])
                recalled = target.get("recalled") or (archived or {}).get("recalled") or target["id"] in self._recalled_ids
                if not recalled and self._after(target) >= QUOTE_AFTER:
                    reply_to = target["qid"]
            if not await hub.sender.send(self.gid, val, reply_to if first else None):
                self._note("出错", "消息没发出去", "warn")
                return sent
            if self._farewell_grace and not offline_notice:
                try:
                    farewell.consume(self.store, now_ms(), DELTA if hub.trailblazer and target and target.get("uid") == hub.trailblazer else PEER_DELTA)
                except OSError:
                    self._note("出错", "收尾已送出，状态暂未写盘；当前群仍保持封口", "warn")
            first = False
            sent = True
            if not offline_notice:
                self._mark_answered()
            self.msgs.append({**self.store.new_msg("her", val), "read": True})
            if not offline_notice:
                self._capture_shared([self.msgs[-1]])
                self._confirm_shared([self.msgs[-1]])
            if self.in_flight:
                self._round_sent.append(self.msgs[-1])
            self.store.save_messages()
            if not offline_notice:
                self._commit_window_exchange()
                self._commit_farewell()
        if sticker and hub.stickers:
            path = hub.stickers.pick(sticker)
            if path and not sent and cut is not None and cut():
                return False
            if path and await hub.sender.send_image(self.gid, path):
                sent = True
                if not offline_notice:
                    self._mark_answered()
                self.msgs.append({**self.store.new_msg("her", ""), "sticker": sticker, "read": True})
                if not offline_notice:
                    self._capture_shared([self.msgs[-1]])
                    self._confirm_shared([self.msgs[-1]])
                if self.in_flight:
                    self._round_sent.append(self.msgs[-1])
                self.store.save_messages()
                if not offline_notice:
                    self._commit_window_exchange()
                    self._commit_farewell()
                hub.events.count("表情")
        return sent

    # ---------- 心跳里的事 ----------

    def _look_ready(self, proactive_on) -> bool:
        return (not self.closed and not self.hub.closed and proactive_on(self.gid)
                and state_at(now_ms()).online and not self.in_flight and not self._leaving()
                and (self.batch_task is None or self.batch_task.done())
                and any(m["role"] == "user" and not m.get("read") and not _trivial(m) for m in self.msgs))

    def _pause_look(self, now: int) -> None:
        self.last_tick = now
        self._look_active = False

    def schedule_look(self, now: int, proactive_on) -> None:
        if not self._look_ready(proactive_on) or (self._look_task is not None and not self._look_task.done()):
            self._pause_look(now)  # 停用、忙碌或空群冻结预算，也不扣次数。
            return
        if self._look_due(now, current_energy(self.hub.her.energy, now)["value"]):
            self._look_task = self.spawn(self.look(proactive_on))

    def _look_due(self, now: int, energy: float) -> bool:
        """每群抽一次平滑预算，可看时才推进；整点和重启均不重新抽。"""
        previous, active = self.last_tick, self._look_active
        self._pause_look(now)
        if energy < TOO_TIRED_TO_ASK:
            return False
        if self.force_look:
            self.force_look = False
            return True
        opts, look = self.hub.options(), self.store.extra["look"]
        block_ms = max(1, int(opts.get("look_block_minutes", LOOK_BLOCK_MIN))) * MIN
        limit = int(opts.get("look_block_max", LOOK_BLOCK_MAX))
        if limit <= 0 or (look.get("block") == now // block_ms and look.get("count", 0) >= limit):
            return False
        per_hour = opts.get("look_per_hour", LOOK_PER_HOUR)
        if not look_wait.rate_per_ms(per_hour, energy < TIRED):
            return False
        self._look_active = True
        cooldown_end = look.get("lastAt", 0) + LOOK_MIN_GAP if look.get("lastAt") else 0
        if now < cooldown_end:
            return False
        dt = max(0, min(now - previous, LOOK_TICK_CAP)) if previous and active else 0
        dt = min(dt, max(0, now - cooldown_end))  # 仅推进跨过冷却线后的那一小段。
        old = self.store.extra.get("lookWait")
        wait = look_wait.advance(old, dt, per_hour, energy < TIRED)
        if wait != old:
            self.store.extra["lookWait"] = wait
            self.store.save_state()
        return wait["remaining"] <= 0

    def _note_look(self, now: int) -> None:
        block_ms = max(1, int(self.hub.options().get("look_block_minutes", LOOK_BLOCK_MIN))) * MIN
        block, look = now // block_ms, self.store.extra["look"]
        self.store.extra["look"] = {"block": block, "count": (look.get("count", 0) if look.get("block") == block else 0) + 1,
                                    "lastAt": now}
        self.store.extra.pop("lookWait", None)  # 只有真正开始阅读才消费到期预算。
        self._pause_look(now)
        self.store.save_state()

    async def look(self, proactive_on=None) -> None:
        """本群独立看、判断和接话，同群整轮占用，其他群不用等。"""
        proactive_on = proactive_on or self.hub.proactive_on
        if not self._look_ready(proactive_on):
            return
        hub, now = self.hub, now_ms()
        energy = current_energy(hub.her.energy, now)
        if energy["value"] < TOO_TIRED_TO_ASK:
            return
        # 领取本群这次机会与共享精力结算之间没有 await，不会覆盖其他会话刚结算的值。
        # 已开始的有效阅读即使模型故障也算尝试，避免故障期间每个心跳重试。
        self._note_look(now)
        hub.her.energy = {**energy, "value": max(0, energy["value"] - 1)}
        hub.her.save()
        hub.events.count("看手机")
        async with self._hold("看手机判断"):
            unread = [m for m in self.msgs if not m.get("read") and m["role"] == "user"]
            pool = [m for m in self.msgs if _seen(m) or m in unread]
            start = max(0, len(pool) - random.randint(*READ_FIRST))
            d = await self.assess(pool, start, "look", pool[start:])
            if d.get("skipped"):
                return
            judged = [m for m in d["read"] if not m.get("called") and not m.get("read")]
            for m in judged:
                m["read"] = True
            current_ids = {m["id"] for m in self.msgs}
            restored = [m for m in d["read"] if m.get("called") and m["id"] not in current_ids
                        and m["id"] not in self._recalled_ids]
            if restored:
                self.msgs[:] = sorted([*restored, *self.msgs], key=lambda m: m["ts"])
            for m in unread:
                if m not in d["read"] and not m.get("called"):
                    m["read"], m["skipped"] = True, True
            self.store.extra["lastLookAt"] = now
            self.store.save_messages()
            self.store.save_state()
            self._record_read(d["read"])
            self._round_remember = d.get("remember", True)
            self._round_reference = d.get("reference", "")
            self._round_selection_key = d.get("selectionKey", "")
            self._note("看手机", f"读了 {len(d['read'])} 条：{'想接' if d['reply'] else '不接'}（{d['reason']}）")
            if d["reply"]:
                self.phase = "回复"
                await self._turn("look", d["focus"], None, d["bg"], d["to"], judged=judged)

    def _schedule_tick(self, now: int) -> None:
        if not self.closed and (self._tick_task is None or self._tick_task.done()):
            self._tick_task = self.spawn(self._tick(now))

    async def _tick(self, now: int) -> None:
        """每群自己的前台心跳：一个群慢，不挡其他群和各自的看手机。"""
        if self.closed or self._leaving() or not state_at(now_ms()).online:
            return
        if not await self.catchup():
            await self.maybe_farewell(now)

    def schedule_memory(self) -> None:
        if not self.closed and not self.mem_busy and (self._memory_task is None or self._memory_task.done()):
            self._memory_task = self.spawn(self.run_memory())

    def schedule_history(self) -> None:
        self._archive_read()
        self._sync_round_segment()
        if self._history_timer is not None:
            self._history_timer.cancel()
            self._history_timer = None
        llm = self.llm
        if (self.closed or self.mem_busy or not getattr(self.hub, "round_llm_for", None)
                or llm is None or llm.provider() is None
                or (self._history_task is not None and not self._history_task.done())):
            return
        due = self.round_memory.next_summary_at()
        if due is None:
            return
        if due <= now_ms():
            self._history_task = self.spawn(self.run_history())
        else:
            # 只挂定时回调，不占前台门闩或后台任务；新读/新发后按最新截止时间重新安排。
            self._history_timer = asyncio.get_running_loop().call_later(
                (due - now_ms()) / 1000, self.schedule_history)

    async def run_history(self) -> bool:
        """连续交谈封口后写一次第一人称记事；新话继续进入下一个片段。"""
        llm = self.llm
        if self.closed or self.mem_busy or llm is None or llm.provider() is None:
            return False
        async def chat(system, messages):
            self.hub.events.count("模型调用")
            with measure(self.hub.events, "每轮记事"):
                result = await llm.chat_with_retry(system, messages)
            if not result.ok:
                self.hub.events.count("模型失败")
            return result
        changed = False
        completed = False
        try:
            while not self.closed and not self.mem_busy and self.round_memory.needs_summary():
                if not await self.round_memory.summarize(chat, vision=llm.supports("image")):
                    break
                changed = True
            completed = True
            return changed
        finally:
            if completed and not self.closed:
                # 回调执行时当前任务已结束；把写记事期间新积累的片段也安排上。
                asyncio.get_running_loop().call_soon(self.schedule_history)

    async def catchup(self) -> bool:
        """上线后补看留言。成了才清掉留言和「回来先回他」；没成（模型失败、没发出去）留着，5 分钟后再试"""
        if self._leaving():
            return False
        left = [m for m in self.msgs if m.get("left")]
        x = self.store.extra
        if not left or self.in_flight or now_ms() - x.get("catchupFailedAt", 0) < CATCHUP_RETRY_MS:
            return False
        now = now_ms()
        since = min(m["ts"] for m in left)
        mid = state_at(int((since + now) / 2))
        promised = bool(x.get("promised"))  # 任务中答应过主要联系人回来再回他
        self._note("补看", f"她上线看到了 {len(left)} 条留言" + ("，先回主要联系人" if promised else ""))
        ok = await self.turn("catchup", [m["id"] for m in left], winext.group_catchup_context(
            "离线期间其中一段的活动（不代表全程，也不是当前状态）：" + describe_activity(mid), promised))
        if ok is None:
            return False
        if ok:
            for m in left:
                m.pop("left", None)
            x.pop("promised", None)
            x.pop("catchupFailedAt", None)
            self.store.save_messages()
        else:
            x["catchupFailedAt"] = now_ms()
            self._note("补看", "没成，留言留着，5 分钟后再试", "warn")
        self.store.save_state()
        return True

    async def maybe_farewell(self, now: int) -> bool:
        if self._leaving() or self._farewell_done(now) or not self._window_engaged(now) or self.in_flight:
            return False
        until = offline_after(now)
        if not until or not (0 < (until - now) / MIN <= FAREWELL_WINDOW_MIN):
            return False
        # 「这一段道过别」等真发出去了才记（turn 里记）：没问成、没发出去，下次心跳还会再试
        await self.turn("farewell", [], persona.build_standalone_farewell_context((until - now) / MIN))
        return True

    def memory_session(self) -> list[dict]:
        """她看过的和她说的"""
        return self._read_history()

    async def run_memory(self, force: bool = False) -> str | None:
        if self.mem_busy:
            return None
        if self.in_flight:
            # 本轮准备时临时标的 read 还可能因撤回/下线退回；只整理已经稳定提交的阅读。
            return "正在回话，请稍后再整理" if force else None
        now = now_ms()
        self.memory.fade(now)  # 变模糊不用模型：照常
        self._sync_round_segment()
        if state_at(now).online and not force:
            return None  # 在线期间只作每轮记事；不因消息/包超过某个数量重复整理长期记忆。
        if not self.llm.supports("tool_use"):
            return None
        if not self.memory.enabled() and not force:
            return None
        if force:
            self._archive_action(self.round_memory.seal_open)
        hist = self.memory_session()
        session = self.memory.pending_session(hist, state_at(now).online and not force, now) \
            if (force or self.memory.can_try(now)) else None
        haze = self.memory.pending_haze() if not session and (force or self.memory.can_try_haze(now)) else None
        tags = not session and not haze and (force or self.memory.can_try_tags(now)) and bool(self.memory.pending_tags())
        if not session and not haze and not tags:
            return "没有要整理的"
        self.mem_busy = True
        try:
            if session:
                closeness = await self.memory.consolidate_peer(self._mem_chat, session, now, f"群「{self.name}」里", self.hub.trailblazer,
                                                               self.hub.intimacy.roster_line,
                                                               summary_dialogue=self.round_memory.memory_note(session), allow_read_only=True)
                if closeness is None:
                    self._note("记忆", "整理失败，10 分钟后再试", "warn")
                    return "整理失败"
                for c in closeness:
                    before = self.hub.intimacy.impression(c["qq"]) if c["qq"] == self.hub.trailblazer else ""
                    d = self.hub.intimacy.apply(c["qq"], c["delta"], c["reason"], c["name"], c.get("impression", ""),
                                                observed_at=c.get("observedAt"))
                    if c["qq"] == self.hub.trailblazer:
                        if self.hub.intimacy.impression(c["qq"]) != before:
                            self._note("印象", "整理这个群时，更新了对主要联系人的印象")
                    else:
                        self._note("亲密度", f"{c['name']} {d:+d}（{c['reason']}）")
                self._note("记忆", f"整理了 {len(session)} 条")
                # 整理进长期记忆的部分，记事就不要了（用户 2026-10-31 定）；原话仍在档案里。
                self._archive_action(self.round_memory.drop_consolidated, self.memory.m.get("doneMsgId"), self.memory.m.get("doneTs", 0))
            elif haze:
                if not await self.memory.merge_haze(self._mem_chat, now, identity=f"你正在回忆在群「{self.name}」里和大家聊过的事。"):
                    return "整理失败"
            elif not await self.memory.tag_old(self._mem_chat, now):
                return "整理失败"
            await self.memory.refresh_vectors()
            return "整理好了"
        finally:
            self.mem_busy = False
            if not self.closed:
                self.schedule_history()

    def _track(self, task: asyncio.Task | None) -> None:
        if task is not None:
            self._running.add(task)
            task.add_done_callback(self._running.discard)
            task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._note("出错", f"群后台任务失败：{type(task.exception()).__name__}", "warn")

    def spawn(self, coro) -> asyncio.Task | None:
        if self.closed:
            coro.close()
            return None
        task = asyncio.get_running_loop().create_task(coro)
        self._track(task)
        return task

    def shutdown(self) -> None:
        """卸载插件：先关上（什么都不再接、不再排、不再记），再停掉正在跑的"""
        self.closed = True
        if self._history_timer is not None:
            self._history_timer.cancel()
            self._history_timer = None
        for t in (self.batch_task, self.window_task, *self._running):
            if t and not t.done():
                t.cancel()


class GroupHub:
    """所有群 + 看手机。chats 由插件按名单建好放进来。"""

    def __init__(self, her, llm, judge_llm, knowledge, people, style, stickers, intimacy, sender, events,
                 trailblazer: str, options, group_name, on_trailblazer_talk, recall_all, proactive_on=None):
        self.her = her
        self.llm = llm
        self.judge_llm = judge_llm
        self.knowledge = knowledge
        self.people = people
        self.style = style
        self.stickers = stickers
        self.intimacy = intimacy
        self.sender = sender
        self.events = events
        self.trailblazer = trailblazer
        self.options = options
        self.group_name = group_name
        self.on_trailblazer_talk = on_trailblazer_talk
        self._recall_all = recall_all
        self.proactive_on = proactive_on or (lambda gid: True)  # 这个群「主动看」开没开（看手机、接话窗口都看它）
        self.chats: dict[str, GroupChat] = {}
        self.closed = False
        self.captions = None  # 看不了图时先转述（caption.py）：async (gid, msgs, saved)，插件放进来

    async def recall_all(self, memory, about: str, msgs: list[dict]) -> str:
        return await self._recall_all(memory, about, msgs)

    async def mem_chat(self, system, messages, tools=None, tool_choice="auto"):
        self.events.count("模型调用")
        return await self.llm.chat_with_retry(system, messages, tools, tool_choice)

    async def heartbeat(self, proactive_on, enabled_on=None) -> None:
        if self.closed:
            return
        now = now_ms()
        her = state_at(now)
        for gid, chat in list(self.chats.items()):
            if enabled_on is not None and not enabled_on(gid):
                chat._pause_look(now)
                continue  # 这个群在仪表盘里关掉了
            if her.online:
                chat._schedule_tick(now)
                chat.schedule_look(now, proactive_on)
            else:
                chat._pause_look(now)
            chat._sync_round_segment()
            chat.schedule_memory()
            chat.schedule_history()
        await asyncio.sleep(0)  # 调度器不等待任何群的模型、图片或发送。

    async def look(self, proactive_on) -> None:
        """显式看一眼：各群同时执行独立阅读，不比较兴趣，也不共享次数。"""
        await asyncio.gather(*(chat.look(proactive_on) for chat in list(self.chats.values())))

    def shutdown(self) -> None:
        self.closed = True
        for chat in self.chats.values():
            chat.shutdown()
