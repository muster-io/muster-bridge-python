# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import asyncio
import base64
import dataclasses
import gc
import weakref
from datetime import datetime
from typing import Any, cast

import httpx
import pytest
from conftest import (
    ALICE,
    TEST_SECRET,
    Callbacks,
    FailingStore,
    FakeClock,
    FakeMessenger,
    MakeBridge,
    event_id_of,
    logged,
    make_config,
    two_bots,
)
from starlette.applications import Starlette
from starlette.routing import Mount

from muster_bridge import (
    BridgeApp,
    ClosedError,
    ConfigError,
    MemoryStore,
    PushTarget,
    StoredEvent,
    UnknownBotError,
    UnknownCursorError,
)
from muster_bridge.config import parse_push_target
from muster_bridge.push import webhook_signature

pytestmark = pytest.mark.anyio

DELAYS = (1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300)
"""The back-off between the attempts of one event, in seconds."""


def pushers() -> list[asyncio.Task[Any]]:
    """Return the pusher tasks of the running loop that have not ended."""
    return [
        task for task in asyncio.all_tasks() if task.get_name().startswith("muster_bridge pusher") and not task.done()
    ]


async def run_soon() -> None:
    """Let every task that can run do so."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_push_signs_events_in_order(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    ids = [await bridge.report_press(f"u{i}") for i in range(3)]
    for want in ids:
        pushed = await callback.next()
        assert pushed.id == want
        assert pushed.headers["webhook-signature"] == webhook_signature(
            base64.b64decode(TEST_SECRET.removeprefix("whsec_")), pushed.id, pushed.timestamp, pushed.body
        )
    assert callback.max_flight == 1
    assert [event["id"] for event in (await bridge.poll("?wait=0")).events] == ids


async def test_push_one_at_a_time(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add(500)
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    first = await bridge.report_press("u1")
    second = await bridge.report_press("u2")
    assert (await callback.next()).id == first
    await clock.wait_for_sleep(1)
    callback.none()
    clock.advance(1)
    assert [(await callback.next()).id for _ in range(2)] == [first, second]


async def test_push_retries_with_backoff_under_the_same_id(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add(500, 500, 401, 503)
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    event_id = await bridge.report_press("u1")
    stamps = []
    for delay in (1, 2, 4, 8, 0):
        pushed = await callback.next()
        assert pushed.id == event_id
        stamps.append(pushed.timestamp)
        if delay:
            await clock.settle(delay)
    assert stamps[0] != stamps[4]
    log = logged(caplog)
    assert log.count("a push failed, retrying") == 4
    assert log.count("WARNING") == 1
    assert f"WARNING a push failed, retrying: bot=main event_id={event_id} attempt=1 status=500" in log
    assert f"DEBUG a push failed, retrying: bot=main event_id={event_id} attempt=4 status=503" in log


async def test_push_keeps_on_401(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add(401, 401)
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    event_id = await bridge.report_press("u1")
    await callback.next()
    await clock.settle(1)
    await callback.next()
    await clock.settle(2)
    pushed = await callback.next()
    assert (pushed.id, pushed.status) == (event_id, 204)


async def test_push_400_drops_the_event(
    make_bridge: MakeBridge, callbacks: Callbacks, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add(400)
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    first = await bridge.report_press("u1")
    second = await bridge.report_press("u2")
    pushed = await callback.next()
    assert (pushed.id, pushed.status) == (first, 400)
    assert (await callback.next()).id == second
    assert len((await bridge.poll("?wait=0")).events) == 2
    assert f"WARNING Muster refused a pushed event for good: bot=main event_id={first}" in logged(caplog)


async def test_push_timeout_retries(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add()
    callback.hold_first = 1
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    event_id = await bridge.report_press("u1")
    await callback.next()
    await clock.settle(10)
    await clock.settle(1)
    pushed = await callback.next()
    assert (pushed.id, pushed.status) == (event_id, 204)
    assert "attempt=1 error=the push timed out after 10 seconds" in logged(caplog)
    assert callback.in_flight == 0


async def test_push_reads_little_of_an_answer(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add(200, 200)
    callback.answer_chunks = 100
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    first = await bridge.report_press("u1")
    second = await bridge.report_press("u2")
    assert [(await callback.next()).id for _ in range(2)] == [first, second]
    await run_soon()
    assert 2 * 4 <= callback.chunks_read <= 2 * 5


async def test_push_backoff_caps_at_five_minutes(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock
) -> None:
    callback = callbacks.add()
    callback.status_of = lambda _: 503
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    await bridge.report_press("u1")
    for delay in DELAYS:
        await callback.next()
        await clock.settle(delay)
    await callback.next()


async def test_push_gives_up_after_an_hour(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add()
    bridge = await make_bridge()
    first = await bridge.report_press("u1")
    second = await bridge.report_press("u2")
    callback.status_of = lambda event_id: 500 if event_id == first else 204
    bridge.app.set_push("main", callback.target())
    elapsed, delay, attempts = 0, 1, 0
    while (await callback.next()).id != second:
        attempts += 1
        if elapsed >= 3600:
            continue  # the last attempt: the pusher gives up and pushes the next event at once
        await clock.settle(delay)
        elapsed += delay
        delay = min(2 * delay, 300)
    assert attempts == 21
    assert elapsed >= 3600
    assert f"WARNING gave up pushing an event, it stays for polling: bot=main event_id={first} attempts=21" in (
        logged(caplog)
    )
    assert len((await bridge.poll("?wait=0")).events) == 2


async def test_push_does_not_follow_redirects(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add(302)
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    event_id = await bridge.report_press("u1")
    assert (await callback.next()).status == 302
    await clock.settle(1)
    pushed = await callback.next()
    assert (pushed.id, pushed.status) == (event_id, 204)
    assert callback.redirected == 0


async def test_push_without_target_queues_for_later(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add()
    bridge = await make_bridge()
    first = await bridge.report_press("u1")
    second = await bridge.report_press("u2")
    await bridge.report_press("x", bot="other")
    assert pushers() == []
    bridge.app.set_push("main", callback.target())
    assert [(await callback.next()).id for _ in range(2)] == [first, second]
    third = await bridge.report_press("u3")
    assert (await callback.next()).id == third
    await run_soon()
    callback.none()


async def test_set_push_outside_the_loop_starts_on_the_next_event(callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add()

    def configure() -> BridgeApp:
        """Build a bridge and set its push target where no event loop runs, as a program does before serving."""
        app = BridgeApp(make_config(), FakeMessenger(), _clock=clock, _transport=callbacks.transport)
        app.set_push("main", callback.target())
        return app

    app = await asyncio.to_thread(configure)
    try:
        assert pushers() == []
        await app.press("main", message_id="m1", user=ALICE, data="d", update_id="u1")
        assert len(pushers()) == 1
        assert (await callback.next()).id == event_id_of("main", "press", "u1")
    finally:
        await app.aclose()


async def test_set_push_retargets(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    failing = callbacks.add()
    failing.status_of = lambda _: 500
    working = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=failing.target()))
    event_id = await bridge.report_press("u1")
    await failing.next()
    await clock.wait_for_sleep(1)
    bridge.app.set_push("main", working.target())
    assert (await working.next()).id == event_id
    failing.none()


async def test_set_push_during_an_attempt(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    held = callbacks.add()
    held.hold_first = 1
    working = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=held.target()))
    event_id = await bridge.report_press("u1")
    await held.next()
    bridge.app.set_push("main", working.target())
    await clock.settle(10)
    assert (await working.next()).id == event_id
    held.none()


async def test_set_push_none_stops_pushing(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add(500)
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    event_id = await bridge.report_press("u1")
    await callback.next()
    await clock.wait_for_sleep(1)
    bridge.app.set_push("main", None)
    await clock.wait_for_no_sleeps()
    clock.advance(3600)
    await bridge.report_press("u2")
    await run_soon()
    callback.none()
    bridge.app.set_push("main", callback.target())
    assert (await callback.next()).id == event_id


async def test_set_push_none_before_any_target_leaves_no_signal(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock
) -> None:
    callback = callbacks.add(500)
    bridge = await make_bridge()
    bridge.app.set_push("main", None)
    event_id = await bridge.report_press("u1")
    bridge.app.set_push("main", callback.target())
    await callback.next()
    await clock.wait_for_sleep(1)
    callback.none()
    clock.advance(1)
    assert (await callback.next()).id == event_id


async def test_set_push_keeps_each_secret_once(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge()
    target = PushTarget(url="https://muster.example.org/api/v1/callbacks/bridge/c1", secret=TEST_SECRET)
    redact = bridge.app._redact
    bridge.app.set_push("main", target)
    held = set(redact._variants)
    for _ in range(50):
        bridge.app.set_push("main", target)
    assert redact._variants == held
    rotated = "whsec_" + base64.b64encode(b"the rotated key of the push target").decode()
    bridge.app.set_push("other", PushTarget(url=target.url, secret=rotated))
    assert redact(f"a {rotated} b {rotated.removeprefix('whsec_')}") == "a [redacted] b [redacted]"
    assert redact(f"a {TEST_SECRET} b") == "a [redacted] b"


async def test_set_push_refuses(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge()
    with pytest.raises(UnknownBotError):
        bridge.app.set_push("nobody", None)
    with pytest.raises(ConfigError) as raised:
        bridge.app.set_push("main", PushTarget(url="ftp://x", secret="whsec_!!!secret"))
    assert raised.value.problems == (
        "the push target: the url needs the http or https scheme, a host and no user information; "
        "the secret needs the whsec_ prefix and then a non-empty key in standard base64",
    )
    assert "!!!secret" not in str(raised.value)
    poll_only = await make_bridge(event_delivery=["poll"])
    with pytest.raises(ConfigError, match="event_delivery does not offer push"):
        poll_only.app.set_push("main", PushTarget(url="https://m/cb", secret=TEST_SECRET))
    poll_only.app.set_push("main", None)
    await bridge.app.aclose()
    with pytest.raises(ClosedError):
        bridge.app.set_push("main", None)


async def test_pushers_are_tasks_of_the_running_loop(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    assert pushers() == []
    await bridge.report_press("u1")
    (task,) = pushers()
    assert task.get_name() == "muster_bridge pusher main"
    assert task.get_loop() is asyncio.get_running_loop()
    await callback.next()
    await bridge.report_press("u2")
    assert pushers() == [task]


@pytest.mark.parametrize("cancelled", [True, False], ids=["asyncio.run", "a loop closed with its tasks pending"])
def test_pushers_restart_on_another_event_loop(
    callbacks: Callbacks, clock: FakeClock, cancelled: bool, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add()
    store = FailingStore()
    calls: list[str] = []

    async def record(method: str) -> None:
        calls.append(method)

    store.before = record
    config = make_config(bots=two_bots(main=callback.target()), store=store)
    app = BridgeApp(config, FakeMessenger(), _clock=clock, _transport=callbacks.transport)
    clients: list[httpx.AsyncClient | None] = []

    async def push(update: str, pushes: int, *, close: bool) -> list[str]:
        """Report a press on the running loop and return the ids of the pushes it brings."""
        callback.got = asyncio.Queue()  # an asyncio queue belongs to the loop that first waits on it
        await app.press("main", message_id="m1", user=ALICE, data="d", update_id=update)
        ids = [(await callback.next()).id for _ in range(pushes)]
        await run_soon()
        reads = calls.count("events_after")
        await run_soon()
        assert calls.count("events_after") == reads, "the pusher reads the store again instead of waiting"
        assert len(pushers()) == 1
        clients.append(app._pushers._client)
        if close:
            await app.aclose()
        return ids

    if cancelled:
        first = asyncio.run(push("u1", 1, close=False))
    else:
        loop = asyncio.new_event_loop()
        try:
            first = loop.run_until_complete(push("u1", 1, close=False))
        finally:
            loop.close()
    task = app._pushers._bots["main"].task
    assert task is not None
    assert task.done() is cancelled
    stale = weakref.ref(task)
    del task
    # The pusher that starts on the new loop begins with the oldest event kept.
    second = asyncio.run(push("u2", 2, close=True))
    assert first == [event_id_of("main", "press", "u1")]
    assert second == [event_id_of("main", "press", "u1"), event_id_of("main", "press", "u2")]
    first_client, second_client = clients
    assert first_client is not None
    assert second_client is not None
    assert second_client is not first_client
    # The pusher that asyncio.run cancels closes its loop's client as it ends; that of a loop closed with the pusher
    # still pending never runs again, and its client is left to the garbage collector.
    assert first_client.is_closed is cancelled
    assert second_client.is_closed
    assert app._pushers._client is None
    # Free the pusher of a closed loop now rather than during a later test. asyncio logs that it destroyed a pending
    # task, as it does for every task of a loop closed while the task was pending.
    gc.collect()
    assert stale() is None
    if not cancelled:
        assert "ERROR Task was destroyed but it is pending!" in logged(caplog)


async def test_the_last_pusher_of_a_loop_closes_its_client(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    main, other = callbacks.add(), callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=main.target(), other=other.target()))
    await bridge.report_press("u1")
    await bridge.report_press("u2", bot="other")
    await main.next()
    await other.next()
    client = bridge.app._pushers._client
    assert client is not None
    closed = []
    for task in pushers():
        task.cancel()
        await asyncio.wait([task])
        closed.append(client.is_closed)
    assert closed == [False, True]
    assert bridge.app._pushers._client is None
    # A pusher that starts again begins with the oldest event kept, with a client of its own.
    await bridge.report_press("u3")
    assert [(await main.next()).id for _ in range(2)] == [event_id_of("main", "press", u) for u in ("u1", "u3")]
    assert bridge.app._pushers._client not in (None, client)


async def test_aclose_stops_the_pushers(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add()
    callback.status_of = lambda _: 500
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    await bridge.report_press("u1")
    await callback.next()
    await clock.wait_for_sleep(1)
    client = bridge.app._pushers._client
    assert client is not None
    await bridge.app.aclose()
    assert pushers() == []
    assert clock.pending() == 0
    assert client.is_closed
    clock.advance(3600)
    await bridge.report_press("u2")
    await run_soon()
    callback.none()
    assert pushers() == []
    assert len((await bridge.poll("?wait=0")).events) == 2
    await bridge.app.aclose()


async def test_aclose_during_an_attempt(make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock) -> None:
    callback = callbacks.add()
    callback.hold_first = 1
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    await bridge.report_press("u1")
    await callback.next()
    assert callback.in_flight == 1
    await bridge.app.aclose()
    assert pushers() == []
    assert callback.in_flight == 0
    assert clock.pending() == 0


async def test_lifespan_stops_the_pushers(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    async with bridge.app.lifespan(None):
        (task,) = pushers()
        assert task.get_name() == "muster_bridge pusher main"
        event_id = await bridge.report_press("u1")
        assert (await callback.next()).id == event_id
    assert task.done()
    assert pushers() == []
    with pytest.raises(ClosedError):
        bridge.app.set_push("main", None)


@pytest.mark.parametrize("hosted", [True, False], ids=["host application", "bridge on its own"])
async def test_lifespan_of_a_host_application(make_bridge: MakeBridge, callbacks: Callbacks, hosted: bool) -> None:
    callback = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    app: Any = Starlette(routes=[Mount("/bridge", app=bridge.app)], lifespan=bridge.app.lifespan)
    if not hosted:
        app = bridge.app
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sent: asyncio.Queue[str] = asyncio.Queue()

    async def send(message: dict[str, Any]) -> None:
        sent.put_nowait(message["type"])

    messages.put_nowait({"type": "lifespan.startup"})
    serving = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, messages.get, send))
    assert await sent.get() == "lifespan.startup.complete"
    (task,) = pushers()
    event_id = await bridge.report_press("u1")
    assert (await callback.next()).id == event_id
    messages.put_nowait({"type": "lifespan.shutdown"})
    await serving
    assert await sent.get() == "lifespan.shutdown.complete"
    assert task.done()
    with pytest.raises(ClosedError):
        bridge.app.set_push("main", None)


async def test_push_store_failures(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add()
    store = FailingStore()
    bridge = await make_bridge(store=store)
    first = await bridge.report_press("u1")
    store.fail("events_after", RuntimeError("events failed"))
    bridge.app.set_push("main", callback.target())
    await clock.wait_for_sleep(1)
    store.fail("events_after", None)
    clock.advance(1)
    assert (await callback.next()).id == first
    store.fail("events_after", UnknownCursorError("forgotten"))
    second = await bridge.report_press("u2")
    await clock.wait_for_sleep(1)
    store.fail("events_after", None)
    clock.advance(1)
    assert [(await callback.next()).id for _ in range(2)] == [first, second]
    log = logged(caplog)
    assert "ERROR the store failed: operation=push bot=main error=RuntimeError: events failed" in log
    assert "WARNING the store forgot the push cursor, pushing every kept event: bot=main" in log
    assert "error=UnknownCursorError: forgotten" in log


class TextBodyStore(MemoryStore):
    """A store with a fault: it gives the events' bodies back as text."""

    broken = True

    async def events_after(self, bot: str, cursor: str, limit: int, now: datetime) -> tuple[list[StoredEvent], str]:
        events, next_cursor = await super().events_after(bot, cursor, limit, now)
        if self.broken:
            events = [dataclasses.replace(event, body=cast("Any", event.body.decode())) for event in events]
        return events, next_cursor


async def test_pusher_outlives_a_fault(
    make_bridge: MakeBridge, callbacks: Callbacks, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    callback = callbacks.add()
    store = TextBodyStore()
    bridge = await make_bridge(store=store, bots=two_bots(main=callback.target()))
    event_id = await bridge.report_press("u1")
    await clock.wait_for_sleep(1)
    assert "ERROR the pusher failed: bot=main error=TypeError: " in logged(caplog)
    store.broken = False
    clock.advance(1)
    assert (await callback.next()).id == event_id


async def test_push_secret_never_logged(
    make_bridge: MakeBridge, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    target = PushTarget(url=f"http://nowhere.example.org/cb?s={TEST_SECRET}", secret=TEST_SECRET)
    bridge = await make_bridge(bots=two_bots(main=target))
    await bridge.report_press("u1")
    await clock.wait_for_sleep(1)
    log = logged(caplog)
    assert "error=ConnectError: cannot connect to http://nowhere.example.org/cb?s=[redacted]" in log
    assert TEST_SECRET.removeprefix("whsec_") not in log


async def test_push_targets_of_each_bot(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    main, other = callbacks.add(), callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=main.target(), other=other.target()))
    event_id = await bridge.report_press("u1")
    await bridge.report_press("o1", bot="other")
    assert (await main.next()).id == event_id
    assert (await other.next()).id == event_id_of("other", "press", "o1")
    await run_soon()
    main.none()


def test_webhook_signature_matches_the_reference_vector() -> None:
    # Standard Webhooks publishes this secret, message and expected header for implementations to check against.
    parsed, faults = parse_push_target(PushTarget(url="https://x", secret="whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"))
    assert parsed is not None, faults
    signature = webhook_signature(parsed.key, "msg_p5jXN8AQM9LWM0D4loKWxJek", "1614265330", b'{"test": 2432232314}')
    assert signature == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
