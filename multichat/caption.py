"""主模型不支持图片时，由宿主选择独立的 ModelProvider 转述。
转述附着在原 attachment 上；同张图片每次运行最多尝试一次。"""

from __future__ import annotations

import asyncio
import os
import re

from .jsutil import now_ms
from .events import redact
from .llm import Llm, b64_of, image_block
from .usage import measure_async

PROMPT = "用中文简短描述这张图片：画面里有什么；图上有字就照原样写出来；是表情包就说是什么表情、什么情绪。不超过 60 字，不评价，不加前缀。"
WAIT_S = 20
RECENT = 4  # 补转述时只补要看的最后几张


def image_note(a: dict | None) -> str:
    """一张图交给她（看不了图时）的写法"""
    if not a:
        return ""
    cap = a.get("caption")
    return f"［发了一张图片：{cap}］" if cap else "［发了一张图片］"


def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip().strip("［］[]")
    return t[:120]


class Captioner:
    def __init__(self, images_dir: str, logger, events=None):
        self.images = images_dir
        self.log = logger
        self.events = events
        self._tasks: dict[str, asyncio.Task] = {}
        self._tried: set[str] = set()
        self.closed = False

    def start(self, provider, a: dict, saved) -> None:
        """开始转述这张图（不等）。provider 是 None = 不用转述"""
        f = a.get("file") if a else None
        if self.closed or provider is None or not f or a.get("kind") != "image" or a.get("caption") or f in self._tried:
            return
        self._tried.add(f)
        task = asyncio.get_running_loop().create_task(self._one(provider, a, saved))
        self._tasks[f] = task
        task.add_done_callback(lambda _t, f=f: self._tasks.pop(f, None))

    @measure_async("看图转述")
    async def _one(self, provider, a: dict, saved) -> None:
        path = os.path.join(self.images, a["file"])
        if not os.path.isfile(path):
            return
        if self.events:
            self.events.count("模型调用")
        r = await Llm(lambda: provider, self.log, events=self.events).chat_with_retry(
            "", [{"role": "user", "content": [image_block(a.get("mediaType", "image/jpeg"), b64_of(path)),
                                              {"type": "text", "text": PROMPT}]}])
        if self.closed:
            return
        cap = _clean(r.text) if r.ok else ""
        if not cap:
            if self.events:
                self.events.count("模型失败")
                self.events.note("出错", f"图片转述没成：{r.kind or '空'}", level="warn")
            return
        a["caption"] = cap
        try:
            saved()
        except Exception as e:  # noqa: BLE001
            self.log.debug(redact(f"[MultiChat] 存图片转述失败：{e!r}"))

    async def ready(self, provider, msgs: list[dict], saved, deadline: int | None = None) -> None:
        """要看这些消息了：最后几张图里还没转述的补上；这些消息里所有还在转的都等它们转完（最多等到 deadline）"""
        if provider is None or self.closed:
            return
        imgs = [m["attachment"] for m in msgs
                if m.get("role") == "user" and (m.get("attachment") or {}).get("kind") == "image"]
        for a in imgs[-RECENT:]:
            self.start(provider, a, saved)
        pending = [self._tasks[a["file"]] for a in imgs if a.get("file") in self._tasks]
        timeout = WAIT_S if deadline is None else (deadline - now_ms()) / 1000
        if pending and timeout > 0:
            await asyncio.wait(pending, timeout=timeout)

    def stop(self) -> None:
        self.closed = True
        for t in list(self._tasks.values()):
            t.cancel()
