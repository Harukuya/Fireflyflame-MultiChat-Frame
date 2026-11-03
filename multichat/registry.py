"""Explicit, persistent session registry. No platform whitelist assumptions."""
from .store import _read, _write
from .validation import entity_id
import os


class Registry:
    whitelist_on = False

    def __init__(self, root):
        self.path = os.path.join(root, "sessions.json")
        data = _read(self.path, {})
        self.private = data.get("private", {})
        self.group = data.get("group", {})

    def save(self):
        _write(self.path, {"private": self.private, "group": self.group})

    def book(self, kind):
        if kind not in ("private", "group"):
            raise ValueError("kind must be private or group")
        return self.private if kind == "private" else self.group

    def add(self, kind, sid, *, name="", memory=True, proactive=False):
        if not entity_id(sid):
            raise ValueError("Invalid conversation ID")
        row = self.book(kind).setdefault(sid, {"enabled": True, "memory": memory, "proactive": proactive})
        row["listed"] = True
        if name:
            row["name"] = str(name)[:200]
        self.save()

    def update(self, kind, sid, field, value):
        row = self.book(kind).get(sid)
        if row is None or field not in {"name", "enabled", "memory", "proactive"}:
            return False
        if not isinstance(value, str if field == "name" else bool):
            return False
        row[field] = value[:200] if field == "name" else value
        self.save()
        return True

    def remove(self, kind, sid):
        if sid in self.book(kind):
            self.book(kind)[sid]["listed"] = False
            self.save()

    def private_on(self, sid):
        row = self.private.get(sid, {})
        return bool(row.get("listed") and row.get("enabled"))

    def group_on(self, sid):
        row = self.group.get(sid, {})
        return bool(row.get("listed") and row.get("enabled"))

    def proactive(self, sid):
        return self.group_on(sid) and bool(self.group[sid].get("proactive"))

    def memory_on(self, scope):
        kind, sid = scope.split(":", 1)
        row = (self.private if kind == "p" else self.group).get(sid, {})
        return bool(row.get("listed") and row.get("enabled") and row.get("memory"))
