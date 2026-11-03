"""
最近动态和今日统计（这边新加的，原版没有）：给仪表盘看。

动态只在内存里留最近 200 件；统计按天存在 stats.json。
"""

from __future__ import annotations

import json
import os
import re
from collections import deque

from .jsutil import day_of, now_ms
from .tool_details import ToolDetails
from .usage import CATEGORIES, TOKEN_FIELDS, TOOL_RESULT_FIELDS, TOOL_OUTCOME_FIELDS, _number

COUNT_FIELDS = ("回话", "群里说话", "不想回", "无需回复", "看手机", "判断调用", "模型调用", "模型失败", "表情", "自动回复")
USAGE_FIELDS = ("requests", "retries", "failed", "cancelled", "elapsedMs", "inputChars", "outputChars",
                "usageKnown", "usagePartial", "usageUnknown", *TOKEN_FIELDS,
                "inputKnown", "outputKnown", "cachedKnown", "reasoningKnown")

# Authorization 头：从冒号后面到这一行结束全部打码（Digest / AWS 这类值里带引号、逗号、空格，没法认出哪里结束，宁可多打）
_AUTH = re.compile(r"(?i)(\b(?:proxy-)?authorization(?:\\?[\"'])?\s*[:=]\s*)[^\r\n]+")
# 凭据字段（按名字认，不认单独的「key」：免得把 key: ArrowLeft 这种正常内容也打掉）。
# 值：带引号的（"…"、'…'、JSON 里转义的 \"…\"）打到配对的那个引号为止，里面有空格、逗号也算；不带引号的到空白、逗号为止
_FIELD = re.compile(
    r"(?i)(\b(?:api[ _-]?key|apikey|x-api-key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|"
    r"secret|password)\b(?:\s+provided)?(?:\\?[\"'])?\s*[:=]\s*)"
    r"(\\\"(?:\\\\\\\"|(?!\\\").)*\\\"|\"(?:[^\"\\\r\n]|\\.)*\"|'(?:[^'\\\r\n]|\\.)*'|[^\s\"',;&}\\]+)")
# ↑ 第一种是 JSON 里转义的 \"…\"；里面再转义一层的引号（\\\"，双重 JSON 才会有）不算结束
_OTHERS = [
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)([?&](?:key|token|access_token|api_key)=)[^&\s\"']+"),  # 网址参数里的 key= / token=
    re.compile(r"()\b(?:sk|ak|pk|rk)-[A-Za-z0-9_-]{12,}"),
]


def _mask_value(v: str) -> str:
    for q in ('\\"', '"', "'"):
        if v.startswith(q) and v.endswith(q) and len(v) >= 2 * len(q):
            return f"{q}***{q}"
    return "***"


def redact(text: str) -> str:
    """报错原文里像凭据的东西打码（Authorization 头、api_key=…、password="…"、Incorrect API key provided: …、?key=…、sk-… 这类）；
    状态码、报错类型照留。外部来的报错文字进日志、动态之前都过一遍这个"""
    out = _AUTH.sub(lambda m: m.group(1) + "***", str(text))
    out = _FIELD.sub(lambda m: m.group(1) + _mask_value(m.group(2)), out)
    for rx in _OTHERS:
        out = rx.sub(lambda m: m.group(1) + "***", out)
    return out


class Events:
    def __init__(self, root: str, logger):
        self.log = logger
        self.items: deque = deque(maxlen=200)
        self._path = os.path.join(root, "stats.json")
        try:
            with open(self._path, encoding="utf-8") as f:
                self.stats = json.load(f)
            if not isinstance(self.stats, dict):
                self.stats = {}
        except (OSError, ValueError):
            self.stats = {}
        # 累计独立存盘，只在文件首次创建时接收仍保留的当日记录；旧日记录不能冒充完整历史。
        self._total_path = os.path.join(root, "stats_total.json")
        total_exists = os.path.exists(self._total_path)
        data = self._read_stats(self._total_path)
        now = now_ms()
        self._totals = {
            "since": _number(data.get("since")) or now,
            "importedDay": data.get("importedDay") if isinstance(data.get("importedDay"), str)
                           and re.fullmatch(r"-?\d{1,8}", data["importedDay"]) else "",
            "counts": self._counts(data.get("counts")),
            "modelUsage": self._usage_rows(data.get("modelUsage")),
        }
        if not total_exists and self.stats.get("day") == str(day_of(now)):
            self._totals["counts"] = self._counts(self.stats)
            self._totals["modelUsage"] = self._usage_rows(self.stats.get("model_usage"))
            if self._totals["counts"] or self._totals["modelUsage"]:
                self._totals["importedDay"] = str(day_of(now))
        self._save_json(self._total_path, self._totals)
        # 按模型的结果从新增记录起算，不能从旧的分类累计猜模型或首次成功次数。
        self._tool_models_path = os.path.join(root, "tool_models.json")
        models = self._read_stats(self._tool_models_path)
        self._tool_models = {
            "since": _number(models.get("since")) or 0,
            "day": models.get("day") if isinstance(models.get("day"), str) else "",
            "today": self._model_rows(models.get("today")),
            "total": self._model_rows(models.get("total")),
        }
        # 独立文件保留升级后的累计数；不能被每日 stats 的清零顺手清掉。
        self._tool_path = os.path.join(root, "tool_results.json")
        try:
            with open(self._tool_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        self._tool_results = {
            "since": _number(data.get("since")) or 0,
            "day": data.get("day") if isinstance(data.get("day"), str) else "",
            "today": self._tool_rows(data.get("today")),
            "total": self._tool_rows(data.get("total")),
        }
        self._tool_details = ToolDetails(root, redact)

    def note(self, kind: str, text: str, scope: str = "", level: str = "info") -> None:
        """记一件事：kind 是类别（收到、回话、跳过、看手机、判断、记忆、出错……）
        ⚠️ 模型那边的报错原文也会记进来：先把像凭据的东西打码（万一对方在报错里回显了 key）"""
        text = redact(text)
        self.items.appendleft({"ts": now_ms(), "kind": kind, "scope": scope, "text": text, "level": level})
        line = f"[MultiChat·{kind}]{'[' + scope + ']' if scope else ''} {text}"
        (self.log.warning if level == "warn" else self.log.info)(line)

    def count(self, key: str, n: int = 1) -> None:
        self._today()
        self.stats[key] = self.stats.get(key, 0) + n
        if key in COUNT_FIELDS:
            self._totals["counts"][key] = (_number(self._totals["counts"].get(key)) or 0) + (_number(n) or 0)
            self._save_json(self._total_path, self._totals)
        self._save()

    def _today(self) -> None:
        day = str(day_of(now_ms()))
        if self.stats.get("day") != day:
            self.stats = {"day": day}

    def _save(self) -> None:
        self._save_json(self._path, self.stats)

    @staticmethod
    def _read_stats(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save_json(path: str, data: dict) -> None:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, allow_nan=False)
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _counts(value) -> dict:
        if not isinstance(value, dict):
            return {}
        return {key: _number(value[key]) or 0 for key in COUNT_FIELDS if key in value}

    @staticmethod
    def _usage_rows(value) -> dict:
        if not isinstance(value, dict):
            return {}
        return {category: {key: _number(row.get(key)) or 0 for key in USAGE_FIELDS}
                for category, row in value.items() if category in CATEGORIES and isinstance(row, dict)}

    def total_snapshot(self) -> dict:
        data = self._totals
        return {"since": data["since"], "importedDay": data["importedDay"],
                "counts": self._counts(data["counts"]), "modelUsage": self._usage_rows(data["modelUsage"])}

    def record_model_usage(self, category: str, sample: dict) -> None:
        """当天各类实际 Provider 请求；未知 token 单列，不用字符估成 token/钱。"""
        self._today()
        category = category if category in CATEGORIES else "其他"
        usage = self.stats.get("model_usage")
        if not isinstance(usage, dict):
            usage = self.stats["model_usage"] = {}
        row = usage.get(category)
        if not isinstance(row, dict):
            row = usage[category] = {}
        fields = USAGE_FIELDS
        # 兼容旧文件和被手工写坏的某一个字段，不能把 NaN / bool 当有效计数。
        for field in fields:
            row[field] = _number(row.get(field)) or 0
        before = dict(row)
        row["requests"] += 1
        for field in ("retries", "failed", "cancelled", "elapsedMs", "inputChars", "outputChars"):
            row[field] += _number(sample.get(field)) or 0
        token_sample = sample.get("tokens")
        tokens = {key: _number(token_sample.get(key)) for key in TOKEN_FIELDS} if isinstance(token_sample, dict) else {}
        tokens = {key: value for key, value in tokens.items() if value is not None}
        known = "inputTokens" in tokens and "outputTokens" in tokens
        row["usageKnown" if known else "usagePartial" if tokens else "usageUnknown"] += 1
        for key, value in tokens.items():
            row[key] += value
            row[key.replace("Tokens", "Known")] += 1
        total = self._totals["modelUsage"].setdefault(category, {})
        for field in fields:
            total[field] = (_number(total.get(field)) or 0) + (row[field] - before[field])
        self._save_json(self._total_path, self._totals)
        self._save()

    def today(self) -> dict:
        if self.stats.get("day") != str(day_of(now_ms())):
            return {}
        return {k: v for k, v in self.stats.items() if k != "day"}

    @staticmethod
    def _tool_rows(value) -> dict:
        """只接收有限的类别和非负整数，不把坏字段或任意内容送去仪表盘。"""
        if not isinstance(value, dict):
            return {}
        return {category: {field: _number(row.get(field)) or 0 for field in TOOL_RESULT_FIELDS}
                for category, row in value.items() if category in CATEGORIES and isinstance(row, dict)}

    def record_tool_result(self, category: str, field: str) -> None:
        if field not in TOOL_RESULT_FIELDS:
            return
        now = now_ms()
        day = str(day_of(now))
        data = self._tool_results
        if not data["since"]:
            data["since"] = now
        if data["day"] != day:
            data["day"], data["today"] = day, {}
        category = category if category in CATEGORIES else "其他"
        for period in ("today", "total"):
            row = data[period].setdefault(category, {key: 0 for key in TOOL_RESULT_FIELDS})
            row[field] += 1
        try:
            tmp = self._tool_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._tool_path)
        except (OSError, ValueError, TypeError):
            pass

    def tool_results_snapshot(self) -> dict:
        data = self._tool_results
        return {"since": data["since"],
                "today": self._tool_rows(data["today"]) if data["day"] == str(day_of(now_ms())) else {},
                "total": self._tool_rows(data["total"])}

    @staticmethod
    def _model_identity(models) -> dict:
        """仅保留来源 ID 和型号；不同模型参与的同一轮单独成组，不把结果张冠李戴。"""
        safe = []
        for item in models if isinstance(models, (list, tuple)) else []:
            if not isinstance(item, dict):
                continue
            provider = redact(item.get("provider", ""))[:160] if isinstance(item.get("provider"), str) else ""
            model = redact(item.get("model", ""))[:160] if isinstance(item.get("model"), str) else ""
            item = {"provider": provider.strip() or "未识别来源", "model": model.strip() or "未提供型号"}
            if item not in safe:
                safe.append(item)
        if not safe:
            safe = [{"provider": "未识别来源", "model": "未调用或未提供型号"}]
        # 顺序保留，区分 A→B 与 B→A；每轮最多三次真实请求。
        safe = safe[:3]
        key = json.dumps([[x["provider"], x["model"]] for x in safe], ensure_ascii=False, separators=(",", ":"))
        return {"id": key, "provider": safe[0]["provider"] if len(safe) == 1 else "多模型任务",
                "model": safe[0]["model"] if len(safe) == 1 else " → ".join(f'{x["provider"]} / {x["model"]}' for x in safe)}

    @staticmethod
    def _model_rows(value) -> dict:
        if not isinstance(value, dict):
            return {}
        rows = {}
        for key, row in value.items():
            if not isinstance(key, str) or len(key) > 2200 or not isinstance(row, dict):
                continue
            if not isinstance(row.get("provider"), str) or not isinstance(row.get("model"), str):
                continue
            safe_key = redact(key)
            rows[safe_key] = {"id": safe_key, "provider": redact(row["provider"])[:160],
                              "model": redact(row["model"])[:1024],
                              **{field: _number(row.get(field)) or 0 for field in TOOL_OUTCOME_FIELDS}}
        return rows

    def record_tool_outcome(self, category: str, outcome: str, models) -> None:
        """每个已结束的必需结果任务只记一个终态，按结束日统计，不把跨日进行中推成失败。"""
        if outcome not in TOOL_OUTCOME_FIELDS:
            return
        now = now_ms()
        data = self._tool_models
        if not data["since"]:
            data["since"] = now
        day = str(day_of(now))
        if data["day"] != day:
            data["day"], data["today"] = day, {}
        identity = self._model_identity(models)
        for period in ("today", "total"):
            row = data[period].setdefault(identity["id"], {**identity, **{k: 0 for k in TOOL_OUTCOME_FIELDS}})
            row[outcome] = (_number(row.get(outcome)) or 0) + 1
        self._save_json(self._tool_models_path, data)

    def tool_models_snapshot(self) -> dict:
        data = self._tool_models
        return {"since": data["since"],
                "today": list(self._model_rows(data["today"]).values()) if data["day"] == str(day_of(now_ms())) else [],
                "total": list(self._model_rows(data["total"]).values())}

    def record_tool_detail(self, category: str, field: str, detail) -> None:
        """阶段原因单独存盘；不回填也不重复增加旧的任务／请求次数。"""
        self._tool_details.record(category, field, detail, now_ms())

    def record_tool_model_details(self, category: str, outcome: str, models, details) -> None:
        """整轮结束时交明细，模型归属与现有终态图使用同一个身份规则。"""
        self._tool_details.record_model(outcome, self._model_identity(models), details, now_ms())

    def tool_details_snapshot(self) -> dict:
        return self._tool_details.snapshot(now_ms())
