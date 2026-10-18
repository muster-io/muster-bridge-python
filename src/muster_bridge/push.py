# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The pushers: each bot's events posted to its push target, signed as Standard Webhooks specifies."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx

from muster_bridge.events import running_loop, stop
from muster_bridge.problems import describe
from muster_bridge.store import UnknownCursorError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from muster_bridge.config import Clock, ParsedTarget
    from muster_bridge.events import BotSignal, Events
    from muster_bridge.problems import Redactor
    from muster_bridge.store import Store, StoredEvent

__all__: list[str] = []

ATTEMPT_TIMEOUT = 10.0
"""Bounds one post of an event to the callback, in seconds."""
RETRY_WINDOW = timedelta(hours=1)
"""How long an event is retried before the pusher moves on and leaves it to polling."""
FIRST_BACKOFF = 1.0
MAX_BACKOFF = 300.0
DRAIN_BYTES = 65536
USER_AGENT = "muster-bridge-python"


def webhook_signature(key: bytes, msg_id: str, timestamp: str, payload: bytes) -> str:
    """Return the webhook-signature header of a Standard Webhooks message.

    It is "v1," and the base64 of the HMAC-SHA256 that key makes of the message id, the timestamp and the payload,
    joined by full stops.
    """
    mac = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + payload, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


@dataclass(slots=True)
class _BotPush:
    """Where a bot's events go, and the task that pushes them."""

    target: ParsedTarget | None
    retarget: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the target changes while the pusher runs; each pusher gets its own, in its event loop."""
    task: asyncio.Task[None] | None = None

    def running(self) -> bool:
        """Report whether the pusher runs: a task of a closed event loop never runs again."""
        return self.task is not None and not self.task.done() and not self.task.get_loop().is_closed()


class Pushers:
    """Pushes each bot's events to its target, one at a time and in order, from tasks of the running event loop.

    A pusher starts on the bot's first event, when a target is set in the running loop, or when the lifespan starts;
    it lives until aclose or until its loop ends. The pushers of a loop share one HTTP client, which the last of them
    closes as it ends.
    """

    def __init__(
        self,
        *,
        targets: dict[str, ParsedTarget | None],
        events: Events,
        store: Store,
        clock: Clock,
        logger: logging.Logger,
        redact: Redactor,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self._bots = {bot: _BotPush(target) for bot, target in targets.items()}
        self._events = events
        self._store = store
        self._clock = clock
        self._log = logger
        self._redact = redact
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def ensure(self, bot: str) -> None:
        """Start the bot's pusher unless it runs, the bot has no target or the pushers are closed; in a running loop."""
        state = self._bots[bot]
        if self._closed or state.target is None or state.running():
            return
        # An asyncio.Event belongs to the event loop that first waits on it, so a pusher on a new loop needs a new one.
        state.retarget = asyncio.Event()
        state.task = asyncio.get_running_loop().create_task(self._run(bot, state), name=f"muster_bridge pusher {bot}")

    def start_all(self) -> None:
        """Start the pusher of every bot with a target."""
        for bot in self._bots:
            self.ensure(bot)

    def set_target(self, bot: str, target: ParsedTarget | None) -> None:
        """Push the bot's events to target from now on; None stops pushing.

        A pusher that runs continues where it was, towards the new target; one that this starts begins with the oldest
        event kept. Outside a running loop the pusher starts on the bot's next event or when the lifespan starts.
        """
        state = self._bots[bot]
        state.target = target
        if state.running():
            state.retarget.set()
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.ensure(bot)

    async def aclose(self) -> None:
        """Stop every pusher, whose end closes the HTTP client of its event loop; the events stay queued for polling."""
        self._closed = True
        await stop(*(state.task for state in self._bots.values()))

    async def _run(self, bot: str, state: _BotPush) -> None:
        loop = asyncio.get_running_loop()
        signal = self._events.signal(bot)
        cursor = ""
        try:
            while True:
                try:
                    cursor = await self._step(bot, state, signal, cursor)
                except Exception as error:  # a fault of a store or of the library never ends the pusher
                    self._log.error("the pusher failed: bot=%s error=%s", bot, self._redact(describe(error)))
                    await self._until_retarget(state, functools.partial(self._clock.sleep, FIRST_BACKOFF))
        finally:
            # Not when the pusher of a closed event loop is freed: nothing can run in that loop any more.
            if running_loop() is loop:
                await self._close_client(loop)

    async def _close_client(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close the loop's HTTP client once no other pusher of the loop runs: its connections belong to the loop."""
        if self._client is None or self._client_loop is not loop:
            return
        me = asyncio.current_task()
        for state in self._bots.values():
            task = state.task
            if task is not None and task is not me and not task.done() and task.get_loop() is loop:
                return
        client, self._client = self._client, None
        await client.aclose()

    async def _step(self, bot: str, state: _BotPush, signal: BotSignal, cursor: str) -> str:
        """Push the event after the cursor, or wait for one; return the cursor to go on from."""
        if state.target is None:
            await state.retarget.wait()
            state.retarget.clear()
            return cursor
        seen = signal.generation
        try:
            events, next_cursor = await self._store.events_after(bot, cursor, 1, self._clock.now())
        except Exception as error:
            if isinstance(error, UnknownCursorError) and cursor:
                self._log.warning("the store forgot the push cursor, pushing every kept event: bot=%s", bot)
                return ""
            self._log.error("the store failed: operation=push bot=%s error=%s", bot, self._redact(describe(error)))
            await self._until_retarget(state, functools.partial(self._clock.sleep, FIRST_BACKOFF))
            return cursor
        if not events:
            await self._until_retarget(state, functools.partial(signal.wait_changed, seen))
            return next_cursor
        if await self._deliver(bot, state, events[0]):
            return next_cursor
        return cursor

    async def _until_retarget(self, state: _BotPush, work: Callable[[], Awaitable[None]]) -> bool:
        """Run work until it ends or the target changes; return whether it ended."""
        if state.retarget.is_set():
            state.retarget.clear()
            return False
        task = asyncio.ensure_future(work())
        retargeted = asyncio.ensure_future(state.retarget.wait())
        try:
            await asyncio.wait({task, retargeted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await stop(task, retargeted)
        if retargeted.done() and not retargeted.cancelled():
            state.retarget.clear()
            return False
        task.result()  # raises what the work raised
        return True

    async def _deliver(self, bot: str, state: _BotPush, event: StoredEvent) -> bool:
        """Push the event until Muster takes or refuses it, or for at least an hour, under the same webhook-id.

        Return False when it stopped without an outcome, because the bot has no target any more.
        """
        first = self._clock.now()
        delay = FIRST_BACKOFF
        attempt = 1
        while True:
            target = state.target
            if target is None:
                return False
            status, error = await self._attempt(target, event)
            if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                return True
            if status == HTTPStatus.BAD_REQUEST:
                self._log.warning("Muster refused a pushed event for good: bot=%s event_id=%s", bot, event.id)
                return True
            if self._clock.now() - first >= RETRY_WINDOW:
                self._log.warning(
                    "gave up pushing an event, it stays for polling: bot=%s event_id=%s attempts=%d",
                    bot,
                    event.id,
                    attempt,
                )
                return True
            level = logging.WARNING if attempt == 1 else logging.DEBUG
            if error is None:
                self._log.log(
                    level,
                    "a push failed, retrying: bot=%s event_id=%s attempt=%d status=%d",
                    bot,
                    event.id,
                    attempt,
                    status,
                )
            else:
                self._log.log(
                    level,
                    "a push failed, retrying: bot=%s event_id=%s attempt=%d error=%s",
                    bot,
                    event.id,
                    attempt,
                    self._redact(error),
                )
            if not await self._until_retarget(state, functools.partial(self._clock.sleep, delay)):
                first, delay, attempt = self._clock.now(), FIRST_BACKOFF, 1
                continue
            delay = min(2 * delay, MAX_BACKOFF)
            attempt += 1

    async def _attempt(self, target: ParsedTarget, event: StoredEvent) -> tuple[int, str | None]:
        """Post the event once and return the answer's status, or 0 and why there is none.

        A sleep of the library's clock ends the attempt after ATTEMPT_TIMEOUT seconds.
        """
        sent_at = str(int(self._clock.now().timestamp()))
        headers = {
            "Content-Type": "application/json",
            "webhook-id": event.id,
            "webhook-timestamp": sent_at,
            "webhook-signature": webhook_signature(target.key, event.id, sent_at, event.body),
        }
        request = asyncio.ensure_future(self._post(target.url, event.body, headers))
        timer = asyncio.ensure_future(self._clock.sleep(ATTEMPT_TIMEOUT))
        try:
            await asyncio.wait({request, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await stop(request, timer)
        if request.cancelled():
            return 0, f"the push timed out after {ATTEMPT_TIMEOUT:g} seconds"
        if (error := request.exception()) is not None:
            return 0, describe(error)
        return request.result(), None

    def _http(self) -> httpx.AsyncClient:
        """Return the HTTP client of the pushes, made on the first push in each event loop."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            # A client's connections belong to the loop that opened them, so a pusher on another loop gets a client
            # of its own. The last pusher of a loop closes the loop's client as it ends; only that of a loop closed
            # with its pushers still pending is left to the garbage collector.
            self._client_loop = loop
            # No redirects: Muster's callback never redirects. The timeout backs up the clock's per-attempt timeout.
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(ATTEMPT_TIMEOUT + 5),
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def _post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        async with self._http().stream("POST", url, content=body, headers=headers) as response:
            # Only the status counts; reading a little of the body lets the client reuse the connection.
            drained = 0
            async for chunk in response.aiter_bytes():
                drained += len(chunk)
                if drained >= DRAIN_BYTES:
                    break
            return response.status_code
