import asyncio
import signal
from typing import (
    TYPE_CHECKING,
    Any,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from pyrogram import Client
from pyrogram.filters import Filter
from pyrogram.handlers import (
    CallbackQueryHandler,
    InlineQueryHandler,
    MessageHandler
)
from pyrogram.types import Message, Update, User

from bot import util

from .bot_mixin_base import BotMixinBase

if TYPE_CHECKING:
    from .bot import Bot

TgEventHandler = Union[CallbackQueryHandler,
                       InlineQueryHandler,
                       MessageHandler]


class TelegramBot(BotMixinBase):
    # Initialized during instantiation
    config: util.config.TelegramConfig[str, Any]
    _plugin_event_handlers: MutableMapping[str, Tuple[TgEventHandler, int]]
    _disconnect: bool
    loaded: bool
    sudo_users: Set[int]

    # Initialized during startup
    client: Client
    user: User
    uid: int
    start_time_us: int
    owner: int

    def __init__(self: "Bot", **kwargs: Any) -> None:
        self.config = util.config.TelegramConfig()
        self._plugin_event_handlers = {}
        self._disconnect = False
        self.loaded = False
        self.sudo_users = set()

        # Propagate initialization to other mixins
        super().__init__(**kwargs)

    async def init_client(self: "Bot") -> None:
        api_id = self.config["api_id"]
        if not api_id:
            raise RuntimeError("API_ID environment variable not set")

        api_hash = self.config["api_hash"]
        if not api_hash:
            raise RuntimeError("API_HASH environment variable not set")

        if bot_token := self.config["bot_token"]:
            # Initialize Telegram client with gathered parameters
            self.client = Client(
                session_name=":memory:", api_id=api_id, api_hash=api_hash,
                bot_token=bot_token
            )
        else:
            raise RuntimeError("BOT_TOKEN environment variable not set")

    async def start(self: "Bot") -> None:
        self.log.info("Starting")
        await self.init_client()

        # Register core command handler
        self.client.add_handler(MessageHandler(self.on_command,
                                               self.command_predicate()), -1)

        # Register conversation handler
        self.client.add_handler(MessageHandler(self.on_conversation,
                                self.conversation_predicate()), 0)

        # Load plugin
        self.load_all_plugins()
        await self.dispatch_event("load")
        self.loaded = True

        if "Aria2" not in self.plugins:
            raise RuntimeError("Aria2 websocket is not running, exiting...")
        if "GoogleDrive" not in self.plugins:
            raise RuntimeError("GoogleDrive environment variable needed not set")

        # Start Telegram client
        try:
            await self.client.start()
        except AttributeError:
            self.log.error(
                "Unable to get input for authorization! Make sure all configuration are done before running the bot."
            )
            raise

        # Get info
        user = await self.client.get_me()
        if not isinstance(user, User):
            raise TypeError("Missing full self user information")
        self.user = user
        # noinspection PyTypeChecker
        self.uid = user.id
        self.owner = int(self.config["owner_id"])

        # Get sudoers from db
        db = self.db.get_collection("sudoers")
        async for user in db.find():
            self.sudo_users.add(user["_id"])

        # Record start time and dispatch start event
        self.start_time_us = util.time.usec()
        await self.dispatch_event("start", self.start_time_us)

        self.log.info("Bot is ready")

        # Dispatch final late start event
        await self.dispatch_event("started")

    async def idle(self: "Bot") -> None:
        signals = {
            k: v
            for v, k in signal.__dict__.items()
            if v.startswith("SIG") and not v.startswith("SIG_")
        }
        disconnect = False

        def signal_handler(signum, _) -> None:
            nonlocal disconnect

            print(flush=True)
            self.log.info(f"Stop signal received ('{signals[signum]}').")
            disconnect = True

        for name in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
            signal.signal(name, signal_handler)

        while not disconnect:
            await asyncio.sleep(1)

    async def run(self: "Bot") -> None:
        try:
            # Start client
            try:
                await self.start()
            except KeyboardInterrupt:
                self.log.warning("Received interrupt while connecting")
                return

            # Request updates, then idle until disconnected
            await self.idle()
        finally:
            # Make sure we stop when done
            try:
                await self.stop()
            finally:
                self.loop.stop()

    def update_plugin_event(self: "Bot",
                            name: str,
                            event_type: Type[TgEventHandler],
                            *,
                            filters: Optional[Filter] = None,
                            group: int = 0) -> None:
        if name in self.listeners:
            # Add if there ARE listeners and it's NOT already registered
            if name not in self._plugin_event_handlers:

                async def event_handler(client: Client, event: Update) -> None:  # skipcq: PYL-W0613
                    await self.dispatch_event(name, event)

                handler_info = (event_type(event_handler, filters), group)
                self.client.add_handler(*handler_info)
                self._plugin_event_handlers[name] = handler_info
        elif name in self._plugin_event_handlers:
            # Remove if there are NO listeners and it's ALREADY registered
            self.client.remove_handler(*self._plugin_event_handlers[name])
            del self._plugin_event_handlers[name]

    def update_plugin_events(self: "Bot") -> None:
        self.update_plugin_event("callback_query", CallbackQueryHandler)
        self.update_plugin_event("inline_query", InlineQueryHandler)
        self.update_plugin_event("message", MessageHandler)

    @property
    def events_activated(self: "Bot") -> int:
        return len(self._plugin_event_handlers)

    def redact_message(self, text: str) -> str:
        api_id = self.config["api_id"]
        api_hash = self.config["api_hash"]
        bot_token = self.config["bot_token"]
        db_uri = self.config["db_uri"]

        if api_id in text:
            text = text.replace(api_id, "[REDACTED]")
        if api_hash in text:
            text = text.replace(api_hash, "[REDACTED]")
        if bot_token in text:
            text = text.replace(bot_token, "[REDACTED]")
        if db_uri in text:
            text = text.replace(db_uri, "[REDACTED]")

        return text

    # Flexible response function with filtering, truncation, redaction, etc.
    async def respond(
        self: "Bot",
        msg: Message,
        text: str,
        *,
        mode: str = "edit",
        redact: bool = True,
        response: Optional[Message] = None,
        **kwargs: Any,
    ) -> Message:
        # Redact sensitive information if enabled and known
        if redact:
            text = self.redact_message(text)

        # Truncate messages longer than Telegram's 4096-character length limit
        text = util.tg.truncate(text)

        # force reply and as default behaviour if response is None
        if mode == "reply" or response is None and mode == "edit":
            return await msg.reply(text, **kwargs)

        # Only accept edit if we already respond the original msg
        if response is not None and mode == "edit":
            return await response.edit(text=text, **kwargs)

        raise ValueError(f"Unknown response mode '{mode}'")
