# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""Shared helpers of the tests: a fake clock, a fake messenger, a failing store, push callbacks and a harness."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any, Protocol

import httpx
import pytest

from muster_bridge import (
    AnswerShownShownAs,
    Bot,
    BridgeApp,
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
    KeyClaim,
    Limits,
    MemoryStore,
    MessageContent,
    MessageEdited,
    MessageInput,
    MessagePosted,
    Person,
    PushTarget,
    ReportedEvent,
    Store,
    StoredAnswer,
    StoredEvent,
    ThreadReplyInput,
    ThreadReplyPosted,
    idempotency_key,
)
from muster_bridge.models import Problem

TOKEN_MAIN = "token-main"
TOKEN_OTHER = "token-other"
TEST_SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
"""A push secret: whsec_ and the base64 of 32 bytes."""
ALICE = Person(external_id="u-alice", display_name="Alice Smith", handle="alice")
LINK_TOKEN = "Ab0_-" * 8 + "xyz"
"""An Account link token: 43 characters of base64url."""
START = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)

LINK_JSON = '"link":{"url":"https://muster.example.org/alert-groups/1","label":"Open in Muster"}'
POST_BODY = (
    '{"channel_id":"alerts","kind":"root","status":"firing","color":"#d32f2f","emoji":"x",'
    '"title":"#42 HighErrorRate",' + LINK_JSON + ',"body":"b","buttons":[],"mentions":[],"notify":true}'
)
EDIT_BODY = (
    '{"status":"acknowledged","color":"#f57c00","emoji":"x","title":"#42 HighErrorRate",'
    + LINK_JSON
    + ',"body":"b","buttons":[]}'
)
REPLY_BODY = '{"thread_ref":null,"body":"b","buttons":[],"mentions":[],"notify":false}'
DM_BODY = '{"user_external_id":"u-alice","body":"Your code is 123456."}'


def with_body(body: str, text: str) -> str:
    """Return a request body with its "body" replaced by text."""
    return body.replace('"body":"b"', '"body":' + json.dumps(text), 1)


def with_buttons(body: str, n: int, data: str) -> str:
    """Return a request body with n buttons whose data is data."""
    buttons = ",".join(f'{{"label":"B{i}","style":"primary","data":{json.dumps(data)}}}' for i in range(n))
    return body.replace('"buttons":[]', f'"buttons":[{buttons}]', 1)


def answer_body(event_id: str) -> str:
    """Return the body of an answerEvent request for the event."""
    return json.dumps({"event_id": event_id, "text": "Done"})


def event_id_of(bot: str, event_type: str, update_id: str) -> str:
    """Return the id of an event reported with a messenger's update id, computed apart from the library."""
    digest = hashlib.sha256(f"{bot}\x00{event_type}\x00{update_id}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, the one event loop the library supports."""
    return "asyncio"


@dataclass(eq=False)
class _Sleeper:
    deadline: datetime
    seconds: float
    future: asyncio.Future[None]


class FakeClock:
    """The library's clock in tests: time moves only with advance, which wakes the sleepers that are due."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self._sleepers: list[_Sleeper] = []
        self._changed = asyncio.Event()

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        sleeper = _Sleeper(self._now + timedelta(seconds=seconds), seconds, asyncio.get_running_loop().create_future())
        self._sleepers.append(sleeper)
        self._signal()
        try:
            await sleeper.future
        finally:
            if sleeper in self._sleepers:
                self._sleepers.remove(sleeper)
            self._signal()

    def advance(self, seconds: float) -> None:
        """Move the clock and wake every sleeper that is due, the earliest first."""
        self._now += timedelta(seconds=seconds)
        for sleeper in sorted(self._sleepers, key=lambda s: s.deadline):
            if sleeper.deadline <= self._now:
                self._sleepers.remove(sleeper)
                if not sleeper.future.done():
                    sleeper.future.set_result(None)
        self._signal()

    def pending(self) -> int:
        return len(self._sleepers)

    async def wait_for_sleep(self, seconds: float) -> None:
        """Wait until a sleep of exactly seconds is pending; the 10 s are a guard against a hang, not a wait."""
        async with asyncio.timeout(10):
            while not any(s.seconds == seconds for s in self._sleepers):
                await self._changed.wait()

    async def wait_for_no_sleeps(self) -> None:
        """Wait until no sleep is pending; the 10 s are a guard."""
        async with asyncio.timeout(10):
            while self._sleepers:
                await self._changed.wait()

    async def settle(self, seconds: float) -> None:
        """Wait for a sleep of seconds, end it, and let the woken tasks run."""
        await self.wait_for_sleep(seconds)
        self.advance(seconds)
        for _ in range(5):
            await asyncio.sleep(0)

    def _signal(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()


@dataclass
class Call:
    """A call of the fake messenger."""

    method: str
    bot: str
    message_id: str | None = None
    payload: object = None
    key: str | None = field(default_factory=idempotency_key)
    event: ReportedEvent | None = None
    text: str | None = None


def raising(error: BaseException) -> Callable[..., Awaitable[Any]]:
    """Return an override of a fake messenger's method that raises error."""

    async def override(*_: object) -> Any:
        raise error

    return override


def answering(value: object) -> Callable[..., Awaitable[Any]]:
    """Return an override of a fake messenger's method that answers value."""

    async def override(*_: object) -> Any:
        return value

    return override


class FakeMessenger:
    """Implements every protocol of a messenger; each call is recorded and answered by its override, or by default."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.post: Callable[[str, MessageInput], Awaitable[MessagePosted]] | None = None
        self.edit: Callable[[str, str, MessageContent], Awaitable[MessageEdited]] | None = None
        self.reply: Callable[[str, str, ThreadReplyInput], Awaitable[ThreadReplyPosted]] | None = None
        self.check: Callable[[str, str], Awaitable[str]] | None = None
        self.answer: Callable[[str, ReportedEvent, str], Awaitable[AnswerShownShownAs]] | None = None
        self.find: Callable[[str, str], Awaitable[Person | None]] | None = None
        self.dm: Callable[[str, DirectMessageInput], Awaitable[DirectMessageSent]] | None = None
        self.delete: Callable[[str, str], Awaitable[None]] | None = None
        self.entered = asyncio.Event()
        """Set by every call."""
        self.block: asyncio.Event | None = None
        """When set, every call waits for it."""

    def count(self, method: str) -> int:
        return sum(1 for call in self.calls if call.method == method)

    async def _record(self, call: Call) -> None:
        self.calls.append(call)
        self.entered.set()
        if self.block is not None:
            await self.block.wait()

    async def post_message(self, bot: str, message: MessageInput) -> MessagePosted:
        await self._record(Call("post_message", bot, payload=message))
        if self.post is not None:
            return await self.post(bot, message)
        return MessagePosted(message_id="m1")

    async def edit_message(self, bot: str, message_id: str, content: MessageContent) -> MessageEdited:
        await self._record(Call("edit_message", bot, message_id, content))
        if self.edit is not None:
            return await self.edit(bot, message_id, content)
        return MessageEdited()

    async def reply_in_thread(self, bot: str, message_id: str, reply: ThreadReplyInput) -> ThreadReplyPosted:
        await self._record(Call("reply_in_thread", bot, message_id, reply))
        if self.reply is not None:
            return await self.reply(bot, message_id, reply)
        return ThreadReplyPosted(message_id="r1", thread_ref=f"t-{message_id}")

    async def check_channel(self, bot: str, channel_id: str) -> str:
        await self._record(Call("check_channel", bot, channel_id))
        if self.check is not None:
            return await self.check(bot, channel_id)
        return "Alerts"

    async def answer_event(self, bot: str, event: ReportedEvent, text: str) -> AnswerShownShownAs:
        await self._record(Call("answer_event", bot, event=event, text=text))
        if self.answer is not None:
            return await self.answer(bot, event, text)
        return AnswerShownShownAs.thread

    async def find_user(self, bot: str, handle: str) -> Person | None:
        await self._record(Call("find_user", bot, payload=handle))
        if self.find is not None:
            return await self.find(bot, handle)
        return ALICE if handle == "alice" else None

    async def send_direct_message(self, bot: str, message: DirectMessageInput) -> DirectMessageSent:
        await self._record(Call("send_direct_message", bot, payload=message))
        if self.dm is not None:
            return await self.dm(bot, message)
        return DirectMessageSent(message_id="d1")

    async def delete_message(self, bot: str, message_id: str) -> None:
        await self._record(Call("delete_message", bot, message_id))
        if self.delete is not None:
            await self.delete(bot, message_id)


class MinimalMessenger:
    """Has the methods of Messenger and no other."""

    async def post_message(self, bot: str, message: MessageInput) -> MessagePosted:
        return MessagePosted(message_id="m1")

    async def edit_message(self, bot: str, message_id: str, content: MessageContent) -> MessageEdited:
        return MessageEdited()

    async def reply_in_thread(self, bot: str, message_id: str, reply: ThreadReplyInput) -> ThreadReplyPosted:
        return ThreadReplyPosted(message_id="r1", thread_ref="t1")

    async def check_channel(self, bot: str, channel_id: str) -> str:
        return channel_id

    async def answer_event(self, bot: str, event: ReportedEvent, text: str) -> AnswerShownShownAs:
        return AnswerShownShownAs.thread


def all_capabilities() -> Capabilities:
    """Return the capabilities of every feature."""
    return Capabilities(
        buttons=CapabilitiesButtons(
            max_per_message=3, max_per_row=3, data_max_bytes=64, styles=[ButtonStyle.primary, ButtonStyle.danger]
        ),
        thread_messages=CapabilitiesThreadMessages.all,
        direct_messages=CapabilitiesDirectMessages(bot_can_start=True),
        private_answers=[CapabilitiesPrivateAnswer.ephemeral],
        native_mentions=True,
        quiet_messages=True,
        status_color=True,
        delete=CapabilitiesDelete(max_age_seconds=None),
        deletion_events=True,
    )


def make_config(**changes: Any) -> Config:
    """Return the configuration of a bridge with two bots, main and other, and every feature, with the changes."""
    config = Config(
        name="test bridge",
        version="1.0.0",
        messenger_name="Test",
        bots=two_bots(),
        capabilities=all_capabilities(),
        limits=Limits(max_body_length=500, edit_max_age_seconds=None),
        user_lookup="login",
    )
    return dataclasses.replace(config, **changes)


@dataclass
class Pushed:
    """A push that a callback got."""

    id: str
    timestamp: str
    body: bytes
    headers: httpx.Headers
    status: int


class Callback:
    """Stands in for Muster's push callback: it checks every push on its own and answers from its script.

    The signature and the timestamp are checked as a receiver of Standard Webhooks checks them; what fails is listed
    in problems, which the callbacks fixture reports at the end of the test.
    """

    def __init__(self, clock: FakeClock, host: str, secret: str, script: Sequence[int]) -> None:
        self.clock = clock
        self.host = host
        self.secret = secret
        self.key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        self.script = list(script)
        self.status_of: Callable[[str], int] | None = None
        self.hold_first = 0
        """The number of pushes, from the first, that get no answer: only the pusher's timeout ends them."""
        self.in_flight = 0
        self.max_flight = 0
        self.redirected = 0
        self.answer_chunks = 0
        """The number of chunks of 16 KiB that each answer streams."""
        self.chunks_read = 0
        self.problems: list[str] = []
        self.got: asyncio.Queue[Pushed] = asyncio.Queue()

    def target(self) -> PushTarget:
        return PushTarget(url=f"http://{self.host}/api/v1/callbacks/bridge/c1", secret=self.secret)

    def signed(self, msg_id: str, timestamp: str, body: bytes, header: str) -> bool:
        expected = hmac.new(self.key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
        for entry in header.split():
            version, _, encoded = entry.partition(",")
            try:
                mac = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if version == "v1" and hmac.compare_digest(mac, expected):
                return True
        return False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/elsewhere":
            self.redirected += 1
            return httpx.Response(HTTPStatus.OK)
        self.in_flight += 1
        self.max_flight = max(self.max_flight, self.in_flight)
        try:
            return await self._serve(request)
        finally:
            self.in_flight -= 1

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        headers = request.headers
        pushed = Pushed(
            headers.get("webhook-id", ""), headers.get("webhook-timestamp", ""), request.content, headers, 0
        )
        if not self.signed(pushed.id, pushed.timestamp, pushed.body, headers.get("webhook-signature", "")):
            self.problems.append(f"push {pushed.id}: the webhook-signature is not a v1 signature with the secret")
        if pushed.timestamp != (now := str(int(self.clock.now().timestamp()))):
            self.problems.append(f"push {pushed.id}: webhook-timestamp {pushed.timestamp}, want the clock's {now}")
        try:
            body_id = json.loads(pushed.body).get("id")
        except ValueError:
            body_id = None
        if body_id != pushed.id:
            self.problems.append(f"push {pushed.id}: the body's id is {body_id!r}")
        if (request.method, headers.get("content-type"), headers.get("user-agent")) != (
            "POST",
            "application/json",
            "muster-bridge-python",
        ):
            self.problems.append(f"push {pushed.id}: {request.method} {headers}")
        if self.script:
            pushed.status = self.script.pop(0)
        elif self.status_of is not None:
            pushed.status = self.status_of(pushed.id)
        else:
            pushed.status = HTTPStatus.NO_CONTENT
        hold = self.hold_first > 0
        if hold:
            self.hold_first -= 1
        self.got.put_nowait(pushed)
        if hold:
            await asyncio.Event().wait()
        location = {"Location": "/elsewhere"} if pushed.status == HTTPStatus.FOUND else {}
        return httpx.Response(pushed.status, headers=location, content=self._answer())

    async def _answer(self) -> AsyncIterator[bytes]:
        for _ in range(self.answer_chunks):
            self.chunks_read += 1
            yield b"x" * 16384

    async def next(self) -> Pushed:
        """Return the next push; the 10 s are a guard against a hang, not a wait."""
        async with asyncio.timeout(10):
            return await self.got.get()

    def none(self) -> None:
        """Check that no push is waiting."""
        assert self.got.empty(), f"unexpected push {self.got.get_nowait().id}"


class Callbacks:
    """The transport of the bridges' pushes: each host is a Callback; another host cannot be reached."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.hosts: dict[str, Callback] = {}
        self.transport = httpx.MockTransport(self._route)

    def add(self, *script: int, secret: str = TEST_SECRET) -> Callback:
        """Return a new callback that answers the statuses of the script, then 204."""
        host = f"callback{len(self.hosts) + 1}.example.org"
        self.hosts[host] = Callback(self.clock, host, secret, script)
        return self.hosts[host]

    async def _route(self, request: httpx.Request) -> httpx.Response:
        callback = self.hosts.get(request.url.host)
        if callback is None:
            raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)
        return await callback.handle(request)


@pytest.fixture
def clock() -> FakeClock:
    """Return the fake clock of the test's bridges and callbacks."""
    return FakeClock()


@pytest.fixture
def callbacks(clock: FakeClock) -> Iterator[Callbacks]:
    """Return the push callbacks of the test, and fail it for every push they found wrong."""
    made = Callbacks(clock)
    yield made
    problems = [problem for callback in made.hosts.values() for problem in callback.problems]
    assert problems == []


@dataclass
class Polled:
    """What one pollEvents answered: the events, the cursor to send next, and the answer as it was sent."""

    events: list[dict[str, Any]]
    cursor: str
    text: str


def two_bots(main: PushTarget | None = None, other: PushTarget | None = None) -> list[Bot]:
    """Return the bots of make_config, main and other, with the push targets."""
    return [Bot(name="main", token=TOKEN_MAIN, push=main), Bot(name="other", token=TOKEN_OTHER, push=other)]


@dataclass
class Harness:
    """A bridge on a fake clock with a fake messenger, and a client that calls it in process."""

    app: BridgeApp
    messenger: FakeMessenger
    clock: FakeClock
    store: Store
    client: httpx.AsyncClient
    callbacks: Callbacks

    async def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = TOKEN_MAIN,
        key: str | None = None,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a request with the token, the Idempotency-Key and the body as JSON, as the ones given."""
        sent: dict[str, str] = {}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        if key is not None:
            sent["Idempotency-Key"] = key
        if body is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        content = body.encode() if body is not None else None
        return await self.client.request(method, path, headers=sent, content=content)

    async def poll(self, query: str = "", token: str = TOKEN_MAIN) -> Polled:
        """Poll the events of the token's bot and check that the answer is a batch."""
        response = await self.call("GET", "/v1/events" + query, token=token)
        assert response.status_code == HTTPStatus.OK, response.text
        assert response.headers["content-type"] == "application/json"
        batch = response.json()
        assert isinstance(batch["events"], list)
        return Polled(batch["events"], batch["next_cursor"], response.text)

    async def report_press(self, update: str, bot: str = "main") -> str:
        """Report a press with the update id, and return the id of its event."""
        await self.app.press(bot, message_id="m1", user=ALICE, data="d", update_id=update)
        return event_id_of(bot, "press", update)


class MakeBridge(Protocol):
    async def __call__(
        self,
        *,
        messenger: FakeMessenger | None = None,
        store: Store | None = None,
        random: Callable[[int], bytes] | None = None,
        **changes: Any,
    ) -> Harness: ...


@pytest.fixture
async def make_bridge(clock: FakeClock, callbacks: Callbacks) -> AsyncIterator[MakeBridge]:
    """Make bridges of make_config with the changes, and close them and their clients at the end of the test."""
    made: list[Harness] = []

    async def make(
        *,
        messenger: FakeMessenger | None = None,
        store: Store | None = None,
        random: Callable[[int], bytes] | None = None,
        **changes: Any,
    ) -> Harness:
        messenger = messenger or FakeMessenger()
        store = store or MemoryStore()
        config = make_config(store=store, **changes)
        app = BridgeApp(config, messenger, _clock=clock, _random=random, _transport=callbacks.transport)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge")
        made.append(Harness(app, messenger, clock, store, client, callbacks))
        return made[-1]

    yield make
    for harness in made:
        await harness.app.aclose()
        await harness.client.aclose()


@pytest.fixture
async def bridge(make_bridge: MakeBridge) -> Harness:
    """Return a bridge of make_config."""
    return await make_bridge()


def problem_of(response: httpx.Response, status: int, error_class: str) -> dict[str, Any]:
    """Check the status, media type and class of a problem answer, and return the problem."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json", response.text
    Problem.model_validate_json(response.content, strict=True)
    problem: dict[str, Any] = response.json()
    assert problem["class"] == error_class, response.text
    assert problem["status"] == status
    assert problem["title"]
    return problem


class FailingStore:
    """A MemoryStore whose methods fail while their error is set."""

    def __init__(self) -> None:
        self.inner = MemoryStore()
        self.errors: dict[str, Exception] = {}
        self.claim: KeyClaim | None = None
        self.before: Callable[[str], Awaitable[None]] | None = None

    def fail(self, method: str, error: Exception | None) -> None:
        """Make the method fail with error, or work again with None."""
        if error is None:
            self.errors.pop(method, None)
        else:
            self.errors[method] = error

    def claim_as(self, claim: KeyClaim | None) -> None:
        """Make claim_key answer claim, or the inner store again with None."""
        self.claim = claim

    async def _check(self, method: str) -> None:
        if self.before is not None:
            await self.before(method)
        if (error := self.errors.get(method)) is not None:
            raise error

    async def claim_key(self, bot: str, key: str, now: datetime, until: datetime) -> KeyClaim:
        await self._check("claim_key")
        if self.claim is not None:
            return self.claim
        return await self.inner.claim_key(bot, key, now, until)

    async def save_key(self, bot: str, key: str, claim_id: str, answer: StoredAnswer, until: datetime) -> None:
        await self._check("save_key")
        await self.inner.save_key(bot, key, claim_id, answer, until)

    async def release_key(self, bot: str, key: str, claim_id: str) -> None:
        await self._check("release_key")
        await self.inner.release_key(bot, key, claim_id)

    async def append_event(self, bot: str, event: StoredEvent, now: datetime) -> bool:
        await self._check("append_event")
        return await self.inner.append_event(bot, event, now)

    async def events_after(self, bot: str, cursor: str, limit: int, now: datetime) -> tuple[list[StoredEvent], str]:
        await self._check("events_after")
        return await self.inner.events_after(bot, cursor, limit, now)

    async def find_event(self, bot: str, event_id: str, now: datetime) -> StoredEvent | None:
        await self._check("find_event")
        return await self.inner.find_event(bot, event_id, now)


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Return the log lines of the test as "LEVEL message"."""
    return "\n".join(f"{record.levelname} {record.getMessage()}" for record in caplog.records)
