"""
本地向量模型：按意思比，不按字面比（原版 embed.ts）

⭐ 算向量的是插件自带的一个小 Node 进程（embed/server.mjs），和原版用同一个 WASM 运行库，
  所以 data/ 里算好的向量直接能用，所有门槛都不用动。
⭐ 模型用不了时 embed() 返回 None：知识／记忆按字面，人物按顺序，语气不补充；程序照样跑。
⚠️ 一次只算一句：凑批会改变结果。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import subprocess
import threading
from collections import OrderedDict

import numpy as np
from .events import redact

MODEL_ID = "jinaai/jina-embeddings-v2-base-zh"
VECTOR_FORMAT = "wasm-single"


def unpack_vec(s: str) -> np.ndarray:
    """int8 + 一个缩放系数（float32）→ 向量。和原版一样先还原成 float32，再按 float64 算点积。"""
    b = base64.b64decode(s)
    scale = struct.unpack_from("<f", b, 0)[0]
    q = np.frombuffer(b, dtype=np.int8, offset=4).astype(np.float64)
    return (q / scale).astype(np.float32).astype(np.float64)


def pack_vec(v: np.ndarray) -> str:
    """原版的 packVec：用 127/最大值 量化（量化用的是 double 的系数，存的是 float32）。"""
    v = np.asarray(v, dtype=np.float64)
    mx = float(np.max(np.abs(v))) if v.size else 0.0
    scale = 127 / mx if mx > 0 else 1.0
    q = np.floor(v * scale + 0.5).astype(np.int64)
    out = struct.pack("<f", scale) + bytes(((q + 256) % 256).astype(np.uint8).tolist())
    return base64.b64encode(out).decode()


class Embedder:
    """管着那个 Node 进程。请求排队，一次只算一句；同一句话记住最近 16 句。"""

    def __init__(self, logger):
        self.log = logger
        self._proc: subprocess.Popen | None = None
        self._ready = False
        self._failed = False
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._write_lock = threading.Lock()
        self._memo: OrderedDict[str, np.ndarray] = OrderedDict()
        self._ready_event: asyncio.Event | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self, node_path: str, server_js: str, model_dir: str) -> bool:
        self._loop = asyncio.get_running_loop()
        self._ready_event = asyncio.Event()
        need = ["tokenizer.json", "tokenizer_config.json", os.path.join("onnx", "model_quantized.onnx")]
        missing = [n for n in need if not os.path.isfile(os.path.join(model_dir, n))]
        if missing:
            self.log.warning(f"[MultiChat·向量] 模型文件不在 {model_dir}（缺 {', '.join(missing)}），联想和检索只按字面比")
            self._failed = True
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [node_path, server_js, model_dir],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=os.path.dirname(server_js), creationflags=flags,
            )
        except OSError as e:
            self.log.warning(redact(f"[MultiChat·向量] 启动 Node 失败（{node_path}）：{e}。使用降级检索"))
            self._failed = True
            return False
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=120)
        except asyncio.TimeoutError:
            self.log.warning("[MultiChat·向量] 模型加载超时，联想和检索只按字面比")
            self.stop()
            return False
        return self._ready

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if "ready" in msg:
                self._ready = bool(msg["ready"])
                if self._ready:
                    self.log.info(f"[MultiChat·向量] 模型就绪，用了 {msg.get('ms')}ms")
                else:
                    self._failed = True
                    self.log.warning(redact(f"[MultiChat·向量] 模型加载失败：{msg.get('error')}。使用降级检索"))
                self._loop.call_soon_threadsafe(self._ready_event.set)
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None:
                self._loop.call_soon_threadsafe(_resolve, fut, msg)
        # 进程退出了
        self._ready = False
        self._failed = True
        for fut in list(self._pending.values()):
            self._loop.call_soon_threadsafe(_resolve, fut, {"error": "向量进程退出了"})
        self._pending.clear()
        if self._ready_event is not None:
            self._loop.call_soon_threadsafe(self._ready_event.set)

    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.log.debug(redact(f"[MultiChat·向量] {line}"))

    async def _embed_one(self, text: str) -> np.ndarray | None:
        if not self._ready or not self._proc or not self._proc.stdin:
            return None
        hit = self._memo.get(text)
        if hit is not None:
            self._memo.move_to_end(text)
            return hit
        self._next_id += 1
        rid = self._next_id
        fut = self._loop.create_future()
        self._pending[rid] = fut
        line = (json.dumps({"id": rid, "text": text}, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
        except OSError:
            self._pending.pop(rid, None)
            return None
        try:
            msg = await asyncio.wait_for(fut, timeout=30)
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return None
        if "vec" not in msg:
            return None
        v = np.frombuffer(base64.b64decode(msg["vec"]), dtype="<f4").astype(np.float64)
        self._memo[text] = v
        if len(self._memo) > 16:
            self._memo.popitem(last=False)
        return v

    async def embed(self, texts: list[str]) -> list[np.ndarray] | None:
        """几段文字 → 归一化的向量（点积就是余弦相似度）。模型用不了时返回 None。"""
        if not texts:
            return []
        out = []
        for t in texts:
            v = await self._embed_one(t)
            if v is None:
                return None
            out.append(v)
        return out

    def stop(self) -> None:
        self._ready = False
        if self._proc:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.terminate()
            except OSError:
                pass
            self._proc = None


def _resolve(fut: asyncio.Future, msg: dict) -> None:
    if not fut.done():
        fut.set_result(msg)
