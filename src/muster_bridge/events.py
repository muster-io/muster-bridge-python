# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The events a bridge reports, queued per bot for pollEvents and the pushers."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from muster_bridge.config import to_json_object
from muster_bridge.messenger import ReportedEvent
from muster_bridge.models import AccountLinkEvent, Event, MessageDeletedEvent, Person, PressEvent, ThreadMessageEvent
from muster_bridge.problems import describe, encode_json, validation_problem
from muster_bridge.store import StoredEvent

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from muster_bridge.config import Clock
    from muster_bridge.messenger import EventType
    from muster_bridge.store import Store

__all__ = ["InvalidEventError", "NotOfferedError", "UnknownBotError"]

EVENT_RETENTION = timedelta(hours=24)
POLL_BATCH = 100
MAX_THREAD_TEXT = 4000

_ACCOUNT_LINK_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class UnknownBotError(LookupError):
    """A report, or set_push, for a bot that is not in Config.bots."""


class NotOfferedError(Exception):
    """A report of an event that needs a feature the bridge does not offer."""


class InvalidEventError(ValueError):
    """A report of an event that would not match api/bridge.yaml; nothing is queued."""


class StoredEventError(Exception):
    """A stored event that is not the JSON of an event: a fault of the store."""


class WaitError(Exception):
    """A failure of a long poll's wait, the clock's sleep or the wait for the next event: no fault of the store."""


def account_link_token(text: str) -> str | None:
    """Return the Account link token of a direct message: its whole text, trimmed and after an optional "/start "."""
    token = text.strip().removeprefix("/start ").strip()
    return token if _ACCOUNT_LINK_TOKEN.fullmatch(token) else None


def event_id(bot: str, event_type: str, update_id: str | None, random: Callable[[int], bytes]) -> str:
    """Return the id of an event: hashed from the bot, the type and the messenger's update id, else random.

    An update the messenger delivers twice is thus one event. Both forms are base64url, which fits webhook-id.
    """
    raw = hashlib.sha256(f"{bot}\x00{event_type}\x00{update_id}".encode()).digest() if update_id else random(16)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def rfc3339(moment: datetime) -> str:
    """Return an aware time in UTC as RFC 3339, with as many digits of the second's fraction as it needs."""
    m = moment.astimezone(UTC)
    text = f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}"
    if m.microsecond:
        text += f".{m.microsecond:06d}".rstrip("0")
    return text + "Z"


def running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the event loop that runs the caller, or None outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def stop(*tasks: asyncio.Future[Any] | None) -> None:
    """Cancel the tasks that have not ended and wait until they have.

    A task of another event loop is only cancelled, in its own loop, and one of a closed loop is left alone: neither
    can be waited for here.
    """
    current = running_loop()
    pending = set()
    for task in tasks:
        if task is None or task.done():
            continue
        loop = task.get_loop()
        if loop is current:
            task.cancel()
            pending.add(task)
        elif not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                loop.call_soon_threadsafe(task.cancel)
    if pending:
        await asyncio.wait(pending)


def _complete(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)


class Wakeup:
    """Wakes the tasks that wait on it, each in its own event loop.

    Unlike asyncio's Event and Condition, no event loop owns it, so that a bridge that runs on one loop after another,
    as Starlette's TestClient does, keeps working. The loops take turns, one running at a time: nothing here is locked,
    so loops that run at once in two threads could miss a wakeup.
    """

    def __init__(self) -> None:
        self._waiters: set[asyncio.Future[None]] = set()

    def wake_all(self) -> None:
        """Wake every task that waits now; a waiter of a closed loop is dropped."""
        waiters, self._waiters = self._waiters, set()
        current = running_loop()
        for waiter in waiters:
            loop = waiter.get_loop()
            if loop is current:
                _complete(waiter)
            elif not loop.is_closed():
                with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                    loop.call_soon_threadsafe(_complete, waiter)

    async def wait(self) -> None:
        """Return once wake_all is called."""
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.add(waiter)
        try:
            await waiter
        finally:
            self._waiters.discard(waiter)


class BotSignal:
    """Wakes whoever waits for the next event of a bot: its long polls and its pusher.

    A waiter reads generation before it reads the store, so that an event queued in between still wakes it. As with
    Wakeup, the event loops of the waiters and of the reports take turns, never run at once in two threads.
    """

    def __init__(self) -> None:
        self.generation = 0
        self._wakeup = Wakeup()

    def fire(self) -> None:
        """Wake every waiter."""
        self.generation += 1
        self._wakeup.wake_all()

    async def wait_changed(self, seen: int) -> None:
        """Return once an event was queued after the generation seen; on the loop whose turn it is."""
        while self.generation == seen:
            await self._wakeup.wait()


class Events:
    """Queues the reported events of each bot, and answers pollEvents and the lookups of answerEvent."""

    def __init__(self, bots: Iterable[str], store: Store, clock: Clock, random: Callable[[int], bytes]) -> None:
        self._signals = {bot: BotSignal() for bot in bots}
        self._store = store
        self._clock = clock
        self._random = random

    def signal(self, bot: str) -> BotSignal:
        """Return the signal of the bot's events."""
        return self._signals[bot]

    async def report(
        self,
        bot: str,
        event_type: EventType,
        members: Mapping[str, Any],
        *,
        update_id: str | None,
        at: datetime | None,
        chat: str | None,
    ) -> bool:
        """Validate the event as an Event of api/bridge.yaml and queue it; return whether it was queued.

        An event whose id is kept already is not queued again. The store's exceptions propagate unchanged.
        """
        if "message_id" in members and not members["message_id"]:
            raise InvalidEventError("message_id is empty")
        if "user" in members and not isinstance(members["user"], Person):
            raise InvalidEventError("user is not a muster_bridge.Person")
        if at is not None and at.utcoffset() is None:
            raise InvalidEventError("at has no time zone")
        now = self._clock.now()
        try:
            occurred = rfc3339(at if at is not None else now)
        except OverflowError:
            raise InvalidEventError("at cannot be given in UTC") from None
        new_id = event_id(bot, event_type, update_id, self._random)
        event: dict[str, Any] = {"id": new_id, "type": event_type, "occurred_at": occurred}
        for name, value in members.items():
            event[name] = to_json_object(value) if isinstance(value, BaseModel) else value
        try:
            body = encode_json(event)
        except (TypeError, ValueError):
            raise InvalidEventError("it cannot be encoded") from None
        try:
            Event.model_validate_json(body, strict=True)
        except ValidationError as error:
            raise InvalidEventError(validation_problem(error, skip=1)) from None
        stored = StoredEvent(id=new_id, type=event_type, body=body, chat=chat, until=now + EVENT_RETENTION)
        queued = await self._store.append_event(bot, stored, now)
        if queued:
            self._signals[bot].fire()
        return queued

    async def poll(
        self, bot: str, cursor: str, wait: int, disconnected: asyncio.Future[Any]
    ) -> tuple[list[bytes], str] | None:
        """Return the events after the cursor at once, or the first that come within wait seconds, or none after it.

        Return None when disconnected ends first: the client has left. The store's exceptions propagate; a failure of
        the wait raises WaitError.
        """
        signal = self._signals[bot]
        sleeper: asyncio.Future[None] | None = None
        changed: asyncio.Future[None] | None = None
        try:
            while True:
                seen = signal.generation
                events, next_cursor = await self._store.events_after(bot, cursor, POLL_BATCH, self._clock.now())
                if events or wait <= 0:
                    return [event.body for event in events], next_cursor
                if sleeper is None:
                    sleeper = asyncio.ensure_future(self._clock.sleep(wait))
                changed = asyncio.ensure_future(signal.wait_changed(seen))
                await asyncio.wait({changed, sleeper, disconnected}, return_when=asyncio.FIRST_COMPLETED)
                if disconnected.done():
                    return None
                waited = sleeper if sleeper.done() else changed
                try:
                    waited.result()  # a failed wait raises rather than polling the store again at once
                except Exception as error:
                    raise WaitError(describe(error)) from error
                if waited is sleeper:
                    return [], next_cursor
        finally:
            await stop(sleeper, changed)

    async def find(self, bot: str, wanted: str) -> tuple[ReportedEvent, str | None] | None:
        """Return the kept event that answerEvent names, with its Account link token, or None.

        None also for a deleted message, which no person caused. Raise StoredEventError for a stored body that is not
        an event's.
        """
        stored = await self._store.find_event(bot, wanted, self._clock.now())
        if stored is None:
            return None
        try:
            event = Event.model_validate_json(stored.body, strict=True).root
        except ValidationError:
            raise StoredEventError("a stored event is not the JSON of an event") from None
        if isinstance(event, MessageDeletedEvent):
            return None
        reported = ReportedEvent(
            id=event.id,
            type=event.type,
            message_id=event.message_id if isinstance(event, PressEvent | ThreadMessageEvent) else None,
            thread_ref=event.thread_ref if isinstance(event, ThreadMessageEvent) else None,
            user=event.user,
            chat=stored.chat,
            occurred_at=event.occurred_at.astimezone(UTC),
        )
        return reported, event.token if isinstance(event, AccountLinkEvent) else None
