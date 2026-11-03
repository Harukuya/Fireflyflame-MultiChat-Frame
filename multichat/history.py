"""每个会话自己的阅读档案：原话留在磁盘，相关时再查确切原话。

群聊每轮记事在 rounds.py；v0.2.17 的小模型摘要已删除（用户 2026-10-31 定），这里只存原话。

这不是长期记忆提炼，不修改亲密度，也不把送达/已读等运行标记放进提示词。
调用方只交她确实看过的消息（群里 read 且未 skipped）和她实际说过的话。
sync 是增量归档：聊天缓存裁短、窗口关闭都不会删档案；已读旧话撤回用 mark_recalled 保留。
"""

from __future__ import annotations

from . import profile

import copy
import json
import math
import os
import re
import uuid

from .jsutil import format_timestamp
from .text import tokens

RECENT = 30
MAX_UNPACK_CHARS = 6000
# 撤回后的已读历史保留来源说明，不虚构平台转发行为。
RECALLED_SOURCE_NOTE = "这是此前已读并保存的内容，之后发生过撤回。不要把它当作新发来的消息；被问到来源时说明是此前已读的记录。"

_MESSAGE_KEYS = ("id", "role", "text", "ts", "uid", "name", "sticker")
_ATTACHMENT_KEYS = ("kind", "name", "file", "mediaType", "caption")
_GENERIC_QUERY = re.compile(r"回想|想起来|记不记得|还记得|记得|原话|原文|以前|之前|我们聊过|聊过|说过|什么|怎样|怎么|的事")


def _clean_message(message: dict) -> dict | None:
    if not isinstance(message, dict) or message.get("role") not in ("user", "her"):
        return None
    if message.get("system") or message.get("skipped"):
        return None
    if message.get("role") == "user" and message.get("read") is False:
        return None
    if not isinstance(message.get("id"), str) or not message["id"]:
        return None
    row = {k: copy.deepcopy(message[k]) for k in _MESSAGE_KEYS if k in message}
    row["text"] = row.get("text") if isinstance(row.get("text"), str) else ""
    stamp = row.get("ts")
    row["ts"] = int(stamp) if isinstance(stamp, (int, float)) and math.isfinite(stamp) else 0
    for key in ("uid", "name", "sticker"):
        if key in row and not isinstance(row[key], str):
            row.pop(key)
    if row.get("uid"):
        row["uid"] = row["uid"][:64]
    attachment = message.get("attachment")
    if isinstance(attachment, dict):
        row["attachment"] = {k: attachment[k] for k in _ATTACHMENT_KEYS if isinstance(attachment.get(k), str)}
    return row


def _text(row: dict) -> str:
    attachment = row.get("attachment") or {}
    note = ""
    if attachment.get("kind") == "image":
        note = f"［发了一张图片：{attachment['caption']}］" if attachment.get("caption") else "［发了一张图片］"
    elif attachment:
        note = f"［发了一个文件：{attachment.get('name') or '文件'}］"
    if row.get("sticker"):
        note += f"［发了一个表情：{row['sticker']}］"
    return note + row.get("text", "")


def _speaker(row: dict) -> str:
    if row["role"] == "her":
        return profile.bot_name()
    return (row.get("name") or row.get("uid") or "对方").replace("\n", " ")[:80]


def _heading(row: dict) -> str:
    stamp = format_timestamp(row.get("ts"))
    speaker = _speaker(row)
    if row["role"] == "user" and row.get("uid") and row.get("name"):
        speaker += f"（账号 {row['uid']}）"
    return f"{stamp} {speaker}：".lstrip()


def _line(row: dict) -> str:
    return _heading(row) + _text(row)


def _query_tokens(query: str) -> dict[str, float]:
    return tokens(_GENERIC_QUERY.sub("", str(query or "")))


def _score(query: str, query_tokens: dict[str, float], text: str) -> float:
    if not query_tokens:
        return 0.0
    target = tokens(text)
    strong = [k for k in query_tokens if len(k) >= 2 and k in target]
    # 单个汉字的偶然重合不算相关；只问一个具体字时允许单字匹配。
    if not strong and not (len(query_tokens) == 1 and next(iter(query_tokens)) in target):
        return 0.0
    overlap = sum(weight for key, weight in query_tokens.items() if key in target)
    return overlap / max(1.0, sum(query_tokens.values()))


def _excerpt(text: str, limit: int, query: str = "") -> str:
    """仅截取确切原文；长消息优先保留命中位置，省略处明确标出。"""
    if len(text) <= limit:
        return text
    if limit < 8:
        return text[:max(0, limit)]
    positions = [text.find(term) for term in _query_tokens(query) if len(term) >= 2 and term in text]
    if positions:
        hit = min(positions)
        start = max(0, min(hit - limit // 3, len(text) - (limit - 4)))
        return ("……" if start else "") + text[start:start + limit - 4] + "……"
    half = (limit - 6) // 2
    return text[:half] + "\n（省略）\n" + text[-half:]


def _excerpt_line(row: dict, limit: int, query: str = "") -> str:
    # 从超长话的中间找原句时，人名/账号 仍必须在，不能跟着前文一起截掉。
    heading = _heading(row)
    if len(heading) >= limit:
        return heading[:max(0, limit)]
    return heading + _excerpt(_text(row), max(0, limit - len(heading)), query)


class History:
    def __init__(self, store, source: str = "本会话"):
        self.store = store
        self.source = str(source or "本会话")[:120]
        self.path = os.path.join(store.root, "history.json")
        self._rows: dict[str, dict] = {}
        self._meta: dict[str, dict] = {}  # 路由/记忆需要的标记；不交给记事或回想。
        self._removed: set[str] = set()
        self._dirty = False
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        removed = data.get("removed", [])
        self._removed = {x for x in removed if isinstance(x, str)} if isinstance(removed, list) else set()
        rows = data.get("messages", [])
        for raw in rows if isinstance(rows, list) else []:
            row = _clean_message(raw)
            if row and row["id"] not in self._removed:
                self._rows[row["id"]] = row
                if raw.get("recalled"):
                    self._meta[row["id"]] = {"recalled": True}
        meta = data.get("meta", {})
        if isinstance(meta, dict):
            for key, value in meta.items():
                if key in self._rows and isinstance(value, dict):
                    self._meta.setdefault(key, {}).update({flag: True for flag in ("answered", "recalled", "declined", "shown")
                                                          if value.get(flag) is True})
                    if value.get("declined") is True and value.get("noReplyKind") in ("unnecessary", "unwilling"):
                        self._meta[key]["noReplyKind"] = value["noReplyKind"]
                    if isinstance(value.get("qid"), (str, int)) and not isinstance(value["qid"], bool):
                        self._meta[key]["qid"] = str(value["qid"])

    @property
    def messages(self) -> list[dict]:
        """有序原文副本；路由和记忆标记供调用方使用，格式化提示词不会读这些字段。"""
        return [copy.deepcopy({**row, **self._meta.get(row["id"], {})}) for row in self._ordered()]

    def _ordered(self) -> list[dict]:
        # 同毫秒按已知的原会话顺序；晚读的早到消息由 sync 的有序批次纠正，不能按随机 id 排。
        return sorted(self._rows.values(), key=lambda row: row["ts"])

    def _order_batch(self, batch: dict[str, None]) -> None:
        """批次的首次 ID 出现给出同毫秒的相对顺序；只改排序，不改内容。

        只同步一条转述/标记没有相对顺序信息，保持原位置。确实需要纠正时，同毫秒仅在档案
        中的消息先留在前面，再按批次原序放其余消息。raw 后面重复的 active_context 不改顺序。
        """
        wanted: dict[int, list[str]] = {}
        for key in batch:
            wanted.setdefault(self._rows[key]["ts"], []).append(key)
        groups: dict[int, list[dict]] = {}
        for row in self._ordered():
            groups.setdefault(row["ts"], []).append(row)
        changed = False
        for stamp, keys in wanted.items():
            if len(keys) < 2:
                continue
            key_set = set(keys)
            group = groups[stamp]
            if [row["id"] for row in group if row["id"] in key_set] == keys:
                continue
            groups[stamp] = [row for row in group if row["id"] not in key_set] + [self._rows[key] for key in keys]
            changed = True
        if changed:
            self._rows = {row["id"]: row for group in groups.values() for row in group}
            self._dirty = True

    def _save(self) -> None:
        os.makedirs(self.store.root, exist_ok=True)
        temporary = f"{self.path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "messages": self._ordered(),
                           "meta": self._meta, "removed": sorted(self._removed)},
                          handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, self.path)
            self._dirty = False
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def sync(self, messages) -> int:
        """增量拷贝、按 id 去重；补好的图片转述会更新原档案。"""
        changed: set[str] = set()
        batch: dict[str, None] = {}
        for raw in messages:
            row = _clean_message(raw)
            if not row or row["id"] in self._removed:
                continue
            key = row["id"]
            if raw.get("recalled") and key not in self._rows:
                continue  # 撤回本身不能让尚未细看的新话进入档案。
            batch.setdefault(key, None)
            metadata = dict(self._meta.get(key, {}))
            if isinstance(raw.get("qid"), (str, int)) and not isinstance(raw["qid"], bool):
                metadata["qid"] = str(raw["qid"])
            for flag in ("answered", "recalled", "declined", "shown"):
                if raw.get(flag):
                    metadata[flag] = True
            if raw.get("declined") and raw.get("noReplyKind") in ("unnecessary", "unwilling"):
                metadata["noReplyKind"] = raw["noReplyKind"]
            if metadata != self._meta.get(key, {}):
                self._meta[key] = metadata
                self._dirty = True
            if self._rows.get(row["id"]) != row:
                self._rows[row["id"]] = row
                changed.add(row["id"])
        if changed:
            self._dirty = True
        self._order_batch(batch)
        if self._dirty:
            self._save()
        return len(changed)

    def remove_ids(self, ids, *, tombstone: bool = True) -> int:
        """显式剔除无效档案；阅读回滚用 tombstone=False。真实撤回已读旧话改用 mark_recalled。"""
        ids = {key for key in ids if isinstance(key, str)}
        count = sum(key in self._rows for key in ids)
        changed = bool(ids - self._removed) if tombstone else bool(count)
        if tombstone:
            self._removed.update(ids)
        for key in ids:
            self._rows.pop(key, None)
            self._meta.pop(key, None)
        if changed or count:
            self._dirty = True
        if self._dirty:
            self._save()
        return count

    def lookup_qid(self, qid) -> dict | None:
        """群收件缓存裁掉以后，仍可找到那条已读消息是否真的回过。"""
        rows = self.lookup_qid_all(qid)
        return rows[0] if rows else None

    def lookup_qid_all(self, qid) -> list[dict]:
        """一条平台消息可能带多张图，对应多个内部记录。"""
        if qid is None:
            return []
        rows = []
        for key, value in self._meta.items():
            if value.get("qid") == str(qid) and key in self._rows:
                rows.append({**self._rows[key], "qid": str(qid), "answered": value.get("answered", False)})
        return rows

    def mark_answered(self, ids) -> None:
        for key in ids:
            if key in self._rows and not self._meta.get(key, {}).get("answered"):
                self._meta.setdefault(key, {})["answered"] = True
                self._dirty = True
        if self._dirty:
            self._save()

    def mark_recalled(self, ids) -> int:
        """用户允许回查已经读过的撤回旧话；只标已有档案，不替未读消息补归档。"""
        count = 0
        for key in ids:
            if key in self._rows and not self._meta.get(key, {}).get("recalled"):
                self._meta.setdefault(key, {})["recalled"] = True
                self._dirty = True
                count += 1
        if self._dirty:
            self._save()
        return count

    def source_note(self, messages) -> str:
        """仅本次用到已读撤回旧话时，带一句角色内来源说明，不逐条重复标记。"""
        if any(row.get("recalled") or self._meta.get(row.get("id"), {}).get("recalled") for row in messages):
            return RECALLED_SOURCE_NOTE
        return ""

    def stats(self) -> dict:
        return {"messages": len(self._rows)}

    def unpack(self, about: str, max_messages: int = 30) -> str:
        """按话题取真实原话（最多三十条/六千字）；原话不经模型重写。"""
        query_tokens = _query_tokens(about)
        if not query_tokens:
            return ""
        try:
            count = max(0, min(RECENT, int(max_messages)))
        except (TypeError, ValueError, OverflowError):
            count = RECENT
        if not count:
            return ""
        rows = self._ordered()
        ranked = [(_score(about, query_tokens, f"{_speaker(row)} {_text(row)}"), index)
                  for index, row in enumerate(rows)]
        score, anchor = max(ranked, default=(0, 0))
        if score <= 0:
            return ""  # 字面找不到时由 rounds.py 借记事的主题／向量定位；这里不猜。
        start = max(0, min(anchor - (count // 2 - 1), len(rows) - count))
        selected = rows[start:start + count]
        header = f"【{self.source}里那时的原话】\n"
        note = self.source_note(selected)
        if note:
            header += note + "\n"
        # 先给命中的那句话留够位置，防止前面的长话挤掉真正想回查的内容。
        anchor_row = rows[anchor]
        anchor_line = _line(anchor_row)
        anchor_budget = min(len(anchor_line), 3000)
        other_budget = max(80, (MAX_UNPACK_CHARS - len(header) - anchor_budget - len(selected)) // max(1, len(selected) - 1))
        lines = [_excerpt_line(row, anchor_budget if row is anchor_row else other_budget, about) for row in selected]
        return (header + "\n".join(lines))[:MAX_UNPACK_CHARS]
