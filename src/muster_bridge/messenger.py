# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The protocols a bridge implements: the calls to its messenger."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from muster_bridge.models import (
        AnswerShownShownAs,
        DirectMessageInput,
        DirectMessageSent,
        MessageContent,
        MessageEdited,
        MessageInput,
        MessagePosted,
        Person,
        ThreadReplyInput,
        ThreadReplyPosted,
    )

__all__ = [
    "Deleter",
    "DirectMessenger",
    "EventType",
    "Messenger",
    "ReportedEvent",
    "UserFinder",
    "idempotency_key",
]

EventType = Literal["press", "thread_message", "account_link", "message_deleted"]
"""The type of an event the bridge reports."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportedEvent:
    """An event the bridge reported, as answer_event is given it."""

    id: str
    type: EventType
    message_id: str | None
    """The message of a press or of a Thread message; None for an account link."""
    thread_ref: str | None
    """The Thread of a Thread message, as the bridge reported it; None for the other types."""
    user: Person
    chat: str | None
    """What the bridge passed with the report; it is never sent to Muster."""
    occurred_at: datetime
    """When it happened, in UTC."""


@runtime_checkable
class Messenger(Protocol):
    """What every bridge implements: the calls to its messenger.

    Each method is given the bot whose token Muster sent, by its name in Config.bots, and returns the protocol's
    answer. It raises a BridgeError to answer its class; any other exception is answered transient with a generic
    detail. The library validates every request before it calls a method and every answer before it sends it.
    """

    async def post_message(self, bot: str, message: MessageInput) -> MessagePosted:
        """Post a message to message.channel_id: a Root message, a Storm summary or a test message."""
        ...

    async def edit_message(self, bot: str, message_id: str, content: MessageContent) -> MessageEdited:
        """Replace the whole message with content; editing to the state the message already shows is a success."""
        ...

    async def reply_in_thread(self, bot: str, message_id: str, reply: ThreadReplyInput) -> ThreadReplyPosted:
        """Post a reply in the Thread of the message.

        reply.thread_ref is the thread_ref Muster sent, None for none; the answer's thread_ref is the one Muster keeps
        and sends with the next reply.
        """
        ...

    async def check_channel(self, bot: str, channel_id: str) -> str:
        """Post nothing and return the channel's name as people see it.

        Raise Forbidden when the bot cannot post, edit and reply there.
        """
        ...

    async def answer_event(self, bot: str, event: ReportedEvent, text: str) -> AnswerShownShownAs:
        """Show text to whoever caused the event, which the bridge reported, and return where it was shown."""
        ...


@runtime_checkable
class UserFinder(Protocol):
    """The messenger's lookup of people, which a user_lookup other than "none" needs."""

    async def find_user(self, bot: str, handle: str) -> Person | None:
        """Return the one account with the handle, of the kind Config.user_lookup declares, or None for nobody."""
        ...


@runtime_checkable
class DirectMessenger(Protocol):
    """The messenger's direct messages, which capabilities.direct_messages needs."""

    async def send_direct_message(self, bot: str, message: DirectMessageInput) -> DirectMessageSent:
        """Send a direct message from the bot, opening the direct chat if needed."""
        ...


@runtime_checkable
class Deleter(Protocol):
    """The messenger's deletion of the bot's messages, which capabilities.delete needs."""

    async def delete_message(self, bot: str, message_id: str) -> None:
        """Delete a message of the bot.

        A message that no longer exists is a success, also when this raises Gone.
        """
        ...


_IDEMPOTENCY_KEY: ContextVar[str | None] = ContextVar("muster_bridge_idempotency_key", default=None)


def idempotency_key() -> str | None:
    """Return the Idempotency-Key of the request the current messenger call serves; None for an operation without one.

    A messenger with an idempotency mechanism of its own can be given it. Keys are per bot.
    """
    return _IDEMPOTENCY_KEY.get()


def bind_idempotency_key(key: str) -> Token[str | None]:
    """Make key the Idempotency-Key of the messenger calls that follow, until unbind_idempotency_key."""
    return _IDEMPOTENCY_KEY.set(key)


def unbind_idempotency_key(token: Token[str | None]) -> None:
    """Restore the Idempotency-Key that bind_idempotency_key replaced."""
    _IDEMPOTENCY_KEY.reset(token)
