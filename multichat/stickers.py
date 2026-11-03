"""
表情包（这边新加的，合并自表情包插件）：她的「发表情」工具。

⭐ 和原版一样，她要做的事走工具，不写进消息（不用 {memes:高兴} 那种占位符）。
⭐ 只列有图的类别；一轮最多一张，是否发送由她决定，不限制间隔或连续轮次。
图放在 plugin_data/<插件>/stickers/<类别>/ 下：旧英文目录保留中文映射，新增目录名直接作为类别。
第一次运行从插件自带的拷过去，之后往那里加图不会被更新覆盖。
"""

from __future__ import annotations

import os
import random
import shutil
from collections import Counter

EMOTIONS = {
    "happy": "开心", "hello": "打招呼", "angry": "生气", "dislike": "嫌弃", "fear": "害怕", "helpless": "无奈",
    "sad": "难过", "shame": "害羞", "bye": "再见", "surprise": "惊讶", "taunt": "嘲讽",
}
EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class Stickers:
    def __init__(self, data_root: str, bundled: str):
        self.root = os.path.join(data_root, "stickers")
        if not os.path.isdir(self.root) and os.path.isdir(bundled):
            shutil.copytree(bundled, self.root)
        os.makedirs(self.root, exist_ok=True)
        for d in EMOTIONS:
            os.makedirs(os.path.join(self.root, d), exist_ok=True)

    def _inside(self, path: str) -> bool:
        """扫描到的目录和图片也不能通过链接跑到表情库以外。"""
        try:
            root = os.path.normcase(os.path.realpath(self.root))
            real = os.path.normcase(os.path.realpath(path))
            return os.path.commonpath([root, real]) == root
        except (OSError, ValueError):
            return False

    def catalog(self) -> dict[str, dict[str, str]]:
        """即时扫描一层类别目录：类别 -> 图片标识 -> 路径。

        旧英文目录与同名中文目录合并；同类内重名的图片用目录前缀区分，避免覆盖。
        仪表盘用一次扫描的结果，避免每张图都重新遍历整个图片库。
        """
        groups = {label: [] for label in EMOTIONS.values()}
        try:
            with os.scandir(self.root) as entries:
                dirs = sorted(entries, key=lambda x: x.name)
        except OSError:
            return {label: {} for label in groups}
        for directory in dirs:
            try:
                if directory.name.startswith(".") or not directory.is_dir() or not self._inside(directory.path):
                    continue
                label = EMOTIONS.get(directory.name.lower(), directory.name)
                rows = groups.setdefault(label, [])
                with os.scandir(directory.path) as entries:
                    files = sorted(entries, key=lambda x: x.name)
                for entry in files:
                    if (not entry.name.startswith(".") and entry.name.lower().endswith(EXTS)
                            and entry.is_file() and self._inside(entry.path)):
                        rows.append((directory.name, entry.name, entry.path))
            except OSError:
                continue  # 用户正好移走/改名目录时，下次扫描再读取，不影响聊天。
        out = {}
        for label, rows in groups.items():
            counts = Counter(name.casefold() for _, name, _ in rows)
            out[label] = dict(sorted(
                (f"{directory}/{name}" if counts[name.casefold()] > 1 else name, path)
                for directory, name, path in rows
            ))
        return out

    def library(self) -> dict[str, list[str]]:
        return {label: list(files) for label, files in self.catalog().items()}

    def available(self) -> list[str]:
        return [label for label, files in self.library().items() if files]

    def can_send(self, history: list[dict] | None = None, now: int | None = None) -> bool:
        """有图就提供工具；兼容旧调用参数，但不再按历史或时间限频。"""
        return bool(self.available())

    def tool(self) -> dict:
        labels = self.available()
        return {
            "name": "send_sticker",
            "description": "发一张你的表情包。你可以根据聊天和自己的心情决定要不要发，想发就发。可以配着文字发，也可以只发表情。",
            "input_schema": {
                "type": "object",
                "properties": {"emotion": {"type": "string", "enum": labels, "description": "选哪一类表情"}},
                "required": ["emotion"],
            },
        }

    def pick(self, label: str) -> str | None:
        files = list(self.catalog().get(label, {}).values())
        return random.choice(files) if files else None

    def path_of(self, label: str, name: str) -> str | None:
        return self.catalog().get(label, {}).get(name)
