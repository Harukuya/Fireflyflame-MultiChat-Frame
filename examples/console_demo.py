"""Run without credentials, a network connection or a messaging platform."""
import asyncio
import logging
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from multichat.runtime import Runtime
from multichat.model_port import ModelResponse, ScriptedModelProvider
from multichat.ports import Conversation, IncomingMessage, ConsoleTransport


class DemoTransport(ConsoleTransport):
    def __init__(self):
        super().__init__()
        self.finished = asyncio.Event()

    async def send_text(self, conversation, text, reply_to=None):
        accepted = await super().send_text(conversation, text, reply_to)
        if {row["scope"] for row in self.sent} >= {"p:demo:reader", "g:demo:reading-room"}:
            self.finished.set()
        return accepted


async def main():
    logging.basicConfig(level=logging.ERROR)
    transport = DemoTransport()
    model = ScriptedModelProvider([
        ModelResponse(text="好呀，我们可以从这本书开始。"),
        ModelResponse(text="我也看到了，大家说说最喜欢哪一段？"),
    ])
    with tempfile.TemporaryDirectory(prefix="multichat-demo-") as folder:
        runtime = Runtime(folder, transport, lambda conversation: model,
                          config={"schedule_mode": "always", "dashboard": {"enabled": False}}, assets_dir=ROOT)
        try:
            runtime.register("private", "demo:reader", name="读者")
            runtime.register("group", "demo:reading-room", name="阅读小组", proactive=False)
            print("Offline demonstration. Batch and typing delays are intentional; allow up to 45 seconds.")
            await runtime.start()
            await runtime.receive(IncomingMessage(Conversation("private", "demo:reader"),
                                                  "demo:reader", "读者", "一起读这本书好吗？", "private-1"))
            await runtime.receive(IncomingMessage(Conversation("group", "demo:reading-room"),
                                                  "demo:reader", "读者", "小伴，来聊聊我们刚读的这篇故事。", "group-1", called=True))
            await asyncio.wait_for(transport.finished.wait(), timeout=45)
            print(f"Demo complete: {len(model.requests)} model calls, {len(transport.sent)} local sends; no network.")
        finally:
            await runtime.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
