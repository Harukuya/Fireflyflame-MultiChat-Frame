"""Quote-aware projection and memory budgets; no real model, host or disk store.

Usage: python -I -B quote_excerpt239_test.py <plugin/framework directory>
Extra acceptance-runner arguments are ignored. Works with all three editions.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime
import importlib
from pathlib import Path
import sys
import types

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

root = Path(sys.argv[1]).resolve()
package = types.ModuleType("quote_excerpt_fixture")
package.__path__ = [str(root / ("firefly" if (root / "firefly").is_dir() else "multichat"))]
sys.modules[package.__name__] = package
H = importlib.import_module(package.__name__ + ".history")
R = importlib.import_module(package.__name__ + ".rounds")
P = importlib.import_module(package.__name__ + ".recent")

# The fallback lets an old edition run to real failing assertions instead of
# mistaking a missing helper import for a useful negative control.
excerpt = getattr(H, "_excerpt_text", lambda row, limit, query="": H._excerpt(H._text(row), limit, query))
NOW = int(datetime(2026, 11, 5, 20, 0).timestamp() * 1000)
OLD = int(datetime(2025, 10, 9, 12, 34).timestamp() * 1000)
OWNER, WRITER = "被引原作者甲", "本条发送者乙"
old_text = "旧材料。" * 180 + "targetmeeting" + "旧材料。" * 180
row = {"id": "new-reply", "qid": "new-qid", "role": "user", "uid": "222", "name": WRITER,
       "text": "真的吗？", "ts": NOW, "read": True,
       "quote": {"qid": "old-qid", "role": "user", "uid": "111", "name": OWNER,
                 "text": old_text, "ts": OLD}}
original = copy.deepcopy(row)
results = []


def check(name, value, detail=None):
    results.append(bool(value))
    print(f"[{'对' if value else '错'}] {name}" + (f" {detail}" if not value and detail is not None else ""))


def unit_text(item, query):
    return P.RecentPool._unit_text({"kind": "raw", "item": item}, query)


shared = unit_text(row, "targetmeeting")
check("共享命中长引文中段仍标明原作者和引用", OWNER in shared and "引用旧消息" in shared)
check("共享原话日期与本条日期分别保留", "2025-10-09 12:34" in shared and "2026-11-05 20:00" in shared)
check("共享当前发送者和本条短回复仍在", WRITER in shared and "真的吗？" in shared and "本条正文" in shared)
check("命中的旧原话仍能在预算内提供", "targetmeeting" in shared and len(shared) < 650, len(shared))
check("共享不重复渲染引文或正文分界", shared.count("引用旧消息") == 1 and shared.count("本条正文") == 1)

long_new = {**row, "text": "新内容。" * 180 + "newdecision" + "新内容。" * 180}
long_shared = unit_text(long_new, "newdecision")
check("长本条正文优先保留命中内容", "newdecision" in long_shared)
check("长正文裁剪仍不把旧作者归到新作者", OWNER in long_shared and WRITER in long_shared and "本条正文" in long_shared)
check("长正文和旧引文分别受预算约束", len(long_shared) < 650 and long_shared.count("引用旧消息") == 1, len(long_shared))

tiny_results = [excerpt(row, budget, "targetmeeting") for budget in (0, 1, 4, 5, 8, 16, 32, 64, 120)]
check("极小预算严格不超长", all(len(text) <= budget for text, budget in zip(tiny_results, (0, 1, 4, 5, 8, 16, 32, 64, 120))))
check("极小预算不脱离作者和时间裸露旧引文", all(
    ("targetmeet" not in text and "旧材料" not in text) or
    (OWNER in text and "2025-10-09 12:34" in text and "本条正文" in text) for text in tiny_results))
check("能容纳当前正文时不会只剩旧引用标签", "真的吗？" in excerpt(row, 64, "targetmeeting"))
for_budget = [excerpt(row, budget, "targetmeeting") for budget in (200, 300, 480, 600, 1000, 2000)]
check("正常预算中的引文保留作者和原时间", all("targetmeeting" not in text or
      (OWNER in text and "2025-10-09 12:34" in text and "本条正文" in text) for text in for_budget))
check("所有引文预算按最终渲染长度计算", all(len(text) <= budget for text, budget in zip(for_budget, (200, 300, 480, 600, 1000, 2000))))

plain = {key: value for key, value in long_new.items() if key != "quote"}
check("无引用原话保持原先摘录行为", excerpt(plain, 480, "newdecision") == H._excerpt(H._text(plain), 480, "newdecision"))
short_quote = {**row, "quote": {**row["quote"], "text": "原话短句"}}
check("短内容不被多余裁剪或改写", excerpt(short_quote, 2000) == H._text(short_quote))
missing = {**row, "quote": {"qid": "missing", "name": OWNER, "role": "user", "missing": True, "text": ""}}
unknown = unit_text(missing, "")
check("取不到原文仍明确未知，不用最近一句代替", "原消息内容未取得" in unknown and "真的吗？" in unknown and "targetmeeting" not in unknown)
check("没有原始时间不造一个当前时间", "原消息时间未提供" in unknown and "2025-10-09" not in unknown)

sources = []
for number in range(16):
    item = copy.deepcopy(row)
    item.update(id=f"new-{number}", uid=str(222 + number), name=f"发送者{number}", ts=NOW + number * 1000)
    item["quote"].update(name=f"原作者{number}")
    sources.append({"scope": f"g:{700000 + number}", "label": f"测试群{number}", "rows": [item], "packs": []})
pool = P.RecentPool(lambda: sources)
context = pool.context("p:800000", "targetmeeting")
check("跨会话多来源引文仍守4000字符总预算", 0 < len(context) <= P.MAX_CHARS, len(context))
displayed = [n for n in range(16) if f"发送者{n}（" in context]
check("每个共享来源中的旧作者与新作者仍配对", bool(displayed) and all(f"原作者{n}（" in context for n in displayed))
check("共享池不把一条引用拆成额外新消息", context.count("〔原句｜") == len(displayed))

huge = {**row, "text": "本条很长。" * 3000 + "当前正文结尾"}
limited = R.RoundMemory._limited_messages([huge], 30, 6000)
text = H._line(limited[0]) if limited else ""
check("临时窗口截断守6000字符预算", bool(limited) and len(text) <= 6000, len(text))
check("临时窗口截断只渲染一次引文", bool(limited) and text.count("引用旧消息") == 1 and text.count("本条正文") == 1)
check("临时窗口裁剪保留两位发言者与旧日期", OWNER in text and WRITER in text and "2025-10-09 12:34" in text)
check("裁剪投影不会保留再次展开的quote字段", bool(limited) and "quote" not in limited[0])
line = H._excerpt_line(row, 400, "targetmeeting")
check("摘要单行预算保留当前作者和引文原作者", len(line) <= 400 and OWNER in line and WRITER in line and "真的吗？" in line)

# Run the real enqueue/de-duplication code with an in-memory persistence sink.
archive = H.History.__new__(H.History)
archive._rows, archive._meta, archive._removed, archive._dirty = {}, {}, set(), False
archive._save = lambda: setattr(archive, "_dirty", False)
rounds = R.RoundMemory.__new__(R.RoundMemory)
rounds.archive, rounds._records, rounds._consumed = archive, [], set()
rounds._shared, rounds._selection, rounds._dirty = {}, None, False
rounds._lock = asyncio.Lock()
rounds._save = lambda: setattr(rounds, "_dirty", False)
one = rounds.enqueue([row, row], round_id="round-one")
two = rounds.enqueue([row], round_id="round-two")
check("引用只随本条入包，重复enqueue不新建一份", one == "round-one" and two is None and len(rounds._records) == 1 and
      rounds._records[0]["ids"] == ["new-reply"] and len(archive.messages) == 1)
check("被引用的人不冒充本轮参与者", rounds._records[0]["people"] == {"222": WRITER})
check("档案保留原作者元数据且当前发言者不被覆盖", archive.messages[0].get("uid") == "222" and
      (archive.messages[0].get("quote") or {}).get("uid") == "111")
check("只渲染和裁剪不更改原消息或原引用快照", row == original)

print(f"\nquote excerpt239 {sum(results)}/{len(results)}")
raise SystemExit(0 if all(results) else 1)
