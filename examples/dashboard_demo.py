"""Local browser dashboard with fake models and a temporary data directory."""
import argparse
import asyncio
import logging
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from multichat.runtime import Runtime
from multichat.ports import ConsoleTransport
from multichat.model_port import ModelResponse, ScriptedModelProvider


async def main(port):
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="multichat-dashboard-demo-") as folder:
        model = ScriptedModelProvider([ModelResponse(text="这是本地演示回复。") for _ in range(20)])
        runtime = Runtime(folder, ConsoleTransport(), lambda conversation: model,
                          {"schedule_mode": "always", "dashboard": {"enabled": True, "port": port}},
                          assets_dir=ROOT)
        try:
            runtime.register("private", "demo:reader", name="演示读者")
            runtime.register("group", "demo:reading-room", name="演示阅读小组", proactive=False)
            await runtime.start()
            print(f"浏览器打开：{runtime.dashboard_url}", flush=True)
            print("假模型、假平台和临时数据；不会连接真实聊天平台。按 Ctrl+C 退出。", flush=True)
            await asyncio.Event().wait()
        finally:
            await runtime.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Local port; 0 selects a free port")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port))
    except KeyboardInterrupt:
        pass
