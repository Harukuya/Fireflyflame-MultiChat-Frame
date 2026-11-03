"""Editable plain-text framework resources; no encryption or embedded keys.

The historical module name is retained so resource consumers have one small API.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DataSource:
    """Read UTF-8 resources beneath a configured data directory."""

    encrypted = False

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute():
            raise ValueError("Resource names must be relative to the data directory")
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Resource path escapes the data directory")
        return target

    def read_text(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8-sig")

    def read_json(self, name: str) -> Any:
        return json.loads(self.read_text(name))


def read_json_resource(path: str | Path) -> Any:
    """Load a directly specified JSON resource (also used by isolated tests)."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))
