"""Small loopback-only HTTP adapter for DashboardService, on Runtime's event loop.

Only bundled page assets and the explicit management API are exposed. This is
not a general web server or a remote-access authentication scheme.
"""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from .dashboard_api import DashboardService


HEADER_LIMIT = 16 * 1024
BODY_LIMIT = 64 * 1024
MAX_CLIENTS = 32


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class LocalDashboard:
    def __init__(self, runtime, port=8765):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("dashboard.port 必须是 0～65535 的整数；0 表示自动分配端口")
        self.runtime, self.port = runtime, port
        self.api = DashboardService(runtime)
        self.assets = Path(__file__).resolve().parents[1] / "pages" / "dashboard"
        self._server = None
        self._clients = set()
        self._token = secrets.token_urlsafe(32)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/" if self._server else None

    async def start(self):
        if self._server:
            return
        # Validate installation before listening; no partial running service.
        for name in ("index.html", "app.js", "style.css", "local.js"):
            if not (self.assets / name).is_file():
                raise RuntimeError(f"仪表盘资源缺失：pages/dashboard/{name}")
        try:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", self.port,
                                                       limit=HEADER_LIMIT)
        except OSError as exc:
            raise RuntimeError(
                f"无法启动本地仪表盘 127.0.0.1:{self.port}；端口可能已被占用。"
                "请修改 config.dashboard.port，或设置 dashboard.enabled=false。"
            ) from exc
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self):
        server, self._server = self._server, None
        if server:
            server.close()
        tasks = [task for task in self._clients if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            # Python 3.12 also waits for accepted connections here: cancel their
            # handlers first so a slow model request cannot hold shutdown open.
            await server.wait_closed()

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        if not self._server or len(self._clients) >= MAX_CLIENTS:
            writer.close()
            await writer.wait_closed()
            return
        self._clients.add(task)
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                if len(raw) > HEADER_LIMIT:
                    raise HTTPError(431, "请求头过大")
                status, mime, payload = await self._request(raw, reader)
            except HTTPError as exc:
                status, mime, payload = self._error(exc.status, exc.message)
            except (asyncio.LimitOverrunError, UnicodeError, ValueError):
                status, mime, payload = self._error(400, "无效请求")
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            except TimeoutError:
                status, mime, payload = self._error(408, "请求超时")
            except asyncio.CancelledError:
                raise
            except Exception:
                # No request data, credentials, paths or model exceptions in HTTP output.
                status, mime, payload = self._error(500, "操作未完成，请查看宿主日志")
            headers = {
                "Content-Type": mime, "Content-Length": str(len(payload)),
                "Connection": "close", "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            }
            head = f"HTTP/1.1 {status} {_STATUS.get(status, 'Error')}\r\n" + "".join(
                f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
            writer.write(head.encode("ascii") + payload)
            await asyncio.wait_for(writer.drain(), 5)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._clients.discard(task)

    @staticmethod
    def _error(status, message):
        return status, "application/json; charset=utf-8", json.dumps(
            {"ok": False, "error": {"code": "http_error", "message": message}}, ensure_ascii=False).encode("utf-8")

    async def _request(self, raw, reader):
        lines = raw.decode("ascii").split("\r\n")
        try:
            method, target, protocol = lines[0].split(" ")
        except ValueError:
            raise HTTPError(400, "无效请求行") from None
        if protocol != "HTTP/1.1" or method not in ("GET", "POST"):
            raise HTTPError(405, "仅支持 HTTP/1.1 GET 和 POST")
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            name = name.lower()
            if not sep or not name or name.strip() != name or name in headers:
                raise HTTPError(400, "无效或重复请求头")
            headers[name] = value.strip()
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        host = headers.get("host", "").lower()
        if host not in hosts:
            raise HTTPError(403, "只允许本机仪表盘地址")
        origin = headers.get("origin")
        if origin is not None and origin != "http://" + host:
            raise HTTPError(403, "不允许跨站访问仪表盘")
        if headers.get("sec-fetch-site") == "cross-site":
            raise HTTPError(403, "不允许跨站访问仪表盘")
        if "transfer-encoding" in headers:
            raise HTTPError(400, "不支持分块请求")
        length_text = headers.get("content-length", "0")
        if not length_text.isascii() or not length_text.isdecimal():
            raise HTTPError(400, "无效内容长度")
        length = int(length_text)
        if length > BODY_LIMIT:
            raise HTTPError(413, "请求内容过大")
        if method == "GET" and length:
            raise HTTPError(400, "GET 不接受请求正文")
        url = urlsplit(target)
        if url.scheme or url.netloc or url.fragment or not target.startswith("/"):
            raise HTTPError(400, "需要本地相对路径")
        path = url.path
        if path.startswith("/api/"):
            supplied = headers.get("authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + self._token):
                raise HTTPError(403, "请从本地仪表盘页面发起操作")
            params = {}
            for key, values in parse_qs(url.query, keep_blank_values=True, max_num_fields=32).items():
                if len(values) != 1:
                    raise HTTPError(400, "查询参数不可重复")
                params[key] = values[0]
            body = None
            if method == "POST":
                if headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                    raise HTTPError(415, "操作需要 JSON 请求")
                data = await asyncio.wait_for(reader.readexactly(length), 5)
                try:
                    body = json.loads(data)
                except (ValueError, UnicodeError):
                    raise HTTPError(400, "无效 JSON") from None
            result = await asyncio.wait_for(self.api.handle(method, path[5:], params=params, body=body), 180)
            return 200, "application/json; charset=utf-8", json.dumps(result, ensure_ascii=False).encode("utf-8")
        if method != "GET":
            raise HTTPError(405, "静态页面只支持 GET")
        assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                  "/style.css": "style.css", "/local.js": "local.js"}
        name = assets.get(path)
        if name is None:
            raise HTTPError(404, "没有这个页面")
        data = (self.assets / name).read_bytes()
        if name == "index.html":
            # Inject only in this adapter. The on-disk page stays host-neutral.
            addition = (f'<meta name="multichat-local-token" content="{self._token}">\n'
                        '<script src="/local.js"></script>\n')
            data = data.replace(b"</head>", addition.encode("utf-8") + b"</head>")
        mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[Path(name).suffix]
        return 200, mime + "; charset=utf-8", data


_STATUS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           405: "Method Not Allowed", 408: "Request Timeout", 413: "Content Too Large",
           415: "Unsupported Media Type", 431: "Request Header Fields Too Large", 500: "Internal Server Error"}
