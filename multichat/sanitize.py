"""
禁则 · 代码层（原版 sanitize.ts）

⭐ 能用代码检查的就不要祈求模型自觉。
"""

from __future__ import annotations

from . import profile

import re
from datetime import datetime

from .jsutil import js_trim, jre

MAX_CHARS_PER_MESSAGE = 60

MODERN_SLANG = []  # 框架不替用户预设禁用语；语气约束由 prompts/voice.md 定义。

_STAR = re.compile(r"\*{1,2}[^*\n]{1,20}\*{1,2}")
_LENTICULAR = re.compile(r"【[^】\n]{1,24}】")
_SQUARE = re.compile(r"\[{1,2}[^\[\]\n]{1,24}\]{1,2}")
_PAREN = re.compile(r"[（(]([^）)\n]{1,12})[）)]")
_PAREN_PUNCT = re.compile(r"[，。？！、,.?!]")
def _speaker():
    return re.compile(r"^\s*" + re.escape(profile.bot_name()) + r"\s*[:：]\s*", re.I)
_BULLET = jre(r"^\s*[-*·•]\s*")
_NUMBER = jre(r"^\s*\d+[.、)]\s*")
_BREAK = re.compile(r"[。！？…；.!?;][^。！？…；.!?;]*$")
_MESSAGE_TIME = re.compile(
    r"[^\S\r\n]*〔消息时间：([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2})〕"
)
_TIME_EXPLANATION = re.compile(
    r"[^\S\r\n]*(?:是什么意思|表示|代表|指的是|这个标签|这种标签|"
    r"是(?:一个|一条|一种)?(?:时间)?标签|是程序|是内部|是记录)"
)


def strip_leading_time_labels(line: str) -> str:
    """只移除单个原始行开头的完整内部时间标签；正文、引用和残片不猜测。"""
    if "\n" in line or "\r" in line:
        return line
    end = 0
    while match := _MESSAGE_TIME.match(line, end):
        try:
            datetime(*(int(part) for part in match.groups()))
        except ValueError:
            break
        end = match.end()
    if not end or _TIME_EXPLANATION.match(line, end):
        return line
    return re.sub(r"^[^\S\r\n]+", "", line[end:])


def _strip_output_time_labels(line: str) -> str:
    # 只越过现有清洗本就认识的角色名、列表前缀，不能从正文中搜标签。
    end = 0
    for pattern in (_speaker(), _BULLET, _NUMBER):
        match = pattern.match(line[end:])
        if match:
            # *标签* 是强调，不把它当作带空格的列表前缀。
            if pattern is _BULLET and match.group(0).endswith("*"):
                continue
            end += match.end()
    return line[:end] + strip_leading_time_labels(line[end:])


def _strip_stage_directions(text: str) -> str:
    t = _STAR.sub("", text)
    t = _LENTICULAR.sub("", t)
    t = _SQUARE.sub("", t)
    # （歪头）——只删看起来像舞台指示的：短、且内部没有句读
    t = _PAREN.sub(lambda m: m.group(0) if _PAREN_PUNCT.search(m.group(1)) else "", t)
    t = _speaker().sub("", t, count=1)
    return t


def _calm_punctuation(text: str) -> str:
    text = re.sub(r"[!！]{2,}", "！", text)
    text = re.sub(r"[?？]{2,}", "？", text)
    return re.sub(r"[~～]{2,}", "～", text)


def detect_modern_slang(text: str) -> list[str]:
    lower = text.lower()
    return [w for w in MODERN_SLANG if w.lower() in lower]


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS_PER_MESSAGE:
        return [text]
    out: list[str] = []
    rest = text
    while len(rest) > MAX_CHARS_PER_MESSAGE:
        window = rest[:MAX_CHARS_PER_MESSAGE]
        m = _BREAK.search(window)
        cut = m.start() + 1 if m and m.start() > MAX_CHARS_PER_MESSAGE * 0.4 else MAX_CHARS_PER_MESSAGE
        out.append(js_trim(rest[:cut]))
        rest = js_trim(rest[cut:])
    if rest:
        out.append(rest)
    return [x for x in out if x]


def to_messages(raw: str) -> list[str]:
    """清洗并分条，保留全部有效内容，不按消息条数截断；清洗后为空 = 她沉默。"""
    lines = []
    for line in re.split(r"\r?\n", raw or ""):
        line = _strip_output_time_labels(line)
        line = js_trim(_calm_punctuation(_strip_stage_directions(line)))
        line = _BULLET.sub("", line, count=1)
        line = _NUMBER.sub("", line, count=1)
        if line:
            lines.append(line)
    merged = [part for line in lines for part in _split_long(line)]
    return merged
