# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import asyncio
import base64
import gc
import json
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import httpx
import pytest
from conftest import (
    ALICE,
    LINK_TOKEN,
    START,
    TOKEN_MAIN,
    TOKEN_OTHER,
    FailingStore,
    FakeClock,
    FakeMessenger,
    Harness,
    MakeBridge,
    all_capabilities,
    answer_body,
    answering,
    event_id_of,
    logged,
    make_config,
    problem_of,
    raising,
)

from muster_bridge import (
    AnswerShownShownAs,
    BridgeApp,
    Forbidden,
    Gone,
    InvalidEventError,
    NotOfferedError,
    Person,
    ReportedEvent,
    UnknownBotError,
)
from muster_bridge.events import BotSignal, Wakeup, stop
from muster_bridge.problems import DETAIL_MESSENGER, DETAIL_WAIT

pytestmark = pytest.mark.anyio

ALICE_JSON = '{"external_id":"u-alice","display_name":"Alice Smith","handle":"alice"}'
GONE = "The bridge does not know this event, or no longer keeps it."


async def test_press_queues_an_event(bridge: Harness) -> None:
    at = datetime(2026, 10, 18, 12, 0, 0, 123000, tzinfo=timezone(timedelta(hours=2)))
    await bridge.app.press("main", message_id="m2", user=ALICE, data="a1.k2.sig", update_id="u1", at=at, chat="chat-1")
    polled = await bridge.poll("?wait=0")
    want = (
        f'{{"id":"{event_id_of("main", "press", "u1")}","type":"press","occurred_at":"2026-10-18T10:00:00.123Z",'
        f'"message_id":"m2","user":{ALICE_JSON},"data":"a1.k2.sig"}}'
    )
    assert polled.text == '{"events":[' + want + '],"next_cursor":' + json.dumps(polled.cursor) + "}"
    assert "chat-1" not in polled.text


async def test_occurred_at_defaults_to_the_clock(bridge: Harness) -> None:
    bridge.clock.advance(90)
    await bridge.app.message_deleted("main", message_id="m1", update_id="u1")
    bridge.clock.advance(0.5)
    await bridge.app.message_deleted("main", message_id="m2", update_id="u2")
    events = (await bridge.poll("?wait=0")).events
    assert [event["occurred_at"] for event in events] == ["2026-10-18T10:01:30Z", "2026-10-18T10:01:30.5Z"]


async def test_random_id_without_update_id(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(random=lambda n: b"\xfb" * n)
    for _ in range(2):
        await bridge.app.press("main", message_id="m1", user=ALICE, data="d")
    events = (await bridge.poll("?wait=0")).events
    want = base64.urlsafe_b64encode(b"\xfb" * 16).decode().rstrip("=")
    assert len(want) == 22
    assert [event["id"] for event in events] == [want]


async def test_update_reported_twice_is_queued_once(bridge: Harness) -> None:
    for _ in range(2):
        await bridge.report_press("u1")
        bridge.clock.advance(1)
    assert len((await bridge.poll("?wait=0")).events) == 1


async def test_same_update_id_of_another_type_or_bot_is_another_event(bridge: Harness) -> None:
    await bridge.app.press("main", message_id="m1", user=ALICE, data="d", update_id="7")
    await bridge.app.thread_message("main", message_id="m1", user=ALICE, text="ack", update_id="7")
    await bridge.app.press("other", message_id="m1", user=ALICE, data="d", update_id="7")
    assert len((await bridge.poll("?wait=0")).events) == 2
    assert len((await bridge.poll("?wait=0", token=TOKEN_OTHER)).events) == 1


async def test_thread_message_event(bridge: Harness) -> None:
    await bridge.app.thread_message("main", message_id="m1", user=ALICE, text="ack", update_id="1")
    await bridge.app.thread_message("main", message_id="m1", user=ALICE, text="ack", thread_ref="", update_id="2")
    await bridge.app.thread_message(
        "main", message_id="m1", user=ALICE, text="é" * 4001, thread_ref="t-m1", update_id="3"
    )
    polled = await bridge.poll("?wait=0")
    first, second, third = polled.events
    assert first["type"] == "thread_message"
    assert '"thread_ref":null' in polled.text
    assert first["thread_ref"] is None
    assert second["thread_ref"] is None
    assert third["thread_ref"] == "t-m1"
    assert third["text"] == "é" * 4000


async def test_message_deleted_event(bridge: Harness) -> None:
    await bridge.app.message_deleted("main", message_id="m1", update_id="d1", at=START)
    want = (
        f'{{"id":"{event_id_of("main", "message_deleted", "d1")}","type":"message_deleted",'
        '"occurred_at":"2026-10-18T10:00:00Z","message_id":"m1"}'
    )
    assert (await bridge.poll("?wait=0")).text.startswith('{"events":[' + want + "]")


@pytest.mark.parametrize(
    ("text", "link"),
    [
        pytest.param(LINK_TOKEN, True, id="token"),
        pytest.param("/start " + LINK_TOKEN, True, id="start"),
        pytest.param("  \n" + LINK_TOKEN + "\t ", True, id="white space"),
        pytest.param("/start   " + LINK_TOKEN, True, id="start with spaces"),
        pytest.param(LINK_TOKEN[:42], False, id="42 characters"),
        pytest.param(LINK_TOKEN + "a", False, id="44 characters"),
        pytest.param(LINK_TOKEN.replace("A", "+", 1), False, id="plus"),
        pytest.param(LINK_TOKEN.replace("A", "/", 1), False, id="slash"),
        pytest.param("/start", False, id="start alone"),
        pytest.param("hello there", False, id="text"),
        pytest.param("my token is " + LINK_TOKEN, False, id="token in text"),
        pytest.param("/start" + LINK_TOKEN, False, id="start without space"),
    ],
)
async def test_direct_message_account_link(bridge: Harness, text: str, link: bool) -> None:
    await bridge.app.direct_message("main", user=ALICE, text=text, update_id="1", chat="dm-alice")
    events = (await bridge.poll("?wait=0")).events
    if not link:
        assert events == []
        return
    (event,) = events
    assert event["type"] == "account_link"
    assert event["token"] == LINK_TOKEN
    assert event["user"]["external_id"] == "u-alice"
    assert event["id"] == event_id_of("main", "account_link", "1")


async def test_reports_need_their_feature(make_bridge: MakeBridge) -> None:
    capabilities = all_capabilities().model_copy(
        update={"buttons": None, "thread_messages": None, "deletion_events": False}
    )
    bridge = await make_bridge(capabilities=capabilities)
    app = bridge.app
    for report, want in (
        (app.press("main", message_id="m1", user=ALICE, data="d"), "press needs capabilities.buttons"),
        (
            app.thread_message("main", message_id="m1", user=ALICE, text="t"),
            "thread_message needs capabilities.thread_messages",
        ),
        (app.message_deleted("main", message_id="m1"), "message_deleted needs capabilities.deletion_events"),
    ):
        with pytest.raises(NotOfferedError, match=want):
            await report
    await app.direct_message("main", user=ALICE, text=LINK_TOKEN)
    assert len((await bridge.poll("?wait=0")).events) == 1


async def test_report_for_an_unknown_bot(bridge: Harness) -> None:
    app = bridge.app
    for report in (
        app.press("nobody", message_id="m1", user=ALICE, data="d"),
        app.thread_message("nobody", message_id="m1", user=ALICE, text="t"),
        app.message_deleted("nobody", message_id="m1"),
        app.direct_message("nobody", user=ALICE, text=LINK_TOKEN),
    ):
        with pytest.raises(UnknownBotError, match="no bot is named 'nobody'"):
            await report
    with pytest.raises(UnknownBotError):
        bridge.app.set_push("nobody", None)


def reports(app: BridgeApp) -> list[tuple[Callable[[], Awaitable[None]], str]]:
    """Return reports of events that do not match api/bridge.yaml, each with the error it raises."""
    unnamed = Person.model_construct(external_id="u")
    return [
        (lambda: app.press("main", message_id="", user=ALICE, data="d"), "message_id is empty"),
        (lambda: app.thread_message("main", message_id="", user=ALICE, text="t"), "message_id is empty"),
        (lambda: app.message_deleted("main", message_id=""), "message_id is empty"),
        (
            lambda: app.press(
                "main", message_id="m1", user=Person.model_construct(external_id="", display_name="A"), data="d"
            ),
            "/user/external_id: String should have at least 1 character",
        ),
        (
            lambda: app.press(
                "main", message_id="m1", user=Person.model_construct(external_id="x" * 513, display_name="A"), data="d"
            ),
            "/user/external_id: String should have at most 512 characters",
        ),
        (
            lambda: app.direct_message("main", user=unnamed, text=LINK_TOKEN),
            "/user/display_name: Field required",
        ),
        (
            lambda: app.press("main", message_id="m1", user=cast("Any", {"external_id": "u"}), data="d"),
            "user is not a muster_bridge.Person",
        ),
        (
            lambda: app.press("main", message_id="m1", user=ALICE, data=cast("Any", 5)),
            "/data: Input should be a valid string",
        ),
        (lambda: app.press("main", message_id="m1", user=ALICE, data=cast("Any", b"d")), "it cannot be encoded"),
        (
            lambda: app.press("main", message_id="m1", user=ALICE, data="d", at=datetime(2026, 10, 18)),
            "at has no time zone",
        ),
        (
            lambda: app.press(
                "main", message_id="m1", user=ALICE, data="d", at=datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
            ),
            "at cannot be given in UTC",
        ),
    ]


async def test_report_of_an_invalid_event(bridge: Harness) -> None:
    for report, want in reports(bridge.app):
        with pytest.raises(InvalidEventError) as raised:
            await report()
        assert str(raised.value) == want
    assert (await bridge.poll("?wait=0")).events == []


async def test_poll_answers_at_once(bridge: Harness) -> None:
    await bridge.report_press("u1")
    assert len((await bridge.poll()).events) == 1
    assert bridge.clock.pending() == 0


async def test_poll_waits_for_an_event(bridge: Harness) -> None:
    polling = asyncio.create_task(bridge.poll())
    await bridge.clock.wait_for_sleep(25)
    assert not polling.done()
    event_id = await bridge.report_press("u1")
    polled = await polling
    assert [event["id"] for event in polled.events] == [event_id]
    assert bridge.clock.pending() == 0


async def test_poll_does_not_block_the_event_loop(bridge: Harness) -> None:
    polling = asyncio.create_task(bridge.poll("?wait=60"))
    await bridge.clock.wait_for_sleep(60)
    for _ in range(3):
        assert (await bridge.call("GET", "/v1/info")).status_code == 200
    await bridge.report_press("u1", bot="other")
    assert (await bridge.poll("?wait=0", token=TOKEN_OTHER)).events != []
    assert not polling.done()
    await bridge.report_press("u2")
    assert len((await polling).events) == 1


async def test_poll_wait_ends_empty(bridge: Harness) -> None:
    first = await bridge.poll("?wait=0")
    polling = asyncio.create_task(bridge.poll(f"?wait=5&cursor={first.cursor}"))
    await bridge.clock.settle(5)
    assert (await polling).text == '{"events":[],"next_cursor":' + json.dumps(first.cursor) + "}"
    assert bridge.clock.pending() == 0


async def test_poll_wait_zero(bridge: Harness) -> None:
    polled = await bridge.poll("?wait=0")
    assert polled.events == []
    assert polled.cursor
    assert bridge.clock.pending() == 0


async def test_poll_unknown_cursor_is_invalid(bridge: Harness) -> None:
    for query in ("?cursor=nope", "?cursor=nope&wait=0"):
        problem = problem_of(await bridge.call("GET", "/v1/events" + query), 400, "invalid")
        assert problem["detail"] == "The cursor is not known to this bridge; poll again without one."


async def test_poll_cursor_advances(bridge: Harness) -> None:
    for i in range(150):
        await bridge.report_press(f"u{i}")
    order: list[str] = []
    cursor = ""
    for want in (100, 50, 0):
        polled = await bridge.poll(f"?wait=0&cursor={cursor}")
        assert len(polled.events) == want
        order += [event["id"] for event in polled.events]
        cursor = polled.cursor
    assert order == [event_id_of("main", "press", f"u{i}") for i in range(150)]


async def test_poll_events_per_bot(bridge: Harness) -> None:
    await bridge.report_press("u1")
    assert (await bridge.poll("?wait=0", token=TOKEN_OTHER)).events == []
    main = await bridge.poll("?wait=0")
    response = await bridge.call("GET", f"/v1/events?wait=0&cursor={main.cursor}", token=TOKEN_OTHER)
    problem_of(response, 400, "invalid")


async def test_poll_keeps_events_24_hours(bridge: Harness) -> None:
    await bridge.report_press("u1")
    bridge.clock.advance(24 * 3600 - 1)
    assert len((await bridge.poll("?wait=0")).events) == 1
    bridge.clock.advance(1)
    assert (await bridge.poll("?wait=0")).events == []


def counting_store() -> tuple[FailingStore, list[str]]:
    """Return a store that lists the name of each of its methods that is called."""
    store = FailingStore()
    calls: list[str] = []

    async def record(method: str) -> None:
        calls.append(method)

    store.before = record
    return store, calls


async def settle() -> None:
    """Let every task that can run do so."""
    for _ in range(20):
        await asyncio.sleep(0)


def test_poll_on_one_event_loop_after_another(clock: FakeClock) -> None:
    store, calls = counting_store()
    app = BridgeApp(make_config(store=store), FakeMessenger(), _clock=clock)

    async def poll(update: str, cursor: str) -> tuple[list[str], str]:
        """Poll with a long wait on the running loop, as Starlette's TestClient does with a loop for each request."""
        query = f"?wait=60&cursor={cursor}" if cursor else "?wait=60"
        headers = {"Authorization": f"Bearer {TOKEN_MAIN}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as client:
            polling = asyncio.create_task(client.get("/v1/events" + query, headers=headers))
            await clock.wait_for_sleep(60)
            reads = calls.count("events_after")
            await settle()
            assert calls.count("events_after") == reads, "the poll reads the store again instead of waiting"
            await app.press("main", message_id="m1", user=ALICE, data="d", update_id=update)
            response = await polling
        assert response.status_code == 200, response.text
        batch = response.json()
        return [event["id"] for event in batch["events"]], batch["next_cursor"]

    first, cursor = asyncio.run(poll("u1", ""))
    second, _ = asyncio.run(poll("u2", cursor))
    assert first == [event_id_of("main", "press", "u1")]
    assert second == [event_id_of("main", "press", "u2")]
    assert clock.pending() == 0


async def test_poll_whose_wait_fails_is_transient(
    make_bridge: MakeBridge, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(_: BotSignal, seen: int) -> None:
        raise RuntimeError(f"the wait after {seen} broke")

    monkeypatch.setattr(BotSignal, "wait_changed", broken)
    store, calls = counting_store()
    bridge = await make_bridge(store=store)
    response = await bridge.call("GET", "/v1/events?wait=60")
    assert problem_of(response, 500, "transient")["detail"] == DETAIL_WAIT
    assert calls.count("events_after") == 1
    assert bridge.clock.pending() == 0
    log = logged(caplog)
    assert (
        "ERROR the wait of a long poll failed: operation=pollEvents bot=main error=RuntimeError: the wait after 0 broke"
        in log
    )
    assert "the store failed" not in log


def collected(task: weakref.ref[asyncio.Task[Any]]) -> bool:
    """Free what no longer holds a task of a closed event loop now, rather than during a later test.

    Return whether the task is gone. asyncio logs that it destroyed a pending task, as it does for every task of a loop
    closed while the task was pending; nothing is raised.
    """
    gc.collect()
    return task() is None


def test_wakeup_wakes_each_waiter_in_its_loop(caplog: pytest.LogCaptureFixture) -> None:
    wakeup = Wakeup()
    other = asyncio.new_event_loop()
    closed = asyncio.new_event_loop()
    try:
        waiting = other.create_task(wakeup.wait())
        other.run_until_complete(asyncio.sleep(0))
        stale = closed.create_task(wakeup.wait())
        closed.run_until_complete(asyncio.sleep(0))
        closed.close()

        async def wake() -> None:
            wakeup.wake_all()

        asyncio.run(wake())
        assert not waiting.done()
        other.run_until_complete(asyncio.wait_for(waiting, 10))
        assert not stale.done()
        ref = weakref.ref(stale)
        del stale
        assert collected(ref)
        assert "ERROR Task was destroyed but it is pending!" in logged(caplog)
    finally:
        other.close()


def test_stop_cancels_a_task_of_another_loop_in_that_loop(caplog: pytest.LogCaptureFixture) -> None:
    other = asyncio.new_event_loop()
    closed = asyncio.new_event_loop()
    try:
        task = other.create_task(asyncio.sleep(3600))
        other.run_until_complete(asyncio.sleep(0))
        stale = closed.create_task(asyncio.sleep(3600))
        closed.run_until_complete(asyncio.sleep(0))
        closed.close()
        asyncio.run(stop(task, stale))
        assert not task.done()
        other.run_until_complete(asyncio.wait([task]))
        assert task.cancelled()
        assert not stale.done()
        ref = weakref.ref(stale)
        del stale
        assert collected(ref)
        assert "ERROR Task was destroyed but it is pending!" in logged(caplog)
    finally:
        other.close()


async def test_poll_client_gone(bridge: Harness) -> None:
    gone = asyncio.Event()
    requested = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "GET",
        "path": "/v1/events",
        "raw_path": b"/v1/events",
        "query_string": b"wait=60",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {TOKEN_MAIN}".encode())],
    }
    serving = asyncio.create_task(bridge.app(scope, cast("Any", receive), cast("Any", send)))
    await bridge.clock.wait_for_sleep(60)
    gone.set()
    await serving
    assert sent == []
    assert bridge.clock.pending() == 0


async def test_answer_event_reaches_the_messenger(bridge: Harness) -> None:
    bridge.messenger.answer = answering(AnswerShownShownAs.ephemeral)
    at = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    await bridge.app.thread_message(
        "main", message_id="m1", thread_ref="t-m1", user=ALICE, text="ack", update_id="u1", at=at, chat="chat-9"
    )
    press_id = await bridge.report_press("u2")
    event_id = event_id_of("main", "thread_message", "u1")
    response = await bridge.call("POST", "/v1/answers", key="k1", body=answer_body(event_id))
    assert response.status_code == 200
    assert response.text == '{"shown_as":"ephemeral","replayed":false}'
    assert (await bridge.call("POST", "/v1/answers", key="k2", body=answer_body(press_id))).status_code == 200
    first, second = bridge.messenger.calls
    assert (first.bot, first.text, first.key) == ("main", "Done", "k1")
    assert first.event == ReportedEvent(
        id=event_id,
        type="thread_message",
        message_id="m1",
        thread_ref="t-m1",
        user=ALICE,
        chat="chat-9",
        occurred_at=at,
    )
    assert first.event is not None
    assert first.event.occurred_at.tzinfo is UTC
    assert second.event == ReportedEvent(
        id=press_id, type="press", message_id="m1", thread_ref=None, user=ALICE, chat=None, occurred_at=START
    )


async def test_answer_event_unknown_id_is_gone(bridge: Harness) -> None:
    response = await bridge.call("POST", "/v1/answers", key="k1", body=answer_body("unknown-event"))
    assert problem_of(response, 404, "gone")["detail"] == GONE
    event_id = await bridge.report_press("u1")
    response = await bridge.call("POST", "/v1/answers", token=TOKEN_OTHER, key="k1", body=answer_body(event_id))
    problem_of(response, 404, "gone")
    assert bridge.messenger.count("answer_event") == 0
    response = await bridge.call("POST", "/v1/answers", key="k1", body=answer_body(event_id))
    assert response.status_code == 200
    assert response.json()["replayed"] is False


async def test_answer_event_for_a_deleted_message_is_gone(bridge: Harness) -> None:
    await bridge.app.message_deleted("main", message_id="m1", update_id="d1")
    event_id = event_id_of("main", "message_deleted", "d1")
    response = await bridge.call("POST", "/v1/answers", key="k1", body=answer_body(event_id))
    assert problem_of(response, 404, "gone")["detail"] == GONE
    assert bridge.messenger.count("answer_event") == 0


async def test_answer_event_of_an_expired_event_is_gone(bridge: Harness) -> None:
    event_id = await bridge.report_press("u1")
    bridge.clock.advance(24 * 3600)
    problem_of(await bridge.call("POST", "/v1/answers", key="k1", body=answer_body(event_id)), 404, "gone")


async def test_answer_event_account_link_token_is_redacted(bridge: Harness, caplog: pytest.LogCaptureFixture) -> None:
    await bridge.app.direct_message("main", user=ALICE, text="/start " + LINK_TOKEN, update_id="1", chat="dm-1")
    event_id = event_id_of("main", "account_link", "1")
    answers = []
    for key, error, status, error_class in (
        ("k1", RuntimeError("cannot answer the link " + LINK_TOKEN), 500, "transient"),
        ("k2", Forbidden("blocked " + LINK_TOKEN), 500, "transient"),
        ("k3", Gone("the link " + LINK_TOKEN + " is gone"), 404, "gone"),
    ):
        bridge.messenger.answer = raising(error)
        response = await bridge.call("POST", "/v1/answers", key=key, body=answer_body(event_id))
        answers.append(problem_of(response, status, error_class))
        assert LINK_TOKEN not in response.text
    assert [answer["detail"] for answer in answers] == [
        DETAIL_MESSENGER,
        DETAIL_MESSENGER,
        "the link [redacted] is gone",
    ]
    event = bridge.messenger.calls[0].event
    assert event is not None
    assert (event.type, event.chat, event.message_id, event.thread_ref) == ("account_link", "dm-1", None, None)
    log = logged(caplog)
    assert "cannot answer the link [redacted]" in log
    assert "class=forbidden" in log
    assert LINK_TOKEN not in log
