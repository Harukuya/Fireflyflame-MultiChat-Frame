"""必需工具结果的原因分布：只存枚举、工具名和次数，不存模型正文。"""

from __future__ import annotations

import json
import os
import re

from .jsutil import day_of
from .usage import CATEGORIES, TOOL_RESULT_FIELDS, TOOL_OUTCOME_FIELDS, _number


REASONS = frozenset((
    "text-only", "wrong-tool", "no-result-tools", "no-key", "auth", "bad-endpoint",
    "bad-model", "no-network", "quota", "timeout", "rate-limit", "server", "empty",
    "unknown", "cancelled",
))
_TOOL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_EXCEPTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_MAX_BUCKETS = 256


def normalize_detail(value) -> dict:
    """仅接受调用端提供的类型字段；错误详情及未知键从不进入存档。"""
    value = value if isinstance(value, dict) else {}
    reason = value.get("reason")
    reason = reason if isinstance(reason, str) and reason in REASONS else "unknown"
    tools = value.get("tools")
    tools = tools if isinstance(tools, (list, tuple, set)) else []
    result = {"reason": reason, "tools": sorted({x for x in tools if isinstance(x, str) and _TOOL.fullmatch(x)})[:8]}
    exception = value.get("exception")
    if isinstance(exception, str) and _EXCEPTION.fullmatch(exception):
        result["exception"] = exception
    status = _number(value.get("http"))
    if status is not None and 100 <= status <= 599:
        result["http"] = status
    return result


def _key(detail: dict) -> str:
    return json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _buckets(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    by_key = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        count = _number(item.get("count")) or 0
        if count:
            _add_bucket(by_key, normalize_detail(item), count)
    return list(by_key.values())


def _add_bucket(by_key: dict, detail: dict, count: int = 1) -> None:
    key = _key(detail)
    # 正常调用只有少量固定类型；损坏文件或异常插件不能无限扩展一列。
    # 达到上限的额外类型归入无明细桶，次数保留，不伪造已知原因。
    if key not in by_key and len(by_key) >= _MAX_BUCKETS - 1:
        detail = {"reason": "unknown", "tools": []}
        key = _key(detail)
    bucket = by_key.setdefault(key, {**detail, "count": 0})
    bucket["count"] += count


def _fields(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for field in TOOL_RESULT_FIELDS:
        buckets = _buckets(value.get(field))
        if buckets:
            result[field] = buckets
    return result


def _category_rows(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {category: _fields(row) for category, row in value.items()
            if category in CATEGORIES and isinstance(row, dict)}


class ToolDetails:
    """用途按事件日期、模型按任务结束日期记账；旧计数不被反推成原因。"""

    def __init__(self, root: str, redact):
        self.path = os.path.join(root, "tool_details.json")
        self.redact = redact
        try:
            with open(self.path, encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError):
            value = {}
        value = value if isinstance(value, dict) else {}
        models = value.get("models")
        models = models if isinstance(models, dict) else {}
        self.data = {
            "since": _number(value.get("since")) or 0,
            "day": value.get("day") if isinstance(value.get("day"), str) else "",
            "today": _category_rows(value.get("today")),
            "total": _category_rows(value.get("total")),
            "models": {period: self._model_rows(models.get(period)) for period in ("today", "total")},
        }

    def _identity(self, value) -> dict | None:
        """ID 的格式沿用 Events._model_identity；重读时仍清理名称和凭据形态。"""
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or len(value["id"]) > 2200:
            return None
        try:
            pairs = json.loads(value["id"])
        except (ValueError, TypeError):
            return None
        if not isinstance(pairs, list) or not 1 <= len(pairs) <= 3:
            return None
        clean = []
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
                return None
            clean.append([self.redact(pair[0])[:160], self.redact(pair[1])[:160]])
        return {
            "id": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
            "provider": clean[0][0] if len(clean) == 1 else "多模型任务",
            "model": clean[0][1] if len(clean) == 1 else " → ".join(f"{p} / {m}" for p, m in clean),
        }

    def _model_rows(self, value) -> dict:
        if not isinstance(value, dict):
            return {}
        result = {}
        for row in value.values():
            identity = self._identity(row)
            if identity is not None:
                result[identity["id"]] = {**identity, "fields": _fields(row.get("fields"))}
        return result

    def _touch(self, now: int) -> None:
        data = self.data
        if not data["since"]:
            data["since"] = now
        day = str(day_of(now))
        if data["day"] != day:
            data["day"] = day
            data["today"] = {}
            data["models"]["today"] = {}

    def _save(self) -> None:
        try:
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _increment(fields: dict, field: str, detail: dict) -> None:
        buckets = {_key({k: v for k, v in row.items() if k != "count"}): row
                   for row in fields.setdefault(field, [])}
        _add_bucket(buckets, detail)
        fields[field] = list(buckets.values())

    def record(self, category: str, field: str, detail, now: int) -> None:
        if field not in TOOL_RESULT_FIELDS:
            return
        category = category if category in CATEGORIES else "其他"
        detail = normalize_detail(detail)
        self._touch(now)
        for period in ("today", "total"):
            self._increment(self.data[period].setdefault(category, {}), field, detail)
        self._save()

    def record_model(self, outcome: str, identity: dict, details, now: int) -> None:
        if outcome not in TOOL_OUTCOME_FIELDS or not isinstance(details, (list, tuple)):
            return
        identity = self._identity(identity)
        if identity is None:
            return
        trace = [(item.get("field"), normalize_detail(item)) for item in details
                 if isinstance(item, dict) and item.get("field") in TOOL_RESULT_FIELDS]
        if not trace:
            return
        self._touch(now)
        for period in ("today", "total"):
            row = self.data["models"][period].setdefault(identity["id"], {**identity, "fields": {}})
            for field, detail in trace:
                self._increment(row["fields"], field, detail)
        self._save()

    def snapshot(self, now: int) -> dict:
        data = self.data
        today = data["day"] == str(day_of(now))
        # 所有清理函数都会重新分配字典和 tools 列表；调用方不能改动内存统计。
        return {
            "since": data["since"],
            "today": _category_rows(data["today"]) if today else {},
            "total": _category_rows(data["total"]),
            "models": {
                "today": list(self._model_rows(data["models"]["today"]).values()) if today else [],
                "total": list(self._model_rows(data["models"]["total"]).values()),
            },
        }
