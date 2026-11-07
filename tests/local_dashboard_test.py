"""Real loopback HTTP with a fake model/platform and isolated temporary data."""
import asyncio
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
from multichat.local_dashboard import LocalDashboard
from multichat.model_port import ScriptedModelProvider
from multichat.ports import ConsoleTransport
from multichat.runtime import Runtime


class LocalDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multichat-local-http-")
        self.instances = []
        self.runtime = self.make()
        await self.runtime.start()
        self.port = self.runtime._dashboard.port
        status, _, html = await self.request("GET", "/")
        self.assertEqual(status, 200)
        self.token = re.search(r'name="multichat-local-token" content="([^"]+)"', html.decode()).group(1)

    def make(self, config=None):
        provider = ScriptedModelProvider([])
        runtime = Runtime(Path(self.temp.name) / str(len(self.instances)), ConsoleTransport(), lambda _: provider,
                          {"schedule_mode": "always", "dashboard": {"port": 0}, **(config or {})},
                          assets_dir=ROOT, logger=logging.getLogger("test.local"))
        self.instances.append(runtime)
        return runtime

    async def asyncTearDown(self):
        for runtime in self.instances:
            await runtime.close()
        self.temp.cleanup()

    async def raw(self, data):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(data)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 3)
        finally:
            writer.close()
            await writer.wait_closed()
        head, body = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        return int(lines[0].split()[1]), dict(line.lower().split(": ", 1) for line in lines[1:]), body

    async def request(self, method, path, body=None, *, headers=None, authenticated=False):
        payload = json.dumps(body).encode() if body is not None else b""
        hs = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
        if authenticated:
            hs["Authorization"] = "Bearer " + self.token
        if body is not None:
            hs.update({"Content-Type": "application/json", "Content-Length": str(len(payload))})
        hs.update(headers or {})
        data = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k,v in hs.items()) + "\r\n"
        return await self.raw(data.encode("ascii") + payload)

    async def test_page_assets_and_host_bridge_stays_untouched(self):
        for path in ("/", "/index.html", "/style.css", "/app.js", "/local.js", "/models.js", "/models.css"):
            status, headers, body = await self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body)
            self.assertEqual(headers['cache-control'], 'no-store')
            self.assertEqual(headers['x-frame-options'], 'deny')
        text = (ROOT/'pages/dashboard/index.html').read_text(encoding='utf-8')
        self.assertNotIn('multichat-local-token', text)
        self.assertNotIn('src="/local.js"', text)

    async def test_runtime_start_defaults_to_local_port(self):
        r = self.make({"dashboard": {}})
        captured = []
        real_start = LocalDashboard.start
        async def allocated(server):
            captured.append(server.port)
            server.port = 0  # Verify default without depending on a user's free 8765.
            await real_start(server)
        with patch.object(LocalDashboard, "start", allocated):
            await r.start()
        self.assertEqual(captured, [8765])
        self.assertRegex(r.dashboard_url, r'^http://127\.0\.0\.1:\d+/$')
        self.assertEqual(r._dashboard._server.sockets[0].getsockname()[0], '127.0.0.1')

    async def test_gets_use_real_dashboard_service(self):
        for path in ('overview', 'sessions', 'memory', 'intimacy', 'sticker', 'models'):
            status, _, body = await self.request('GET', '/api/'+path, authenticated=True)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['ok'], path)

    async def test_write_and_reopen_read_from_real_runtime(self):
        status, _, body = await self.request('POST', '/api/sessions/add',
            {'kind': 'group', 'id': 'room:local', 'name': '演示', 'proactive': False}, authenticated=True)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])
        self.assertIn('room:local', self.runtime.sessions.group)
        status, _, body = await self.request('GET', '/api/sessions', authenticated=True)
        self.assertEqual(json.loads(body)['rows'][0]['id'], 'room:local')
        _, _, body = await self.request('POST', '/api/sessions/update',
            {'kind': 'group', 'id': 'room:local', 'field': 'memory', 'value': False}, authenticated=True)
        self.assertTrue(json.loads(body)['ok'])
        self.assertFalse(self.runtime.sessions.group['room:local']['memory'])

    async def test_service_error_reaches_browser(self):
        _, _, body = await self.request('POST', '/api/sessions/add',
            {'kind':'group', 'id':True}, authenticated=True)
        self.assertFalse(json.loads(body)['ok'])
        _, _, body = await self.request('GET', '/api/unknown', authenticated=True)
        self.assertEqual(json.loads(body)['error']['code'], 'not_found')

    async def test_all_api_reads_and_writes_require_local_token(self):
        for method, path, body in [('GET','/api/overview',None),('POST','/api/reload',{}),
                                   ('GET','/api/models',None),('POST','/api/models/update',{'routes':{'summary':''}})]:
            status, _, _ = await self.request(method,path,body)
            self.assertEqual(status,403)
            status, _, _ = await self.request(method,path,body,headers={'Authorization':'Bearer fake-only'})
            self.assertEqual(status,403)

    async def test_external_origin_and_dns_rebinding_rejected(self):
        for headers in ({'Host':'attacker.invalid:'+str(self.port)},
                        {'Origin':'https://attacker.invalid'}, {'Origin':'null'},
                        {'Sec-Fetch-Site':'cross-site'}):
            status, _, _ = await self.request('GET','/',headers=headers)
            self.assertEqual(status,403,headers)
            status, _, _ = await self.request('POST','/api/reload',{},headers=headers,authenticated=True)
            self.assertEqual(status,403,headers)

    async def test_local_origin_and_no_cors_headers(self):
        status, headers, _ = await self.request('GET','/api/overview',authenticated=True,
            headers={'Origin':f'http://127.0.0.1:{self.port}', 'Sec-Fetch-Site':'same-origin'})
        self.assertEqual(status,200)
        self.assertNotIn('access-control-allow-origin',headers)

    async def test_static_allowlist_blocks_paths_and_source_data(self):
        for path in ('/../config.example.json','/%2e%2e/config.example.json','/data/knowledge/kb.json',
                     '/multichat/runtime.py','/favicon.ico','//attacker.invalid/foo'):
            status, _, body = await self.request('GET',path)
            self.assertIn(status,(400,404),path)
            self.assertNotIn(self.token.encode(),body)

    async def test_post_type_and_json_validation(self):
        status, _, _ = await self.request('POST','/api/reload',{},authenticated=True,
                                         headers={'Content-Type':'text/plain'})
        self.assertEqual(status,415)
        status, _, _ = await self.raw((f'POST /api/reload HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
            f'Authorization: Bearer {self.token}\r\nContent-Type: application/json\r\nContent-Length: 1\r\n\r\n'+'x').encode())
        self.assertEqual(status,400)

    async def test_oversize_duplicate_and_chunked_requests_rejected(self):
        requests = [('Content-Length: 65537\r\n',413),
                    ('Host: localhost:1\r\n',400), ('Transfer-Encoding: chunked\r\n',400)]
        for extra, expected in requests:
            raw=(f'POST /api/reload HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n{extra}\r\n').encode()
            status, _, _ = await self.raw(raw)
            self.assertEqual(status, expected)

    async def test_port_conflict_does_not_start_heartbeat(self):
        other = self.make({'dashboard':{'port':self.port}})
        with self.assertRaisesRegex(RuntimeError, 'dashboard.port'):
            await other.start()
        self.assertIsNone(other._heartbeat_task)
        self.assertIsNone(other.dashboard_url)
        other.config['dashboard']['port'] = 0
        await other.start()
        self.assertIsNotNone(other.dashboard_url)

    async def test_disabled_service_and_idempotent_start_close(self):
        other = self.make({'dashboard':{'enabled':False}})
        await other.start()
        self.assertIsNone(other.dashboard_url)
        task = other._heartbeat_task
        await other.start()
        self.assertIs(task,other._heartbeat_task)
        await other.close()
        await other.close()
        self.assertTrue(task.done())

    async def test_close_cancels_blocked_request_and_releases_port(self):
        entered = asyncio.Event()
        async def blocked(*a, **kw):
            entered.set()
            await asyncio.Event().wait()
        service = self.runtime._dashboard
        with patch.object(service.api, 'handle', blocked):
            request = asyncio.create_task(self.request('GET','/api/overview',authenticated=True))
            await asyncio.wait_for(entered.wait(),2)
            await asyncio.wait_for(self.runtime.close(),2)
            await asyncio.gather(request,return_exceptions=True)
        self.assertEqual(service._clients,set())
        self.assertIsNone(self.runtime.dashboard_url)
        check = await asyncio.start_server(lambda r,w:w.close(),'127.0.0.1',self.port)
        check.close()
        await check.wait_closed()

    async def test_concurrent_start_starts_only_one_server(self):
        other = self.make()
        await asyncio.gather(other.start(), other.start(), other.start())
        self.assertIsNotNone(other.dashboard_url)
        await asyncio.gather(other.close(),other.close())
        self.assertIsNone(other.dashboard_url)

    async def test_configuration_validation(self):
        for dashboard in (None,False,{'enabled':'true'},{'port':True},{'port':-1},{'port':65536}):
            other=self.make({'dashboard':dashboard})
            with self.assertRaises(ValueError):
                await other.start()
            self.assertIsNone(other._heartbeat_task)

    async def test_restart_finished_heartbeat_reuses_dashboard(self):
        server = self.runtime._dashboard
        url = self.runtime.dashboard_url
        old = self.runtime._heartbeat_task
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        await self.runtime.start()
        self.assertIs(self.runtime._dashboard, server)
        self.assertEqual(self.runtime.dashboard_url, url)
        self.assertIsNot(self.runtime._heartbeat_task, old)
        status, _, _ = await self.request('GET','/api/overview',authenticated=True)
        self.assertEqual(status, 200)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]],verbosity=2)
