"""Platform-independent composition root for the multichat state machines.

Providers are synchronous selectors ``Conversation | None -> ModelProvider``;
model requests themselves are async. Register sessions explicitly, then pass
IncomingMessage objects to receive(). Importing this module needs no chat SDK.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import shutil
import uuid
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from . import persona, profile
from .caption import Captioner
from .data_bundle import DataSource
from .embed import Embedder
from .engine import Engine
from .events import Events, redact
from .group import GroupChat, GroupHub
from .intimacy import Intimacy
from .jsutil import now_ms
from .knowledge import Knowledge
from .llm import Llm
from .memory import Memory
from .model_routes import ROUTES, ROUTE_IDS, current_purpose, pin_provider
from .model_selection import ModelSelection, ModelSelectionError, provider_label
from .people import People
from .ports import Conversation, IncomingMessage
from .recent import RecentPool
from .quotes import normalize_quote
from .registry import Registry
from .stickers import Stickers
from .store import Her, Store
from .style import Style
from .validation import entity_id


class _PrivateSender:
    def __init__(self, runtime, sid):
        self.runtime, self.conversation = runtime, Conversation("private", sid)

    async def send(self, text):
        return await self.runtime._send(self.conversation, text=text)

    async def send_image(self, path):
        return await self.runtime._send(self.conversation, image=path)

    async def typing(self):
        if self.runtime._enabled(self.conversation) and not self.runtime._stopped:
            try:
                await self.runtime.transport.typing(self.conversation)
            except Exception as error:
                self.runtime._error("Typing notification", error, self.conversation.scope)


class _GroupSender:
    def __init__(self, runtime):
        self.runtime = runtime

    async def send(self, gid, text, reply_to=None):
        return await self.runtime._send(Conversation("group", gid), text=text, reply_to=reply_to)

    async def send_image(self, gid, path):
        return await self.runtime._send(Conversation("group", gid), image=path)


class _PrivateEngine(Engine):
    """Registry switch only gates independent outreach; ongoing follow-ups remain."""
    def __init__(self, *args, proactive_on, **kwargs):
        self._proactive_on = proactive_on
        super().__init__(*args, **kwargs)

    def _should_reach_out(self, now):
        return self._proactive_on() and super()._should_reach_out(now)


class Runtime:
    def __init__(self, data_root, transport, provider_for, config=None, assets_dir=None,
                 judge_provider_for=None, caption_provider_for=None, *, embedder=None, logger=None,
                 model_directory=None):
        self.config = deepcopy(config or {})
        self.assets_dir = Path(assets_dir or Path(__file__).resolve().parent.parent).resolve()
        self.data_root = str(Path(data_root).resolve())
        Path(self.data_root).mkdir(parents=True, exist_ok=True)
        self.images_dir = Path(self.data_root, "images")
        self.images_dir.mkdir(exist_ok=True)
        self.transport = transport
        self._provider_for = provider_for
        self._judge_provider_for = judge_provider_for or provider_for
        self._caption_provider_for = caption_provider_for
        self._model_selection = ModelSelection(self.data_root, self.config, model_directory)
        self.log = logger or logging.getLogger("multichat")
        self.embedder = embedder or Embedder(self.log)  # Literal fallback; no Node process started.
        self._owns_embedder = embedder is None
        self._stopped = False
        self._receiving = 0
        self._receivers: dict[asyncio.Task, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._recalled: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._background: dict[str, asyncio.Task] = {}
        self._heartbeat_task = None
        self._lifecycle_lock = asyncio.Lock()
        self._dashboard = None
        self._llms, self._judge_llms = {}, {}
        self.stores, self.memories, self.engines = {}, {}, {}
        self._data_error = ""
        self.profile_state, self.knowledge, self.people, self.style = self._prepare_data()
        with self.profile_scope():
            self.her = Her(self.data_root)
            self.events = Events(self.data_root, self.log)
            self.intimacy = Intimacy(self.data_root, self.trailblazer)
            self.sessions = Registry(self.data_root)
            self.stickers = Stickers(self.data_root, str(self.assets_dir / "stickers"))
            self.captioner = Captioner(str(self.images_dir), self.log, self.events)
            self._recent_pool = RecentPool(self._recent_sources)
            self.hub = GroupHub(
                self.her, self.llm_for(""), self.judge_llm_for(""), self.knowledge, self.people,
                self.style, self.stickers, self.intimacy, _GroupSender(self), self.events,
                self.trailblazer, self.options, self.group_name, self._on_primary_talk,
                self._recall_for_group, proactive_on=self.sessions.proactive)
            self.hub.captions = self._group_captions
            self.hub.llm_for = lambda gid: self.llm_for(self.group_umo(gid))
            self.hub.judge_for = lambda gid: self.judge_llm_for(self.group_umo(gid))
            self.hub.round_llm_for = self.hub.llm_for
            self.hub.embedder = self.embedder
            self.hub.recent_context = self._recent_context
            self._restore_sessions()

    @property
    def trailblazer(self):
        return entity_id(self.config.get("primary_user_id", ""))

    def profile_scope(self):
        return profile.use(self.profile_state)

    def replica_dir(self):
        return str(self.assets_dir)

    def data_dir(self):
        return str(self.assets_dir / "data")

    def _prepare_data(self):
        prepared = profile.prepare(self.config, self.assets_dir / "prompts")
        source = DataSource(self.data_dir())
        self_path = "self/她对自己的认知.md"
        prepared["self_block"] = persona.build_self_block(source.read_text(self_path)) if (Path(self.data_dir()) / self_path).is_file() else None
        with profile.use(prepared):
            knowledge = Knowledge("", self.embedder, self.log, data=source.read_json("knowledge/kb.json"))
            people = People("", self.embedder, data=source.read_json("people.json"))
            style = Style("", self.embedder, data=source.read_json("style.json"))
        return prepared, knowledge, people, style

    @staticmethod
    def _conversation(scope):
        if not scope:
            return None
        if isinstance(scope, Conversation):
            return scope
        kind, sep, sid = str(scope).partition(":")
        if not sep or kind not in ("p", "g") or not entity_id(sid):
            raise ValueError("Invalid conversation scope")
        return Conversation("private" if kind == "p" else "group", sid)

    @staticmethod
    def _validated(kind, sid):
        if kind not in ("private", "group") or not entity_id(sid) or entity_id(sid) != sid:
            raise ValueError("Invalid conversation identifier")
        return Conversation(kind, sid)

    def private_umo(self, sid):
        return self._validated("private", sid).scope

    def group_umo(self, sid):
        return self._validated("group", sid).scope

    def trailblazer_umo(self):
        return self.private_umo(self.trailblazer) if self.trailblazer else ""

    def _default_provider(self, conversation, purpose):
        if purpose == "caption":
            return self._caption_provider_for(conversation) if self._caption_provider_for else None
        if purpose in {"window_judge", "look_judge"}:
            return self._judge_provider_for(conversation)
        return self._provider_for(conversation)

    def model_provider(self, umo=None, purpose=None):
        conversation = self._conversation(umo)
        purpose = purpose or current_purpose() or ("group_reply" if conversation and conversation.kind == "group" else "private_reply")
        if purpose not in ROUTE_IDS:
            raise ValueError("Unknown model purpose")

        def resolve():
            if self._model_selection.selected(purpose):
                try:
                    return self._model_selection.resolve(purpose, conversation)
                except Exception as error:
                    # The host directory may fail; never leak its exception or
                    # silently call the default model after an explicit choice.
                    self.log.warning("Model directory lookup failed (%s)", type(error).__name__)
                    return None
            return self._default_provider(conversation, purpose)

        return pin_provider((id(self), conversation.scope if conversation else "", purpose), resolve)

    def chat_provider(self, umo=None, purpose=None):
        return self.model_provider(umo, purpose)

    def judge_provider(self, umo=None, purpose=None):
        return self.model_provider(umo, purpose or current_purpose() or "look_judge")

    def models_view(self):
        conversation = self._conversation(self.trailblazer_umo())
        supported = self._model_selection.supported
        note = "模型名称以主要联系人或宿主默认会话为样例，实际请求仍按所在会话解析。空选项严格沿用该用途原有宿主默认；保存从下一次业务请求生效。"
        try:
            providers = self._model_selection.catalog(conversation)
        except Exception:
            providers = []
            note += " 宿主模型目录暂不可用，已保存选择不会自动回退。"
        if not supported:
            note += " 当前宿主未提供模型目录，请在宿主配置模型。"
        rows = []
        for route in ROUTES:
            selected = self._model_selection.selected(route["id"])
            try:
                provider = (self._model_selection.resolve(route["id"], conversation) if selected
                            else self._default_provider(conversation, route["id"]))
            except Exception:
                provider = None
            rows.append({**route, "selected": selected, "effective": provider_label(provider),
                         "available": provider is not None, "defaultLabel": "跟随宿主会话默认"})
        return {"supported": supported, "providers": providers, "routes": rows, "note": note}

    def update_model_routes(self, routes):
        if self._stopped:
            raise ModelSelectionError("closed", "框架已停止，不能修改模型")
        self._model_selection.update(routes, self._conversation(self.trailblazer_umo()))
        return {**self.models_view(), "message": "模型用途已保存，将在下一次业务请求生效"}

    def llm_for(self, umo):
        if umo not in self._llms:
            self._llms[umo] = Llm(lambda: self.chat_provider(umo), self.log, events=self.events)
        return self._llms[umo]

    def judge_llm_for(self, umo):
        if umo not in self._judge_llms:
            self._judge_llms[umo] = Llm(lambda: self.judge_provider(umo), self.log, events=self.events)
        return self._judge_llms[umo]

    def caption_provider(self, umo, llm, *, force_text=False):
        if not force_text and llm.supports("image"):
            return None
        return self.model_provider(umo, "caption")

    def _private_captions(self, sid):
        def see(msgs, saved, deadline=None, *, force_text=False):
            scope = self.private_umo(sid)
            return self.captioner.ready(self.caption_provider(scope, self.llm_for(scope), force_text=force_text), msgs, saved, deadline)
        return see

    def _group_captions(self, gid, msgs, saved, deadline=None, *, force_text=False):
        scope = self.group_umo(gid)
        return self.captioner.ready(self.caption_provider(scope, self.llm_for(scope), force_text=force_text), msgs, saved, deadline)

    def options(self):
        def number(name, default, minimum=0):
            try:
                value = float(self.config.get(name, default))
                return max(minimum, value) if value < float("inf") else default
            except (TypeError, ValueError):
                return default
        return {"split_sentences": bool(self.config.get("split_sentences", False)), "trailblazer": self.trailblazer,
                "decline_trailblazer": bool(self.config.get("decline_primary", True)),
                "offline_reply": bool(self.config.get("offline_reply", True)),
                "offline_text_rest": self.config.get("offline_text_rest") or f"{profile.bot_name()}正在休息中",
                "offline_text_mission": self.config.get("offline_text_busy") or f"{profile.bot_name()}正在忙碌中",
                "mission_ack_text": self.config.get("busy_ack_text") or "现在正忙，等有空了再回复。",
                "look_per_hour": number("look_per_hour", 1.5),
                "look_block_minutes": int(number("look_block_minutes", 60, 1)),
                "look_block_max": int(number("look_block_max", 2))}

    def _enabled(self, conversation):
        return self.sessions.private_on(conversation.id) if conversation.kind == "private" else self.sessions.group_on(conversation.id)

    def _require_enabled(self, kind, sid):
        conversation = self._validated(kind, sid)
        if self._stopped:
            raise RuntimeError("Runtime is closed")
        if not self._enabled(conversation):
            raise ValueError("Conversation is not registered and enabled")
        return conversation

    def register(self, kind, sid, *, name="", memory=True, proactive=False):
        with self.profile_scope():
            self._validated(kind, sid)
            if self._stopped:
                raise RuntimeError("Runtime is closed")
            self.sessions.add(kind, sid, name=name, memory=memory, proactive=proactive)
            if self._enabled(Conversation(kind, sid)):
                return self.engine(sid) if kind == "private" else self.chat(sid)
            return None

    def _restore_sessions(self):
        for kind in ("private", "group"):
            for sid in list(self.sessions.book(kind)):
                if self._enabled(Conversation(kind, sid)):
                    (self.engine if kind == "private" else self.chat)(sid)

    def group_name(self, gid):
        return self.sessions.group.get(gid, {}).get("name") or gid

    def scope_label(self, scope):
        conversation = self._conversation(scope)
        if conversation is None:
            return ""
        row = self.sessions.book(conversation.kind).get(conversation.id, {})
        name = row.get("name") or (profile.primary_name() if conversation.kind == "private" and conversation.id == self.trailblazer else conversation.id)
        return ("私聊·" if conversation.kind == "private" else "群·") + name

    def _store(self, scope):
        conversation = self._conversation(scope)
        if conversation is None:
            raise ValueError("A conversation is required")
        if scope not in self.stores:
            # IDs are opaque platform strings, never filesystem path components.
            digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
            folder = Path(self.data_root, "conversations", conversation.kind, digest)
            st = Store(str(folder), self.her)
            st.images = str(self.images_dir)
            st.umo = scope
            st.save_state()
            self.stores[scope] = st
        return self.stores[scope]

    def _memory(self, scope):
        if scope not in self.memories:
            self.memories[scope] = Memory(self._store(scope), self.embedder, self.log,
                                         enabled=lambda: self.sessions.memory_on(scope),
                                         others=lambda: self._others_for(scope))
        return self.memories[scope]

    def _others_for(self, scope):
        if not self.config.get("share_memory", True):
            return []
        out = []
        for source, mem in self.memories.items():
            if source == scope or not self.sessions.memory_on(source):
                continue
            note = "（这是私聊里的事，不要向无关的人转述）" if source.startswith("p:") else ""
            out.append((mem, self.scope_label(source), note))
        return out

    def memory_history(self, scope):
        if scope.startswith("g:") and scope[2:] in self.hub.chats:
            return self.hub.chats[scope[2:]].memory_session()
        return self._store(scope).messages

    def engine(self, sid):
        with self.profile_scope():
            self._require_enabled("private", sid)
            row = self.sessions.private[sid]
            if sid not in self.engines:
                scope = self.private_umo(sid)
                peer = None if sid == self.trailblazer else {"qq": sid, "name": row.get("name") or sid}
                self.engines[sid] = _PrivateEngine(
                    self._store(scope), self.llm_for(scope), self._memory(scope),
                    self.knowledge, self.people, self.style, _PrivateSender(self, sid), self.log,
                    failure_notice=bool(self.config.get("failure_notice", True)), peer=peer,
                    events=self.events, stickers=self.stickers, intimacy=self.intimacy,
                    options=self.options, scope=scope, captions=self._private_captions(sid),
                    proactive_on=lambda: self.sessions.private_on(sid) and bool(self.sessions.private.get(sid, {}).get("proactive")))
            engine = self.engines[sid]
            if engine.peer:
                engine.peer["name"] = row.get("name") or sid
            engine.recent_context = lambda query, speaker_uid="": self._recent_context(
                self.private_umo(sid), query, speaker_uid=speaker_uid or sid)
            return engine

    def chat(self, gid):
        with self.profile_scope():
            self._require_enabled("group", gid)
            if gid not in self.hub.chats:
                scope = self.group_umo(gid)
                self.hub.chats[gid] = GroupChat(gid, self.hub, self._store(scope), self._memory(scope))
            return self.hub.chats[gid]

    def _recent_sources(self):
        if self._stopped:
            return []
        sources = [(self.private_umo(sid), e) for sid, e in self.engines.items() if self.sessions.private_on(sid) and not e.closed]
        sources += [(self.group_umo(gid), c) for gid, c in self.hub.chats.items() if self.sessions.group_on(gid) and not c.closed]
        return [{**source.recent_snapshot(), "scope": scope, "label": self.scope_label(scope)} for scope, source in sources]

    def _recent_context(self, scope, query, *, speaker_uid=""):
        if self._stopped:
            return ""
        if not self.config.get("share_recent", True):
            # Disabling sharing must not erase this group's own short-term notes.
            local = RecentPool(lambda: [source for source in self._recent_sources() if source["scope"] == scope])
            return local.context(scope, query, speaker_uid=speaker_uid)
        return self._recent_pool.context(scope, query, speaker_uid=speaker_uid)

    def _on_primary_talk(self, depth):
        if self.trailblazer and self.sessions.private_on(self.trailblazer):
            store = self._store(self.private_umo(self.trailblazer))
            store.extra["groupTalkAt"] = now_ms()
            store.save_state()

    async def _recall_for_group(self, memory, about, msgs):
        recalled, commit = await memory.recall_deferred(about, msgs, now_ms())
        past = await self.knowledge.recall(about)
        return ((past if recalled == "想不起来。" else f"{recalled}\n{past}") if past else recalled), commit

    def _error(self, action, error, scope=""):
        self.events.note("出错", redact(f"{action}: {type(error).__name__}: {error}"), level="warn", scope=scope)

    async def _send(self, conversation, *, text=None, image=None, reply_to=None):
        if self._stopped or not self._enabled(conversation):
            return False
        try:
            if image is None:
                accepted = await self.transport.send_text(conversation, text or "", reply_to=reply_to)
            else:
                accepted = await self.transport.send_image(conversation, str(image))
            return accepted is True
        except Exception as error:
            self._error("Send failed", error, conversation.scope)
            return False

    def add_image(self, source_path):
        """An adapter explicitly supplies a local image; copy it into runtime-owned storage."""
        if self._stopped:
            raise RuntimeError("Runtime is closed")
        source = Path(source_path).resolve(strict=True)
        suffix = source.suffix.lower()
        if not source.is_file() or suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            raise ValueError("Unsupported image file")
        target = self.images_dir / (uuid.uuid4().hex + suffix)
        shutil.copyfile(source, target)
        return {"kind": "image", "file": target.name, "mediaType": mimetypes.guess_type(target.name)[0] or "image/jpeg"}

    def _attachments(self, values):
        out = []
        for value in values:
            if not isinstance(value, dict) or value.get("kind") != "image":
                raise ValueError("Only imported image attachments are supported")
            name = value.get("file")
            if not isinstance(name, str) or not name or any(c in name for c in ("/", "\\", ":")) or Path(name).name != name:
                raise ValueError("Attachment file must be a basename returned by add_image")
            path = (self.images_dir / name).resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(self.images_dir.resolve()):
                raise ValueError("Attachment escapes image storage")
            out.append({"kind": "image", "file": name, "mediaType": mimetypes.guess_type(name)[0] or "image/jpeg"})
        return out

    async def receive(self, message: IncomingMessage):
        with self.profile_scope():
            conversation = self._validated(message.conversation.kind, message.conversation.id)
            if self._stopped or not self._enabled(conversation):
                return False
            if not entity_id(message.sender_id) or not isinstance(message.text, str):
                raise ValueError("Message sender and text are invalid")
            if conversation.kind == "private" and message.sender_id != conversation.id:
                raise ValueError("Private conversation ID must identify its sender")
            attachments = self._attachments(message.attachments)
            quote = normalize_quote(getattr(message, "quote", None))
            called = bool(message.called or (quote and quote.get("role") == "her"))
            if not message.text.strip() and not attachments and quote is None:
                return False
            qid = str(message.message_id) if message.message_id is not None else None
            key = (conversation.scope, qid)
            task = asyncio.current_task()
            self._receiving += 1
            self._receivers[task] = conversation.scope
            try:
                async with self._locks.setdefault(conversation.scope, asyncio.Lock()):
                    if self._stopped or not self._enabled(conversation) or (qid is not None and key in self._recalled):
                        return False
                    if conversation.kind == "private":
                        if message.sender_name:
                            name = str(message.sender_name)[:200]
                            if self.sessions.private[conversation.id].get("name") != name:
                                self.sessions.update("private", conversation.id, "name", name)
                        engine = self.engine(conversation.id)
                        if engine.peer and message.sender_name:
                            engine.peer["name"] = str(message.sender_name)[:200]
                        if quote is not None:
                            await engine.on_user_message(message.text, attachments, qid, quote=quote)
                        else:
                            await engine.on_user_message(message.text, attachments, qid)
                        store = engine.store
                    else:
                        chat = self.chat(conversation.id)
                        for i, attachment in enumerate(attachments or [None]):
                            if qid is not None and key in self._recalled:
                                break
                            await chat.on_message(message.sender_id, str(message.sender_name)[:200] or message.sender_id,
                                                  message.text if i == 0 else "", [attachment] if attachment else [], qid, called,
                                                  **({"quote": quote} if i == 0 and quote is not None else {}))
                        store = chat.store
                    if conversation.kind == "private" or called:
                        provider = self.caption_provider(conversation.scope, self.llm_for(conversation.scope))
                        for attachment in attachments:
                            self.captioner.start(provider, attachment, store.save_messages)
                    return True
            finally:
                self._receiving -= 1
                self._receivers.pop(task, None)

    async def recall(self, conversation, qid):
        with self.profile_scope():
            conversation = self._validated(conversation.kind, conversation.id)
            if self._stopped or not self._enabled(conversation):
                return False
            key = (conversation.scope, str(qid))
            self._recalled[key] = None
            self._recalled.move_to_end(key)
            while len(self._recalled) > 2048:
                self._recalled.popitem(last=False)
            target = self.engine(conversation.id) if conversation.kind == "private" else self.chat(conversation.id)
            await target.on_recall(str(qid))
            return True

    def _spawn(self, key, factory):
        existing = self._background.get(key)
        if self._stopped or (existing is not None and not existing.done()):
            return existing
        async def work():
            with self.profile_scope():
                try:
                    return await factory()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._error(f"Background {key}", error)
        with self.profile_scope():
            task = asyncio.create_task(work())
        self._background[key] = task
        task.add_done_callback(lambda completed: self._background.pop(key, None) if self._background.get(key) is completed else None)
        return task

    async def tick(self):
        with self.profile_scope():
            if self._stopped:
                return
            self._restore_sessions()
            for sid, engine in list(self.engines.items()):
                if self.sessions.private_on(sid):
                    self._spawn(f"tick:p:{sid}", engine.heartbeat)
            self._spawn("groups", lambda: self.hub.heartbeat(self.sessions.proactive, self.sessions.group_on))
            await asyncio.sleep(0)

    @property
    def dashboard_url(self):
        return self._dashboard.url if self._dashboard else None

    async def start(self):
        async with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("Runtime is closed")
            if self._heartbeat_task is not None and not self._heartbeat_task.done():
                return
            settings = self.config.get("dashboard", {})
            if not isinstance(settings, dict) or type(settings.get("enabled", True)) is not bool:
                raise ValueError("dashboard 必须是对象，dashboard.enabled 必须是布尔值")
            if settings.get("enabled", True) and self._dashboard is None:
                from .local_dashboard import LocalDashboard
                server = LocalDashboard(self, settings.get("port", 8765))
                await server.start()  # Failed binding must not leave a running heartbeat.
                self._dashboard = server
                self.log.info("本地仪表盘：%s", self.dashboard_url)
            async def heartbeat():
                while not self._stopped:
                    await self.tick()
                    await asyncio.sleep(30)
            with self.profile_scope():
                self._heartbeat_task = asyncio.create_task(heartbeat())

    def spawn_memory(self, scope, force=True):
        with self.profile_scope():
            conversation = self._conversation(scope)
            if conversation is None:
                raise ValueError("A conversation is required")
            target = self.engine(conversation.id) if conversation.kind == "private" else self.chat(conversation.id)
            return self._spawn(f"memory:{scope}", lambda: target.run_memory(force=force))

    async def force_look(self, gid=None):
        with self.profile_scope():
            if self._stopped:
                raise RuntimeError("Runtime is closed")
            ids = [gid] if gid is not None else list(self.hub.chats)
            tasks = []
            for sid in ids:
                if self.sessions.proactive(sid):
                    chat = self.chat(sid)
                    chat.force_look = True
                    tasks.append(self._spawn(f"look:g:{sid}", lambda c=chat: c.look(self.sessions.proactive)))
            if tasks:
                await asyncio.gather(*tasks)

    @staticmethod
    def _tasks_of(target):
        tasks = set(getattr(target, "_running", ()))
        for name in ("batch_task", "req_task", "window_task", "_offline_notice_task", "_tick_task", "_look_task", "_memory_task", "_history_task"):
            task = getattr(target, name, None)
            if isinstance(task, asyncio.Task):
                tasks.add(task)
        return tasks

    async def _stop_session(self, conversation):
        scope = conversation.scope
        target = self.engines.pop(conversation.id, None) if conversation.kind == "private" else self.hub.chats.pop(conversation.id, None)
        tasks = self._tasks_of(target) if target is not None else set()
        if target is not None:
            target.shutdown()
        tasks.update(task for task, source in self._receivers.items() if source == scope)
        tasks.update(task for key, task in self._background.items() if key.endswith(":" + scope))
        await self._cancel(tasks)

    @staticmethod
    async def _cancel(tasks):
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def update_session(self, kind, sid, field, value):
        with self.profile_scope():
            if self._stopped:
                raise RuntimeError("Runtime is closed")
            conversation = self._validated(kind, sid)
            if not self.sessions.update(kind, sid, field, value):
                return False
            if field == "enabled":
                if value:
                    if self._enabled(conversation):
                        (self.engine if kind == "private" else self.chat)(sid)
                else:
                    await self._stop_session(conversation)
            elif field == "name" and kind == "private" and sid in self.engines:
                self.engine(sid)
            return True

    async def remove_session(self, kind, sid):
        with self.profile_scope():
            if self._stopped:
                raise RuntimeError("Runtime is closed")
            conversation = self._validated(kind, sid)
            if sid not in self.sessions.book(kind):
                return False
            self.sessions.remove(kind, sid)
            await self._stop_session(conversation)
            return True

    async def reload_data(self):
        with self.profile_scope():
            if self._stopped:
                raise RuntimeError("Runtime is closed")
            sources = [*self.engines.values(), *self.hub.chats.values()]
            if self._receiving or any(s.in_flight or s.mem_busy or any(not t.done() for t in self._tasks_of(s)) for s in sources) or self._background or self.captioner._tasks:
                raise RuntimeError("Runtime is busy")
            prepared, knowledge, people, style = self._prepare_data()
            # No await: a caller can never observe half old/half new resources.
            self.profile_state, self.knowledge, self.people, self.style = prepared, knowledge, people, style
            for source in [*self.engines.values(), self.hub]:
                source.knowledge, source.people, source.style = knowledge, people, style
            self._data_error = ""
            return True

    reload = reload_data

    async def close(self):
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self):
        with self.profile_scope():
            if self._stopped:
                return
            self._stopped = True
            if self._dashboard:
                await self._dashboard.close()
                self._dashboard = None
            tasks = set(self._background.values()) | set(self._receivers)
            if self._heartbeat_task:
                tasks.add(self._heartbeat_task)
            for target in [*self.engines.values(), *self.hub.chats.values()]:
                tasks.update(self._tasks_of(target))
                target.shutdown()
            self.hub.closed = True
            tasks.update(self.captioner._tasks.values())
            self.captioner.stop()
            await self._cancel(tasks)
            self._background.clear()
            if self._owns_embedder:
                self.embedder.stop()
