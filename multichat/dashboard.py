"""
平台无关的仪表盘视图。宿主通过 DashboardService 或自身页面桥调用。

只是后台看：心情、精力、亲密度这些数字只出现在这里，永远不进聊天。
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from .energy import energy_band
from .intimacy import band_of
from .jsutil import MIN, local_dt, now_ms
from .memory import LEVEL_NAME, active_facts, open_threads, thread_expires_at
from .mood import band_of as mood_band, settle as settle_mood
from .profile import bot_name, primary_name
from .schedule import describe_activity, offline_after, online_since, state_at

ACTIVITY_NAME = {"sleep": "休息", "duty": "在线", "downtime": "在线", "night": "在线", "mission": "忙碌中"}
MOOD_NAME = {"warm": "心情轻快", "normal": "平静", "cool": "有些低落", "distant": "情绪低沉"}
ENERGY_NAME = {"fresh": "有精神", "low": "有点乏", "tired": "累了"}
MERGED = {"astrbot_plugin_recall_cancel": "撤回取消回复", "memes": "表情包",
          "time_prompt": "时间观念", "astrbot_plugin_splitter": "对话分段", "astrbot_plugin_favourpro": "好感度（FavourPro）"}
SELF_SENDING = {"astrbot_plugin_proactive_chat": "主动聊天", "astrbot_plugin_reread": "复读",
                "astrbot_plugin_initiativedialogue_for_group": "群聊主动对话", "astrbot_plugin_heartflow": "心流",
                "spectrecore": "SpectreCore"}


def _ts(ms: int | None) -> str:
    return local_dt(ms).strftime("%m-%d %H:%M") if ms else ""


def _status_key(state) -> str:
    """在线优先；忙碌状态与休息状态分别展示。"""
    return "online" if state.online else ("mission" if state.activity == "mission" else "rest")


def timeline_today(now: int) -> list[list]:
    """今天的在线时间段：[[开始分钟, 结束分钟, 在干嘛], ...]（按分钟取样）"""
    d = local_dt(now)
    start = int(datetime(d.year, d.month, d.day).timestamp() * 1000)
    out: list[list] = []
    for i in range(0, 1440, 5):
        s = state_at(start + i * MIN)
        key = s.activity if s.online else "offline_" + _status_key(s)
        if out and out[-1][2] == key:
            out[-1][1] = i + 5
        else:
            out.append([i, i + 5, key])
    return out


def overview(p) -> dict:
    now = now_ms()
    her = state_at(now)
    until, since = offline_after(now), online_since(now)
    e = p.engines.get(p.trailblazer)
    mood = settle_mood(p.her.mood, now)["value"] if getattr(p.her, "mood", None) else None
    from .energy import current_energy
    energy = current_energy(p.her.energy, now)["value"]
    thought = e.store.thought if e else None
    status_key = _status_key(her)
    status = {"online": "在线", "mission": "忙碌中", "rest": "休息"}[status_key]
    chat_p = p.chat_provider(p.trailblazer_umo())
    judge_p = p.judge_llm_for(p.trailblazer_umo()).provider() if hasattr(p, "judge_llm_for") else p.judge_provider()
    version = {}
    vpath = os.path.join(p.replica_dir(), "版本.json")
    if os.path.isfile(vpath):
        try:
            with open(vpath, encoding="utf-8") as f:
                version = json.load(f)
        except (OSError, ValueError):
            pass
    return {
        "her": {
            "name": bot_name(),
            "status": status, "statusKey": status_key, "online": her.online,
            "activity": ACTIVITY_NAME.get(her.activity, her.activity),
            "detail": describe_activity(her), "herHour": f"{int(her.her_hour)}:{int(her.her_hour % 1 * 60):02d}",
            "destination": her.destination, "onlineSince": _ts(since), "offlineAt": _ts(until),
            "nextOnline": _ts(_next_online(now)) if not her.online else "",
            "timeline": timeline_today(now), "nowMinute": local_dt(now).hour * 60 + local_dt(now).minute,
            "mood": None if mood is None else round(mood, 1), "moodBand": MOOD_NAME.get(mood_band(mood)) if mood is not None else "",
            "energy": round(energy, 1), "energyBand": ENERGY_NAME[energy_band(energy)],
            "thought": None if not thought else {
                "topic": thought["topic"] or ("（意犹未尽）" if thought.get("planted") else ""),
                "kind": "追问" if thought.get("curious") else "意犹未尽" if thought.get("planted") else "主动念头",
                "due": _ts(int(thought["dueAt"]))},
        },
        "model": {
            "chat": _provider_name(chat_p), "judge": _provider_name(judge_p),
            "judgeIsChat": chat_p is not None and (chat_p is judge_p or bool(
                (getattr(chat_p, "provider_config", {}) or {}).get("id") and
                (getattr(chat_p, "provider_config", {}) or {}).get("id") ==
                (getattr(judge_p, "provider_config", {}) or {}).get("id"))),
            "tools": p.llm_for(p.trailblazer_umo()).supports("tool_use"), "image": p.llm_for(p.trailblazer_umo()).supports("image"),
            "caption": _caption_name(p),
            "summary": "每个群用自己的主模型写记事（不需要另选模型）",
            "vectors": _vectors_name(p),
            "data": p.data_dir(), "version": version.get("exportedAt", ""),
        },
        "stats": p.events.today(),
        "statsTotal": _stats_total_view(p.events),
        "toolResults": _tool_results_view(p.events),
        "stickers": {k: len(v) for k, v in p.stickers.library().items()} if p.stickers else {},
        "stickerDir": p.stickers.root if p.stickers else "",
        "events": [{**x, "time": local_dt(x["ts"]).strftime("%H:%M:%S")} for x in list(p.events.items)[:100]],
        "env": env_check(p),
        "unfinished": unfinished(p),
    }


def _tool_results_view(events) -> dict:
    snapshot = getattr(events, "tool_results_snapshot", None)
    data = snapshot() if callable(snapshot) else {"since": 0, "today": {}, "total": {}}
    try:
        since_text = local_dt(data["since"]).strftime("%Y-%m-%d %H:%M") if data["since"] else ""
    except (OverflowError, OSError, ValueError):
        since_text = "未知时间"
    view = {**data, "sinceText": since_text}
    details_fn = getattr(events, "tool_details_snapshot", None)
    details = details_fn() if callable(details_fn) else None
    if isinstance(details, dict):
        view["details"] = {**details, "sinceText": _since_text(details.get("since"))}
    models_fn = getattr(events, "tool_models_snapshot", None)
    if not callable(models_fn):
        return view
    models = models_fn()
    view["models"] = {**models, "sinceText": _since_text(models.get("since")),
                      "note": "按任务结束日期统计；无需补交成功也可能经过网络重试。旧记录没有模型归属，不回填到图中；多个模型参与的同一任务单独列出。"}
    if isinstance(details, dict):
        detail_models = details.get("models")
        view["models"]["details"] = {**(detail_models if isinstance(detail_models, dict) else {}),
                                     "since": details.get("since", 0),
                                     "sinceText": _since_text(details.get("since"))}
    return view


def _since_text(since) -> str:
    try:
        return local_dt(since).strftime("%Y-%m-%d %H:%M") if since else ""
    except (OverflowError, OSError, ValueError, TypeError):
        return "未知时间"


def _stats_total_view(events) -> dict:
    snapshot = getattr(events, "total_snapshot", None)
    data = snapshot() if callable(snapshot) else {"since": 0, "importedDay": "", "counts": {}, "modelUsage": {}}
    return {**data, "sinceText": _since_text(data.get("since")),
            "note": "累计包含启用累计统计时仍保留的当日数据及之后的记录；更早的每日数据已清零，无法补回。"
                    if data.get("importedDay") else "累计从启用累计统计后开始保存；更早的每日数据无法补回。"}


def unfinished(p) -> list[dict]:
    """做到一半、进程重启、宿主重载就会停掉、不会接着做完的事（内存中的任务不自动续传，
    在仪表盘上说清楚）。存在数据目录里、重启后会接着做的（留言、私聊里角色还没看到的话、念头）不列"""
    out = []

    def add(scope: str, text: str) -> None:
        out.append({"scope": p.scope_label(scope) if scope else "", "text": text})

    for qq, e in list(getattr(p, "engines", {}).items()):
        scope = getattr(e, "scope", "") or f"p:{qq}"
        if getattr(e, "in_flight", False):
            add(scope, "正在发送离线状态通知" if getattr(e, "_offline_notice_task", None) is not None else
                "正在补发留言" if getattr(e, "catching_up", False) else
                "正在回话（发到一半的，没发的那几句不会再发）")
        if getattr(e, "mem_busy", False):
            add(scope, "正在整理记忆（整理到一半的不会存）")
    hub = getattr(p, "hub", None)
    for gid, c in list(getattr(hub, "chats", {}).items()) if hub else []:
        scope = f"g:{gid}"
        if c.in_flight:
            add(scope, getattr(c, "phase", "") or "正在回话，或者在判断要不要接")
        elif c.batch_task is not None and not c.batch_task.done():
            add(scope, "排着一轮（叫角色的、接话窗口里的）")
        called = [m for m in c.msgs if m["role"] == "user" and m.get("called") and not m.get("read") and not m.get("left")]
        if called:
            add(scope, f"有 {len(called)} 条叫角色的还没回（重启以后不会自动回）")
        if getattr(c, "mem_busy", False):
            add(scope, "正在整理记忆（整理到一半的不会存）")
    cap = getattr(p, "captioner", None)
    n = len(getattr(cap, "_tasks", {}) or {}) if cap else 0
    if n:
        add("", f"{n} 张图正在转述（重启以后，要看的时候会重新转）")
    q = getattr(p, "_receiving", 0) or 0
    if q:
        add("", f"{q} 条消息正在收（图在下载），还没交给角色")
    return out


def _next_online(now: int) -> int | None:
    """下次上线的那一分钟：一分钟一分钟往后找（和 group._next_online_at 一样，不会跳过不到一小时的在线）"""
    t = now // MIN * MIN + MIN
    for _ in range(48 * 60):
        if state_at(t).online:
            return t
        t += MIN
    return None


def _caption_name(p) -> str:
    """对话模型看不了图时，宿主注入的转述模型"""
    llm = p.llm_for(p.trailblazer_umo())
    if llm.supports("image"):
        return ""
    prov = p.caption_provider(p.trailblazer_umo(), llm)
    return _provider_name(prov) if prov is not None else "没设（由宿主提供 caption_provider_for）"


def _provider_name(prov) -> str:
    if prov is None:
        return "（没有可用的模型）"
    cfg = getattr(prov, "provider_config", {}) or {}
    return f"{cfg.get('id', '?')}（{cfg.get('model') or (cfg.get('model_config') or {}).get('model', '')}）"


def _vectors_name(p) -> str:
    if not p.embedder.ready:
        return "向量服务未就绪：知识/记忆/风格按字面，人物按名字与文件顺序，额外口吻参考不补充"
    modes = []
    for attr, label, fallback in (("knowledge", "知识", "字面"), ("people", "人物", "名字与文件顺序"),
                                  ("style", "风格", "字面；额外口吻参考不补充")):
        ready = bool(getattr(getattr(p, attr, None), "vector_ready", False))
        modes.append(f"{label}：{'向量可用' if ready else fallback}")
    return "向量服务已就绪；" + "；".join(modes)


def env_check(p) -> list[dict]:
    out = []
    s = p.sessions
    if not p.trailblazer:
        out.append({"level": "info", "text": "还没设置主要联系人账号 ID：这是可选项，可在框架配置中填写；其他受管私聊和群聊仍可单独添加。"})
    if hasattr(p, "context"):
        if not s.whitelist_on:
            out.append({"level": "info", "text": "AstrBot 没开白名单：名单只存在插件里，增删会话只改插件这边。"})
        try:
            cfg = p.context.get_config()
            ps = cfg.get("platform_settings", {}) or {}
            wl = [str(x).strip() for x in ps.get("id_whitelist", [])]
            bypass = ps.get("wl_ignore_admin_on_friend", False) and p.trailblazer in [str(x) for x in cfg.get("admins_id", [])]
            if p.trailblazer and ps.get("enable_id_white_list") and wl and p.trailblazer_umo() not in wl and not bypass:
                out.append({"level": "warn", "text": f"主要联系人 {primary_name()}（{p.trailblazer}）的当前私聊地址不在 AstrBot 白名单里，也没有管理员豁免：消息会被 AstrBot 拦下。"})
        except (AttributeError, TypeError):
            pass
        try:
            stars = {st.name: st for st in p.context.get_all_stars()}
        except Exception:  # noqa: BLE001
            stars = {}
        for name, label in MERGED.items():
            st = stars.get(name)
            if st is not None and getattr(st, "activated", True):
                out.append({"level": "warn", "text": f"「{label}」插件（{name}）已经合并进来了，请在 AstrBot 里停用它，不然会两边都动。"})
        for name, label in SELF_SENDING.items():
            st = stars.get(name)
            if st is not None and getattr(st, "activated", True):
                out.append({"level": "warn", "text": f"「{label}」插件（{name}）会自己往外发消息，插件拦不住；名单里的会话请在它那边排除，或者停用它。"})
    else:
        out.append({"level": "info", "text": "平台登录、收发权限和会话白名单由宿主适配器配置；框架会话名单不替代平台授权。"})
    if not p.embedder.ready:
        out.append({"level": "info", "text": "当前使用字面检索，样例资料可正常使用。语义检索是可选能力：使用内置向量适配器时再配置 Node.js 和匹配模型文件；也可由宿主提供向量服务。"})
    for qq, d in s.private.items():
        if d.get("listed") and d.get("enabled"):
            llm = p.llm_for(p.private_umo(qq))
            if llm.provider() is None:
                out.append({"level": "warn", "text": f"私聊 {qq} 没有可用的模型，请检查宿主的模型选择器。"})
            elif not llm.supports("tool_use"):
                out.append({"level": "warn", "text": f"私聊 {qq} 用的模型没勾「工具调用」：没有念头、追问、记忆。"})
    if not out:
        out.append({"level": "ok", "text": "没发现会和角色抢话、或者会拦住消息的设置。"})
    return out


def sessions_view(p) -> list[dict]:
    rows = []
    s = p.sessions
    for qq, d in s.private.items():
        if not d.get("listed"):
            continue
        st = p.stores.get(f"p:{qq}")
        last = next((m for m in reversed(st.messages) if not m.get("system")), None) if st else None
        rows.append({"kind": "private", "id": qq, "name": d.get("name") or (primary_name() if qq == p.trailblazer else qq),
                     "trailblazer": qq == p.trailblazer, "enabled": d.get("enabled", True), "memory": d.get("memory", False),
                     "last": _ts(last["ts"]) if last else ""})
    for gid, d in s.group.items():
        if not d.get("listed"):
            continue
        chat = p.hub.chats.get(gid) if p.hub else None
        her_last = next((m for m in reversed(chat.msgs) if m["role"] == "her"), None) if chat else None
        look = chat.store.extra.get("look", {}) if chat else {}
        opts = p.hub.options() if p.hub and callable(getattr(p.hub, "options", None)) else {}
        look_block = max(1, int(opts.get("look_block_minutes", 60))) * MIN
        rows.append({"kind": "group", "id": gid, "name": d.get("name") or gid, "enabled": d.get("enabled", True),
                     "proactive": d.get("proactive", False), "memory": d.get("memory", False),
                     "last": _ts(her_last["ts"]) if her_last else "", "lastLook": _ts(chat.store.extra.get("lastLookAt")) if chat else "",
                     "lookCount": look.get("count", 0) if look.get("block") == now_ms() // look_block else 0,
                     "lookMax": int(opts.get("look_block_max", 2)),
                     "history": chat.history_archive.stats() if chat and getattr(chat, "history_archive", None) else None,
                     "rounds": chat.round_memory.stats() if chat and getattr(chat, "round_memory", None) else None,
                     "summaryModel": _provider_name(p.chat_provider(p.group_umo(gid)))
                     if hasattr(p, "chat_provider") and hasattr(p, "group_umo") else ""})
    return rows


def memory_view(p, scope: str) -> dict:
    mem = p.memories.get(scope)
    if mem is None:
        return {"scopes": scopes(p), "scope": scope, "empty": True}
    now = now_ms()
    expire = getattr(mem, "expire_low", None)
    if callable(expire):
        expire(now)
    m = mem.m
    return {
        "scopes": scopes(p), "scope": scope, "enabled": mem.enabled(),
        "episodes": [{"id": e["id"], "level": LEVEL_NAME[e["level"]], "when": _ts(e["start"]), "summary": e["summary"],
                      "gist": e["gist"], "feeling": e["feeling"], "weight": e["weight"], "topics": e.get("topics") or []}
                     for e in sorted(m["episodes"], key=lambda e: -e["start"])],
        "words": [{"id": w["id"], "text": w["text"], "about": w["about"], "when": _ts(w["ts"])} for w in reversed(m["words"])],
        "facts": [{"id": f["id"], "category": f["category"], "content": f["content"],
                   "who": f"{f.get('subjectName') or f['subject']}（账号 {f['subject']}）" if f.get("subject") else f.get("subjectName", ""),
                   "when": _ts(f["updatedAt"])} for f in sorted(active_facts(m), key=lambda f: -f["updatedAt"])],
        "threads": [{"id": t["id"], "content": t["content"], "when": _ts(t["createdAt"]),
                     "expiresAt": thread_expires_at(t), "expires": _ts(thread_expires_at(t)),
                     "kind": t.get("kind", ""), "reason": t.get("reason", "")}
                    for t in open_threads(m, now)],
        "haze": [{"id": h["id"], "month": h["month"], "text": h["text"]} for h in m["haze"]],
        "pending": len(mem.unconsolidated(p.memory_history(scope))),
    }


def scopes(p) -> list[dict]:
    out = []
    for scope in p.memories:
        kind, sid = scope.split(":", 1)
        out.append({"scope": scope, "label": p.scope_label(scope)})
    return out


def retire(p, scope: str, kind: str, item_id: int) -> bool:
    mem = p.memories.get(scope)
    if mem is None:
        return False
    m, now = mem.m, now_ms()
    if kind == "fact":
        target = next((f for f in m["facts"] if f["id"] == item_id), None)
        if target:
            target["retiredAt"] = now
    elif kind == "thread":
        target = next((t for t in m["threads"] if t["id"] == item_id), None)
        if target:
            target["closedAt"] = now
    elif kind in ("episode", "word"):
        key = "episodes" if kind == "episode" else "words"
        before = len(m[key])
        m[key] = [x for x in m[key] if x["id"] != item_id]
        target = before != len(m[key])
    else:
        return False
    mem.store.save_memory()
    return bool(target)


def intimacy_view(p) -> list[dict]:
    rows = []
    for qq in list(p.intimacy.people):
        x = p.intimacy.get(qq)
        rows.append({"qq": qq, "name": x["name"], "value": x["value"], "band": band_of(x["value"]),
                     "groups": [p.sessions.group.get(g, {}).get("name") or g for g in x.get("groups", [])],
                     "lastDelta": x.get("lastDelta", 0), "lastReason": x.get("lastReason", ""), "talkedAt": _ts(x.get("talkedAt")),
                     "impression": x.get("impression", "")})
    return sorted(rows, key=lambda r: -r["value"])
