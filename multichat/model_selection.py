"""Host-owned model directory and credential-free dashboard selections."""
from __future__ import annotations

from pathlib import Path

from .events import redact
from .model_routes import ROUTES, ROUTE_IDS
from .store import _read, _write


class ModelSelectionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _id(value):
    if (not isinstance(value, str) or len(value) > 200 or value != value.strip()
            or any(ord(c) < 32 for c in value)):
        return None
    return value


def _public(value, limit=200):
    return redact(value.replace("\r", " ").replace("\n", " "))[:limit] if isinstance(value, str) else ""


def provider_label(provider):
    if provider is None:
        return "没有可用模型"
    cfg = getattr(provider, "provider_config", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    model = _public(cfg.get("model"))
    name = _public(cfg.get("id")) or type(provider).__name__
    return f"{name}（{model}）" if model else name


class ModelSelection:
    """Selections override config per purpose; empty means the original host default.

    Only directory IDs are saved. Hosts must return *enabled chat providers* and
    resolve them for the requested conversation, without exposing credentials.
    """

    def __init__(self, data_root, config, directory=None):
        self.directory = directory
        self.path = str(Path(data_root) / "model_selection.json")
        self.initial = self._clean(config.get("request_models", {}))
        saved = _read(self.path, {})
        self.overrides = self._clean(saved.get("routes", {}) if isinstance(saved, dict) else {})

    @staticmethod
    def _clean(values):
        if not isinstance(values, dict):
            return {}
        return {key: value for key, value in values.items() if key in ROUTE_IDS and _id(value) is not None}

    @property
    def supported(self):
        return (callable(getattr(self.directory, "enabled_chat_models", None))
                and callable(getattr(self.directory, "provider_by_id", None)))

    def selected(self, purpose):
        return self.overrides.get(purpose, self.initial.get(purpose, ""))

    def catalog(self, conversation):
        if not self.supported:
            return []
        rows = self.directory.enabled_chat_models(conversation)
        if not isinstance(rows, (list, tuple)):
            raise ModelSelectionError("model_directory_failed", "宿主模型目录暂不可用")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            ident = _id(row.get("id"))
            if not ident or ident in seen or row.get("enabled") is False or row.get("kind", "chat") != "chat":
                continue
            raw = row.get("modalities")
            modalities = sorted({x for x in raw if isinstance(x, str) and x in {"text", "image", "tool_use", "audio", "video"}}) if isinstance(raw, (list, tuple)) else []
            known = row.get("capabilitiesKnown")
            seen.add(ident)
            result.append({"id": ident, "label": _public(row.get("label")) or ident,
                           "model": _public(row.get("model")), "modalities": modalities,
                           "capabilitiesKnown": known if isinstance(known, bool) else bool(modalities)})
        return result

    @staticmethod
    def _compatible(row, purpose):
        required = next((r["requires"] for r in ROUTES if r["id"] == purpose), "")
        return not required or not row["capabilitiesKnown"] or required in row["modalities"]

    def resolve(self, purpose, conversation):
        """Explicit failure is None, never an implicit fallback to another model."""
        ident = self.selected(purpose)
        if not ident or not self.supported:
            return None
        row = next((r for r in self.catalog(conversation) if r["id"] == ident), None)
        if row is None or not self._compatible(row, purpose):
            return None
        provider = self.directory.provider_by_id(ident, conversation)
        return provider if callable(getattr(provider, "complete", None)) else None

    def update(self, routes, conversation):
        if not self.supported:
            raise ModelSelectionError("model_selection_unsupported", "当前宿主未提供可选择的模型目录")
        if not isinstance(routes, dict) or not routes:
            raise ModelSelectionError("invalid_routes", "请提供需要修改的模型用途")
        if any(key not in ROUTE_IDS or _id(value) is None for key, value in routes.items()):
            raise ModelSelectionError("invalid_routes", "模型用途或模型 ID 无效")
        catalog = {row["id"]: row for row in self.catalog(conversation)}
        for purpose, ident in routes.items():
            if not ident:
                continue
            row = catalog.get(ident)
            if row is None:
                raise ModelSelectionError("invalid_model", "所选模型不存在或未启用，请刷新后重试")
            if not self._compatible(row, purpose):
                raise ModelSelectionError("model_capability", "所选模型声明的能力不满足该用途")
            if not callable(getattr(self.directory.provider_by_id(ident, conversation), "complete", None)):
                raise ModelSelectionError("invalid_model", "所选模型暂不可用，请检查宿主设置")
        updated = {**self.overrides, **routes}
        _write(self.path, {"version": 1, "routes": updated})
        self.overrides = updated
