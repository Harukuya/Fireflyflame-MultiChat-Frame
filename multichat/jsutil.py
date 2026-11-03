"""
和原版 TypeScript 对得上要用到的几样小东西。

⚠️ 作息、哈希、取整、字符类这些地方，原版用的是 JS 的语义：
  - Math.round 是「.5 往上」，Python 的 round 是「.5 取偶」——不能混用
  - 位运算是 32 位有符号整数
  - 正则里的 \\s、\\p{P}、\\p{S} 和 Python 的 re 不完全一样
照抄这些，作息和检索才能和原版一模一样。
"""

from __future__ import annotations

import math
import random
import re
import time
import unicodedata
from datetime import datetime

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR

# JS 正则里 \s 认的那些空白（比 Python 的 \s 少几个控制字符，多一个 ﻿）
WS = "\t\n\v\f\r    -     　﻿"
_WS_SET = set("\t\n\v\f\r       　﻿") | {chr(c) for c in range(0x2000, 0x200B)}


def now_ms() -> int:
    return int(time.time() * 1000)


def js_round(x: float) -> int:
    """JS 的 Math.round：.5 一律往上。"""
    return math.floor(x + 0.5)


def js_fixed(x: float, digits: int = 2) -> float:
    """+x.toFixed(digits)：按二进制的精确值四舍五入（不是取偶）。"""
    from decimal import ROUND_HALF_UP, Decimal
    return float(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def js_trim(s: str) -> str:
    """JS 的 String.prototype.trim。"""
    i, j = 0, len(s)
    while i < j and s[i] in _WS_SET:
        i += 1
    while j > i and s[j - 1] in _WS_SET:
        j -= 1
    return s[i:j]


def is_ws(ch: str) -> bool:
    return ch in _WS_SET


def _cat(ch: str) -> str:
    return unicodedata.category(ch)[0]


def strip_ps(text: str) -> str:
    """去掉 JS 的 /[\\s\\p{P}\\p{S}]/gu 认的字。"""
    return "".join(ch for ch in text if ch not in _WS_SET and _cat(ch) not in ("P", "S"))


def strip_p(text: str) -> str:
    """去掉 JS 的 /[\\s\\p{P}]/gu 认的字（风格样例按字比时用）。"""
    return "".join(ch for ch in text if ch not in _WS_SET and _cat(ch) != "P")


def is_ps(ch: str) -> bool:
    return ch in _WS_SET or _cat(ch) in ("P", "S")


def to_i32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def to_u32(v: int) -> int:
    return v & 0xFFFFFFFF


def local_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1000)


def format_timestamp(ts) -> str:
    """请求中的真实记录时间，沿用本地时区；坏值不补成今天或 1970 年。"""
    try:
        if type(ts) not in (int, float) or not math.isfinite(ts) or ts <= 0:
            return "时间不详"
        return local_dt(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "时间不详"


def message_time(ts) -> str:
    return f"〔消息时间：{format_timestamp(ts)}〕"


def day_of(ts: int) -> int:
    """按本地日算的第几天（原版的 dayOf：本地零点的时刻 / 一天）。"""
    d = local_dt(ts)
    midnight = datetime(d.year, d.month, d.day).timestamp() * 1000
    return math.floor(midnight / DAY)


def rand_between(a: float, b: float) -> float:
    return a + random.random() * (b - a)


def jre(pattern: str, flags: int = 0) -> re.Pattern:
    """把原版正则里的 \\s 换成 JS 的那组空白再编译（字符类里面直接展开，外面套一层方括号）。"""
    out: list[str] = []
    in_class = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt == "s":
                out.append(WS if in_class else f"[{WS}]")
            else:
                out.append(c + nxt)
            i += 2
            continue
        if c == "[" and not in_class:
            in_class = True
        elif c == "]" and in_class:
            in_class = False
        elif c == "$" and not in_class and not flags & re.M:
            # JS 的 $（不带 m）只认结尾；Python 的 $ 还认结尾那个换行的前面
            out.append(r"\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return re.compile("".join(out), flags)
