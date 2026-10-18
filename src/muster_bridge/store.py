# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""Where the library keeps the first answers of Idempotency-Keys and the queued events."""

from __future__ import annotations

import base64
import enum
import heapq
import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from itertools import count
from operator import itemgetter
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from muster_bridge.messenger import EventType

__all__ = ["KeyClaim", "KeyState", "MemoryStore", "Store", "StoredAnswer", "StoredEvent", "UnknownCursorError"]


class KeyState(enum.Enum):
    """The outcome of Store.claim_key."""

    CLAIMED = "claimed"
    """The key was free and the caller runs the request."""
    RUNNING = "running"
    """Another request with the key is still running."""
    ANSWERED = "answered"
    """The key has a stored answer, in KeyClaim.answer."""


@dataclass(frozen=True, slots=True)
class StoredAnswer:
    """The first successful answer of an Idempotency-Key."""

    status: int
    body: bytes
    """The JSON of the first answer, with replayed false."""


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyClaim:
    """The result of Store.claim_key."""

    state: KeyState
    answer: StoredAnswer | None = None
    """The stored answer, with ANSWERED."""
    claim_id: str = ""
    """Names the claim, with CLAIMED; the library gives it back to save_key and release_key."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredEvent:
    """An event queued for a bot."""

    id: str
    type: EventType
    body: bytes
    """The event exactly as pollEvents and the push send it."""
    chat: str | None
    """What the bridge gave with the report; it is never sent to Muster."""
    until: datetime
    """The time until which the event is kept, at least."""


class UnknownCursorError(LookupError):
    """A cursor that the store did not give for the bot."""


class Store(Protocol):
    """Keeps, per bot, the first answers of Idempotency-Keys and the queued events.

    Every method is given the time of the library's clock. A bridge that must keep its keys and events across restarts
    implements it over its own database; MemoryStore keeps them in memory.
    """

    async def claim_key(self, bot: str, key: str, now: datetime, until: datetime) -> KeyClaim:
        """Claim the key of the bot, or say why not.

        Return ANSWERED with the answer when one is stored for (bot, key) until after now; else RUNNING when another
        claim holds the key until after now; else claim the key until `until` and return CLAIMED with an id that
        tells this claim from any later claim of the same key.
        """
        ...

    async def save_key(self, bot: str, key: str, claim_id: str, answer: StoredAnswer, until: datetime) -> None:
        """Store the answer of (bot, key), kept until `until`, in place of the claim claim_id.

        When nothing holds the key, because the claim expired and was forgotten, store the answer all the same. When
        another claim or an answer holds the key, leave it as it is: the first answer stays, and so does a newer
        request's claim.
        """
        ...

    async def release_key(self, bot: str, key: str, claim_id: str) -> None:
        """Remove the claim claim_id of (bot, key), so that a retry with the key runs again.

        Remove nothing else: not an answer, and not the claim of a request that took the key over after claim_id
        expired.
        """
        ...

    async def append_event(self, bot: str, event: StoredEvent, now: datetime) -> bool:
        """Queue the event for the bot unless an event with its id is kept, and return whether it was queued."""
        ...

    async def events_after(self, bot: str, cursor: str, limit: int, now: datetime) -> tuple[list[StoredEvent], str]:
        """Return up to limit of the bot's kept events, oldest first, that follow cursor, and the cursor after them.

        "" starts before the oldest; a limit of 0 or less returns every one. Returning none, give cursor back
        unchanged, or, for "", the cursor at the end of the queue. Raise UnknownCursorError for a cursor that this
        store did not give for the bot.
        """
        ...

    async def find_event(self, bot: str, event_id: str, now: datetime) -> StoredEvent | None:
        """Return the kept event of the bot with the id, or None."""
        ...


@dataclass(slots=True)
class _KeyEntry:
    """A claim, named by claim_id, or the stored answer of a key."""

    until: datetime
    claim_id: str = ""
    answer: StoredAnswer | None = None


@dataclass(slots=True)
class _Queue:
    """The events of one bot, numbered from 1; a cursor is the epoch and the number of the last event read."""

    epoch: str
    head: int = 0
    events: list[tuple[int, StoredEvent]] = field(default_factory=list)
    by_id: dict[str, int] = field(default_factory=dict)

    def purge(self, now: datetime) -> None:
        """Forget the expired events at the front of the queue."""
        n = 0
        for seq, event in self.events:
            if event.until > now:
                break
            if self.by_id.get(event.id) == seq:
                del self.by_id[event.id]
            n += 1
        del self.events[:n]

    def find(self, seq: int) -> StoredEvent | None:
        """Return the event numbered seq, if it is still queued."""
        i = bisect_left(self.events, seq, key=itemgetter(0))
        if i < len(self.events) and self.events[i][0] == seq:
            return self.events[i][1]
        return None

    def cursor(self, seq: int) -> str:
        """Return the cursor after the event numbered seq."""
        return f"{self.epoch}.{seq}"

    def parse(self, cursor: str) -> int:
        """Return the number of a cursor of this queue, which is known when it is not past the head."""
        epoch, dot, number = cursor.partition(".")
        if not dot or epoch != self.epoch or not number.isascii() or not number.isdigit() or int(number) > self.head:
            raise UnknownCursorError("the cursor is not one of this store's")
        return int(number)


class MemoryStore:
    """A Store that keeps keys and events in memory, for one process and one event loop at a time.

    A restart forgets them, and Muster then polls again without a cursor. Its memory is bounded by time, not by size:
    it holds every event of the last 24 hours, whatever Muster has already polled or acknowledged (a Thread message
    carries up to 4000 characters), and the answers of the Idempotency-Keys of the last 24 hours. A bridge whose day of
    traffic does not fit in memory implements Store over its database, which may also drop the events before the cursor
    that Muster last sent. No method awaits, so each call is atomic on the event loop.
    """

    def __init__(self) -> None:
        self._keys: dict[tuple[str, str], _KeyEntry] = {}
        self._key_expiry: list[tuple[datetime, int, tuple[str, str]]] = []
        self._expiry_order = count()
        self._claims = count(1)
        self._queues: dict[str, _Queue] = {}

    async def claim_key(self, bot: str, key: str, now: datetime, until: datetime) -> KeyClaim:
        """See Store.claim_key."""
        self._purge_keys(now)
        entry = self._keys.get((bot, key))
        if entry is not None and entry.until > now:
            if entry.answer is not None:
                return KeyClaim(state=KeyState.ANSWERED, answer=entry.answer)
            return KeyClaim(state=KeyState.RUNNING)
        claim_id = str(next(self._claims))
        self._set_key((bot, key), _KeyEntry(until=until, claim_id=claim_id))
        return KeyClaim(state=KeyState.CLAIMED, claim_id=claim_id)

    async def save_key(self, bot: str, key: str, claim_id: str, answer: StoredAnswer, until: datetime) -> None:
        """See Store.save_key."""
        held = self._keys.get((bot, key))
        if held is not None and (held.answer is not None or held.claim_id != claim_id):
            return
        self._set_key((bot, key), _KeyEntry(until=until, answer=answer))

    async def release_key(self, bot: str, key: str, claim_id: str) -> None:
        """See Store.release_key."""
        held = self._keys.get((bot, key))
        if held is not None and held.answer is None and held.claim_id == claim_id:
            del self._keys[bot, key]

    async def append_event(self, bot: str, event: StoredEvent, now: datetime) -> bool:
        """See Store.append_event."""
        queue = self._queue(bot, now)
        seq = queue.by_id.get(event.id)
        if seq is not None:
            kept = queue.find(seq)
            if kept is not None and kept.until > now:
                return False
        queue.head += 1
        queue.events.append((queue.head, event))
        queue.by_id[event.id] = queue.head
        return True

    async def events_after(self, bot: str, cursor: str, limit: int, now: datetime) -> tuple[list[StoredEvent], str]:
        """See Store.events_after."""
        queue = self._queue(bot, now)
        after = queue.parse(cursor) if cursor else 0
        if limit <= 0:
            limit = len(queue.events)
        out: list[StoredEvent] = []
        last = after
        for i in range(bisect_right(queue.events, after, key=itemgetter(0)), len(queue.events)):
            if len(out) >= limit:
                break
            seq, event = queue.events[i]
            if event.until > now:
                out.append(event)
                last = seq
        if out:
            return out, queue.cursor(last)
        if not cursor:
            return [], queue.cursor(queue.head)
        return [], cursor

    async def find_event(self, bot: str, event_id: str, now: datetime) -> StoredEvent | None:
        """See Store.find_event."""
        queue = self._queues.get(bot)
        if queue is None:
            return None
        queue.purge(now)
        seq = queue.by_id.get(event_id)
        if seq is None:
            return None
        event = queue.find(seq)
        if event is None or event.until <= now:
            return None
        return event

    def _set_key(self, ref: tuple[str, str], entry: _KeyEntry) -> None:
        self._keys[ref] = entry
        heapq.heappush(self._key_expiry, (entry.until, next(self._expiry_order), ref))

    def _purge_keys(self, now: datetime) -> None:
        """Forget the keys whose time ended; a later entry of the same key has its own expiry."""
        while self._key_expiry and self._key_expiry[0][0] <= now:
            until, _, ref = heapq.heappop(self._key_expiry)
            entry = self._keys.get(ref)
            if entry is not None and entry.until == until:
                del self._keys[ref]

    def _queue(self, bot: str, now: datetime) -> _Queue:
        """Return the bot's queue, purged of expired events.

        A queue is made on first use with an epoch of its own, so that the cursors of another bot or of an earlier
        process are unknown.
        """
        queue = self._queues.get(bot)
        if queue is None:
            queue = self._queues[bot] = _Queue(epoch=base64.urlsafe_b64encode(os.urandom(6)).decode())
        queue.purge(now)
        return queue
