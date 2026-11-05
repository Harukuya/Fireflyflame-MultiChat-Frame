"""Platform boundary. Implement these ports; core never imports a chat SDK."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class Conversation:
    kind: Literal["private", "group"]
    id: str

    @property
    def scope(self) -> str:
        return ("p:" if self.kind == "private" else "g:") + self.id


@dataclass
class IncomingMessage:
    conversation: Conversation
    sender_id: str
    sender_name: str
    text: str = ""
    message_id: str | None = None
    called: bool = False
    attachments: list[dict] = field(default_factory=list)
    # Host-verified first-level quoted message; no SDK objects, URLs or files.
    quote: dict | None = None


class Transport(Protocol):
    """Return True only after the platform accepted delivery; False on failure.

    reply_to is an original platform message ID; adapters without quotes may
    ignore it. image_path is a local file. Implement unsupported image/typing
    methods as False/no-op. Authorization and bot-loop filtering live outside.
    """
    async def send_text(self, conversation: Conversation, text: str,
                        reply_to: str | None = None) -> bool: ...
    async def send_image(self, conversation: Conversation, image_path: str) -> bool: ...
    async def typing(self, conversation: Conversation) -> None: ...


class ConsoleTransport:
    """Offline demonstration: no external messages, no credentials."""
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, conversation, text, reply_to=None):
        self.sent.append({"scope": conversation.scope, "text": text, "reply_to": reply_to})
        print(f"[{conversation.scope}] {text}")
        return True

    async def send_image(self, conversation, image_path):
        self.sent.append({"scope": conversation.scope, "image": image_path})
        print(f"[{conversation.scope}] image: {image_path}")
        return True

    async def typing(self, conversation):
        pass
