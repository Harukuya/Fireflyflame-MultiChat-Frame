"""Memory universe host routes use fake state and an isolated loopback listener."""
import asyncio
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
from multichat.dashboard_api import DashboardService
from multichat.local_dashboard import LocalDashboard
from multichat import universe


def blocked(*args, **kwargs):
    raise AssertionError("A read-only memory route called a mutable helper")


def fixture():
    stamp = int(time.time() * 1000) - 60_000
    memory = {"episodes": [{"id": 1, "start": stamp, "end": stamp,
        "summary": "A synthetic conversation about the sky", "gist": "The sky", "feeling": "calm",
        "weight": 4, "level": 0, "topics": ["sky"], "recalls": 0, "lastRecalledAt": 0}],
        "facts": [], "words": [], "threads": [], "haze": [], "doneTs": 0, "doneMsgId": None}
    scope = "p:service:synthetic"
    row = {"listed": True, "enabled": True, "memory": True, "name": "Synthetic reader"}
    runtime = N(closed=False, _stopped=False, profile_scope=nullcontext,
        memories={scope: N(m=memory, enabled=blocked, expire_low=blocked, fade=blocked,
                          unconsolidated=blocked, store=N(memory=memory, save_memory=blocked))},
        sessions=N(private={scope[2:]: row}, group={}), hub=N(chats={}),
        stores={}, engines={}, scope_label=lambda key: "Synthetic reader",
        events=N(note=blocked), intimacy=N(get=blocked), memory_history=blocked)
    return runtime, memory


class UniverseHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime, self.memory = fixture()
        self.original = copy.deepcopy(self.memory)
        self.server = LocalDashboard(self.runtime, port=0)
        await self.server.start()
        self.port = self.server.port
        status, _, page = await self.request("GET", "/memory-universe/")
        self.assertEqual(status, 200)
        self.token = re.search(rb'name="multichat-local-token" content="([^"]+)"', page).group(1).decode()

    async def asyncTearDown(self):
        await self.server.close()
        self.assertEqual(self.memory, self.original)

    async def request(self, method, path, *, token=False, headers=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        hs = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
        if token:
            hs["Authorization"] = "Bearer " + self.token
        hs.update(headers or {})
        raw = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hs.items()) + "\r\n"
        try:
            writer.write(raw.encode("ascii"))
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(), 3)
        finally:
            writer.close()
            await writer.wait_closed()
        head, body = reply.split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        return int(lines[0].split()[1]), dict(line.lower().split(": ", 1) for line in lines[1:]), body

    async def test_meta_items_detail_keep_complete_protocol(self):
        _, _, body = await self.request("GET", "/api/memory-universe/meta", token=True)
        meta = json.loads(body)
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["schemaVersion"], 1)
        self.assertIsInstance(meta["generatedAt"], (int, float))
        self.assertIn("utcOffsetMinutes", meta)
        _, _, body = await self.request("GET", "/api/memory-universe/items", token=True)
        items = json.loads(body)
        self.assertTrue(items["ok"], items)
        self.assertEqual(items["data"]["page"]["matched"], 1)
        node = items["data"]["items"][0]
        _, _, body = await self.request("GET", "/api/memory-universe/item?" + urlencode({"id": node["id"]}), token=True)
        detail = json.loads(body)
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["data"]["node"]["id"], node["id"])
        self.assertIn("synthetic", detail["data"]["rememberedText"])

    async def test_structured_query_and_missing_item_errors_survive_http(self):
        for params, code in (("limit=invalid", "INVALID_QUERY"), ("limit=1&limit=2", "INVALID_QUERY")):
            status, _, body = await self.request("GET", "/api/memory-universe/items?" + params, token=True)
            result = json.loads(body)
            self.assertEqual(status, 200)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(result["schemaVersion"], 1)
        status, _, body = await self.request("GET", "/api/memory-universe/item?id=missing", token=True)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])

    async def test_all_universe_reads_require_same_local_auth(self):
        for path in DashboardService.UNIVERSE_GET:
            status, _, _ = await self.request("GET", "/api/" + path)
            self.assertEqual(status, 403)
            status, _, _ = await self.request("GET", "/api/" + path, token=True,
                                              headers={"Origin": "https://not-local.invalid"})
            self.assertEqual(status, 403)

    async def test_new_static_files_and_token_injection_stay_page_scoped(self):
        for name in ("index.html", "app.js", "style.css", "universe-api.js", "mock-data.js"):
            status, headers, body = await self.request("GET", "/memory-universe/" + name)
            self.assertEqual(status, 200)
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            if name.endswith(".js"):
                self.assertIn("text/javascript", headers["content-type"])
            if name == "index.html":
                self.assertIn(b'<script src="/local.js">', body)
                self.assertIn(self.token.encode(), body)
        disk = (ROOT / "pages/memory-universe/index.html").read_text(encoding="utf-8")
        self.assertNotIn("multichat-local-token", disk)
        self.assertNotIn('src="/local.js"', disk)

    async def test_static_allowlist_does_not_expose_parent_or_runtime(self):
        for path in ("/memory-universe/../dashboard/index.html", "/memory-universe/%2e%2e/data/knowledge/kb.json",
                     "/memory-universe/README.md", "/memory-universe/serve.py", "/memory-universe/universe.py"):
            status, _, body = await self.request("GET", path)
            self.assertEqual(status, 404)
            self.assertNotIn(self.token.encode(), body)

    async def test_unexpected_read_failure_does_not_write_event_or_leak_data(self):
        with patch.object(universe, "handle", side_effect=RuntimeError("SYNTHETIC_PRIVATE_TEXT")):
            status, _, body = await self.request("GET", "/api/memory-universe/meta", token=True)
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "READ_FAILED")
        self.assertNotIn("SYNTHETIC_PRIVATE_TEXT", body.decode())

    async def test_mutations_are_not_added_to_universe(self):
        service = DashboardService(self.runtime)
        result = await service.handle("POST", "memory-universe/items", body={})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "method_not_allowed")

    async def test_local_navigation_has_no_token_in_href(self):
        _, _, body = await self.request("GET", "/local.js")
        self.assertIn(b'"memory-universe": "/memory-universe/"', body)
        self.assertNotIn(self.token.encode(), body)
        _, _, page = await self.request("GET", "/")
        self.assertIn(b'id="memory-universe-link"', page)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
