"""
要一直存着的东西（原版 store.ts / disk.ts 那几个键）

由宿主提供独立数据目录；运行文件与源码分开保存：
  messages.json    聊天记录（图片另存在 images/，这里只记路径）
  her.json         所有会话共用的心情、精力（旧看手机计数保留作迁移源）
  state.json       念头、念头账本、主动找他的次数、会话地址
  memory.json      记忆
  memory-vec.json  记忆的向量缓存
⚠️ 写文件先写临时文件再换名，断电也不会写坏一半。
"""

from __future__ import annotations

import json
import os
import uuid

from .jsutil import now_ms
from .memory import empty_memory
from .mood import settle as settle_mood
from .thought import empty_ledger


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # 坏了就挪开留个底，从默认值重来
        try:
            os.replace(path, f"{path}.broken-{now_ms()}")
        except OSError:
            pass
        return default


def _write(path: str, data) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


class Her:
    """她自己的共享心情和精力；旧 look 只供各群首次迁移，之后各群独立计次。"""

    def __init__(self, root: str):
        os.makedirs(root, exist_ok=True)
        self._path = os.path.join(root, "her.json")
        data = _read(self._path, {})
        self.energy: dict | None = data.get("energy")
        self.look: dict = data.get("look") or {}
        self.mood: dict = settle_mood(data.get("mood"))
        if self.mood != data.get("mood"):
            self.save()

    def save(self) -> None:
        _write(self._path, {"energy": self.energy, "look": self.look, "mood": self.mood})


class Store:
    """一个会话的状态；Runtime 使用会话 ID 的哈希分配安全子目录"""

    def __init__(self, root: str, her: Her | None = None):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.her = her or Her(root)
        self.images = os.path.join(root, "images")
        os.makedirs(self.images, exist_ok=True)
        self.messages: list[dict] = _read(self._p("messages.json"), [])
        state = _read(self._p("state.json"), {})
        # 旧 state.json 的 mood 表示和主要联系人的关系，不移植成她自己的情绪。
        if self.her.energy is None and state.get("energy"):
            self.her.energy = state["energy"]  # 旧版存在这里
            self.her.save()
        self.thought: dict | None = state.get("thought")
        self.thought_ledger: dict = state.get("thoughtLedger") or empty_ledger()
        self.proactive_day: dict = state.get("proactiveDay") or {"day": 0, "n": 0}
        self.umo: str | None = state.get("umo")
        # 其他会话自己要存的小东西（群：读到哪了、她最近一次说话、接话方式……）
        self.extra: dict = {k: v for k, v in state.items()
                            if k not in ("mood", "energy", "thought", "thoughtLedger", "proactiveDay", "umo")}
        self.memory: dict = {**empty_memory(), **_read(self._p("memory.json"), {})}
        self.memory_vec: dict = _read(self._p("memory-vec.json"), {})

    def _p(self, name: str) -> str:
        return os.path.join(self.root, name)

    @property
    def mood(self) -> dict:
        return self.her.mood

    @mood.setter
    def mood(self, value: dict) -> None:
        self.her.mood = value

    @property
    def energy(self) -> dict | None:
        return self.her.energy

    @energy.setter
    def energy(self, value: dict | None) -> None:
        self.her.energy = value

    def save_messages(self) -> None:
        _write(self._p("messages.json"), self.messages)

    def save_state(self) -> None:
        _write(self._p("state.json"), {
            "thought": self.thought, "thoughtLedger": self.thought_ledger,
            "proactiveDay": self.proactive_day, "umo": self.umo, **self.extra,
        })
        self.her.save()

    def save_memory(self) -> None:
        _write(self._p("memory.json"), self.memory)

    def save_memory_vec(self) -> None:
        _write(self._p("memory-vec.json"), self.memory_vec)

    @staticmethod
    def new_msg(role: str, text: str, system: bool = False, attachment: dict | None = None, ts: int | None = None) -> dict:
        m = {"id": f"{now_ms()}-{uuid.uuid4().hex[:6]}", "role": role, "text": text, "ts": now_ms() if ts is None else ts}
        if system:
            m["system"] = True
        if attachment:
            m["attachment"] = attachment
        return m

    def her_never_replied(self) -> bool:
        return not any(m["role"] == "her" and not m.get("system") for m in self.messages)
