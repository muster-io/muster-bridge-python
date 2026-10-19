# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""A bridge whose messenger lives in memory and writes everything the bot posts to its log.

Muster, muster bridge check or a person with curl can try the Bridge protocol with it, without a real messenger:

    printf example-token > /tmp/bt
    python examples/memory_bridge.py --token-file /tmp/bt

It offers every feature, or with BRIDGE_FEATURES only those it lists, comma-separated: buttons, thread_messages,
direct_messages (which also turns on user_lookup login), private_answers, native_mentions, quiet_messages,
status_color, deletion_events and delete. Outside the protocol it serves POST /demo/press {"message_id", "user"},
which presses the first button of the message as the person with that login. Unlike the protocol, which needs the
token, it has no authentication of its own: the example listens on loopback by default.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

import muster_bridge
from muster_bridge import (
    AnswerShownShownAs,
    Bot,
    BridgeApp,
    Button,
    ButtonStyle,
    Capabilities,
    CapabilitiesButtons,
    CapabilitiesDelete,
    CapabilitiesDirectMessages,
    CapabilitiesPrivateAnswer,
    CapabilitiesThreadMessages,
    Config,
    DirectMessageInput,
    DirectMessageSent,
    Forbidden,
    Gone,
    Limits,
    Mention,
    MessageContent,
    MessageEdited,
    MessageInput,
    MessagePosted,
    NotOfferedError,
    Person,
    PushTarget,
    ReportedEvent,
    ThreadReplyInput,
    ThreadReplyPosted,
    replace_mentions,
)

BOT = "main"
FEATURES = (
    "buttons",
    "thread_messages",
    "direct_messages",
    "private_answers",
    "native_mentions",
    "quiet_messages",
    "status_color",
    "deletion_events",
    "delete",
)
MAX_DEMO_BODY = 64 << 10

log = logging.getLogger("memory_bridge")


def features() -> set[str]:
    """Return the features BRIDGE_FEATURES lists, or all of them when it is not set."""
    listed = os.environ.get("BRIDGE_FEATURES")
    if listed is None:
        return set(FEATURES)
    on: set[str] = set()
    for item in listed.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in FEATURES:
            raise ValueError(f'BRIDGE_FEATURES: unknown feature "{name}"; the features are {", ".join(FEATURES)}')
        on.add(name)
    return on


def read_secret(path: str) -> str:
    """Return the token or push secret a file holds, without the white space around it."""
    secret = Path(path).read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError("it holds no secret")
    return secret


def parse_users(listed: str) -> dict[str, Person]:
    """Return the people of --users, login:display name, comma-separated, by login."""
    people: dict[str, Person] = {}
    for entry in listed.split(","):
        if not entry.strip():
            continue
        login, sep, name = entry.partition(":")
        login, name = login.strip(), name.strip()
        if not sep or not login or not name:
            raise ValueError(f"--users: {entry!r} is not login:display name")
        if login in people:
            raise ValueError(f"--users: the login {login!r} appears twice")
        people[login] = Person(external_id=login, display_name=name, handle=login)
    return people


def parse_listen(listen: str) -> tuple[str, int]:
    """Return the host, without the brackets of an IPv6 address, and the port of --listen."""
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"--listen: {listen!r} is not host:port")
    return host.removeprefix("[").removesuffix("]"), int(port)


def capabilities(on: set[str]) -> Capabilities:
    """Return the capabilities of the features that are on."""
    return Capabilities(
        buttons=CapabilitiesButtons(
            max_per_message=5,
            max_per_row=5,
            data_max_bytes=64,
            styles=[ButtonStyle.primary, ButtonStyle.success, ButtonStyle.neutral, ButtonStyle.danger],
        )
        if "buttons" in on
        else None,
        thread_messages=CapabilitiesThreadMessages.all if "thread_messages" in on else None,
        direct_messages=CapabilitiesDirectMessages(bot_can_start=True) if "direct_messages" in on else None,
        private_answers=[CapabilitiesPrivateAnswer.ephemeral] if "private_answers" in on else None,
        native_mentions=True if "native_mentions" in on else None,
        quiet_messages=True if "quiet_messages" in on else None,
        status_color=True if "status_color" in on else None,
        delete=CapabilitiesDelete(max_age_seconds=None) if "delete" in on else None,
        deletion_events=True if "deletion_events" in on else None,
    )


@dataclass
class Message:
    """A message of the bot, or a reply in the Thread of its root."""

    channel: str
    title: str
    body: str
    buttons: list[Button]
    root: str | None = None
    deleted: bool = False


@dataclass
class MemoryChat:
    """The in-memory messenger: a Messenger, a UserFinder, a DirectMessenger and a Deleter.

    Every message it keeps is also written to the log, which stands in for the messenger's screen. Its ids start with a
    prefix that is random for each start, so that a restarted example never hands out an id that Muster still holds
    for a message of the previous run.
    """

    channels: set[str]
    people: dict[str, Person]
    private: bool
    native: bool
    id_prefix: str = field(default_factory=lambda: secrets.token_hex(3))
    messages: dict[str, Message] = field(default_factory=dict)
    last: int = 0
    last_dm: int = 0

    async def post_message(self, bot: str, message: MessageInput) -> MessagePosted:
        """Post a message to one of the channels."""
        if message.channel_id not in self.channels:
            raise Forbidden("The bot has not joined this channel.")
        message_id = self._next_id()
        self.messages[message_id] = Message(message.channel_id, message.title, message.body, message.buttons)
        log.info(
            "message posted: bot=%s message_id=%s channel=%s kind=%s status=%s title=%r body=%r buttons=%s",
            bot,
            message_id,
            message.channel_id,
            message.kind.value,
            message.status.value,
            message.title,
            self._mentions(message.body, message.mentions),
            labels(message.buttons),
        )
        return MessagePosted(message_id=message_id)

    async def edit_message(self, bot: str, message_id: str, content: MessageContent) -> MessageEdited:
        """Replace a message with the content."""
        message = self._find(message_id)
        message.title, message.body, message.buttons = content.title, content.body, content.buttons
        log.info(
            "message edited: bot=%s message_id=%s status=%s title=%r body=%r buttons=%s",
            bot,
            message_id,
            content.status.value,
            content.title,
            content.body,
            labels(content.buttons),
        )
        return MessageEdited()

    async def reply_in_thread(self, bot: str, message_id: str, reply: ThreadReplyInput) -> ThreadReplyPosted:
        """Post a reply in the Thread of a message, which is its root's Thread for a reply."""
        parent = self._find(message_id)
        root = parent.root or message_id
        reply_id = self._next_id()
        self.messages[reply_id] = Message(parent.channel, "", reply.body, reply.buttons, root=root)
        thread_ref = f"t-{root}"
        log.info(
            "thread reply posted: bot=%s message_id=%s thread_ref=%s notify=%s body=%r buttons=%s",
            bot,
            reply_id,
            thread_ref,
            reply.notify,
            self._mentions(reply.body, reply.mentions),
            labels(reply.buttons),
        )
        return ThreadReplyPosted(message_id=reply_id, thread_ref=thread_ref)

    async def check_channel(self, bot: str, channel_id: str) -> str:
        """Return the channel's name, its id here."""
        if channel_id not in self.channels:
            raise Forbidden("The bot has not joined this channel.")
        return channel_id

    async def answer_event(self, bot: str, event: ReportedEvent, text: str) -> AnswerShownShownAs:
        """Show the answer: directly for an account link, privately for a press when private answers are on."""
        shown = AnswerShownShownAs.thread
        if event.type == "account_link":
            shown = AnswerShownShownAs.direct
        elif event.type == "press" and self.private:
            shown = AnswerShownShownAs.ephemeral
        log.info(
            "answer shown: bot=%s event_id=%s type=%s message_id=%s person=%s chat=%s shown_as=%s text=%r",
            bot,
            event.id,
            event.type,
            event.message_id,
            event.user.external_id,
            event.chat,
            shown.value,
            text,
        )
        return shown

    async def find_user(self, bot: str, handle: str) -> Person | None:
        """Return the person with the login, or None."""
        return self.people.get(handle)

    async def send_direct_message(self, bot: str, message: DirectMessageInput) -> DirectMessageSent:
        """Send a direct message to one of the people."""
        if message.user_external_id not in self.people:
            raise Forbidden("This person cannot receive a message from the bot.")
        self.last_dm += 1
        message_id = f"{self.id_prefix}-d{self.last_dm}"
        # The log is the person's screen here, so the body is logged: it carries the Account link code that the person
        # must see. A real bridge never logs the body of a direct message; it may hold such a code or another secret.
        log.info(
            "direct message sent: bot=%s message_id=%s person=%s body=%r",
            bot,
            message_id,
            message.user_external_id,
            message.body,
        )
        return DirectMessageSent(message_id=message_id)

    async def delete_message(self, bot: str, message_id: str) -> None:
        """Delete a message of the bot."""
        self._find(message_id).deleted = True
        log.info("message deleted: bot=%s message_id=%s", bot, message_id)

    async def demo_press(self, request: Request, bridge: BridgeApp) -> Response:
        """Press the first button of a message as a person: what a script does in place of a person."""
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_DEMO_BODY:
                return PlainTextResponse("the body is larger than 64 KiB", HTTPStatus.BAD_REQUEST)
        try:
            press = json.loads(body)
        except ValueError:
            press = None
        if not isinstance(press, dict) or not all(isinstance(press.get(k), str) for k in ("message_id", "user")):
            return PlainTextResponse('the body is not {"message_id": …, "user": …}', HTTPStatus.BAD_REQUEST)
        message_id, login = press["message_id"], press["user"]
        person = self.people.get(login)
        if person is None:
            return PlainTextResponse(f"no person has the login {json.dumps(login)}", HTTPStatus.BAD_REQUEST)
        message = self.messages.get(message_id)
        if message is None or message.deleted or not message.buttons:
            return PlainTextResponse(
                f"the message {json.dumps(message_id)} does not exist or has no buttons", HTTPStatus.BAD_REQUEST
            )
        button = message.buttons[0]
        try:
            await bridge.press(BOT, message_id=message_id, user=person, data=button.data, chat=message.channel)
        except NotOfferedError:
            return PlainTextResponse("this bridge does not offer buttons", HTTPStatus.CONFLICT)
        except Exception:
            log.exception("the press could not be reported")
            return PlainTextResponse("the press could not be reported", HTTPStatus.INTERNAL_SERVER_ERROR)
        log.info("button pressed: bot=%s message_id=%s person=%s label=%r", BOT, message_id, login, button.label)
        return Response(status_code=HTTPStatus.NO_CONTENT)

    def _find(self, message_id: str) -> Message:
        message = self.messages.get(message_id)
        if message is None or message.deleted:
            raise Gone("No message has this id.")
        return message

    def _next_id(self) -> str:
        self.last += 1
        return f"{self.id_prefix}-m{self.last}"

    def _mentions(self, body: str, mentions: list[Mention]) -> str:
        """Return body with its Mention markers as the messenger shows them: @login or the name."""
        return replace_mentions(body, mentions, lambda m: f"@{m.external_id}" if self.native else m.display_name)


def labels(buttons: list[Button]) -> list[str]:
    """Return the labels of the buttons."""
    return [button.label for button in buttons]


def configure(args: argparse.Namespace) -> tuple[Config, MemoryChat]:
    """Return the bridge's Config and its in-memory messenger, from the flags and BRIDGE_FEATURES."""
    on = features()
    try:
        token = read_secret(args.token_file)
    except (OSError, ValueError) as error:
        raise ValueError(f"--token-file: {error}") from None
    if (args.push_url is None) != (args.push_secret_file is None):
        raise ValueError("--push-url and --push-secret-file go together")
    push = None
    if args.push_url is not None:
        try:
            push = PushTarget(url=args.push_url, secret=read_secret(args.push_secret_file))
        except (OSError, ValueError) as error:
            raise ValueError(f"--push-secret-file: {error}") from None
    people = parse_users(args.users)
    config = Config(
        name="muster-bridge-python memory example",
        version=muster_bridge.__version__,
        messenger_name="Memory example",
        bots=[Bot(name=BOT, token=token, push=push)],
        limits=Limits(max_body_length=4000, edit_max_age_seconds=None),
        capabilities=capabilities(on),
        user_lookup="login" if "direct_messages" in on else "none",
    )
    chat = MemoryChat(
        channels={c.strip() for c in args.channels.split(",") if c.strip()},
        people=people,
        private="private_answers" in on,
        native="native_mentions" in on,
    )
    return config, chat


def main() -> None:
    """Serve the protocol and /demo/press until SIGINT or SIGTERM, then stop the server and the bridge's pushers."""
    parser = argparse.ArgumentParser(description="A bridge whose messenger lives in memory.")
    parser.add_argument("--listen", default="127.0.0.1:18095", help="the address to listen on")
    parser.add_argument("--token-file", required=True, help="the file that holds the bot's token")
    parser.add_argument("--channels", default="alerts,ops", help="the channels the bot can use, comma-separated")
    parser.add_argument(
        "--users",
        default="alice:Alice Smith,bob:Bob Brown",
        help="the people, as login:display name, comma-separated",
    )
    parser.add_argument("--push-url", help="where to push the events, the callback URL of the Bridge Connection")
    parser.add_argument("--push-secret-file", help="the file that holds the push secret, whsec_…")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        host, port = parse_listen(args.listen)
        config, chat = configure(args)
        bridge = BridgeApp(config, chat)
    except ValueError as error:
        log.error("stopped: %s", error)
        sys.exit(2)

    async def demo_press(request: Request) -> Response:
        return await chat.demo_press(request, bridge)

    app = Starlette(
        routes=[Route("/demo/press", demo_press, methods=["POST"]), Mount("/", app=bridge)],
        lifespan=bridge.lifespan,
    )
    # uvicorn sets no timeout that cuts a request short: a long poll of /v1/events holds its answer up to 60 seconds.
    # A proxy in front of a real bridge needs a read timeout above 75 seconds.
    uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
