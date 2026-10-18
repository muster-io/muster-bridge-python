# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import asyncio
import json
import socket
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
import uvicorn
from conftest import (
    ALICE,
    DM_BODY,
    EDIT_BODY,
    POST_BODY,
    REPLY_BODY,
    TOKEN_MAIN,
    TOKEN_OTHER,
    FailingStore,
    Harness,
    MakeBridge,
    all_capabilities,
    answer_body,
    answering,
    event_id_of,
    logged,
    problem_of,
    raising,
    with_body,
    with_buttons,
)
from starlette.applications import Starlette
from starlette.routing import Mount

from muster_bridge import (
    AnswerShownShownAs,
    Button,
    ButtonStyle,
    CapabilitiesButtons,
    DirectMessageSent,
    KeyClaim,
    KeyState,
    MessageEdited,
    MessagePosted,
    Person,
    StoredAnswer,
    StoredEvent,
    ThreadReplyPosted,
    Transient,
    idempotency_key,
)
from muster_bridge.app import check_buttons
from muster_bridge.problems import DETAIL_MESSENGER, DETAIL_STORE

pytestmark = pytest.mark.anyio

STILL_RUNNING = "A request with this Idempotency-Key is still running."
NO_KEY = "Send an Idempotency-Key header of 1 to 255 visible ASCII characters (no spaces)."
ANSWER_BODY = answer_body(event_id_of("main", "press", "u1"))
"""The body of an answer to the press that report_press("u1") reports."""


async def prepare(bridge: Harness, path: str) -> None:
    """Report the press that ANSWER_BODY answers, for a request to answerEvent."""
    if path == "/v1/answers":
        await bridge.report_press("u1")


def http_scope(method: str, path: str, headers: list[tuple[bytes, bytes]], **changes: Any) -> dict[str, Any]:
    """Return the scope of an HTTP request with the main bot's token, for direct ASGI calls."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {TOKEN_MAIN}".encode()), *headers],
        "server": ("bridge", 80),
        "client": ("127.0.0.1", 50000),
    }
    return {**scope, **changes}


@pytest.mark.parametrize(
    ("method", "path", "key", "body", "status", "called"),
    [
        ("GET", "/v1/info", None, None, 200, None),
        ("POST", "/v1/messages", "k1", POST_BODY, 201, "post_message"),
        ("PATCH", "/v1/messages/m1", None, EDIT_BODY, 200, "edit_message"),
        ("DELETE", "/v1/messages/m1", None, None, 204, "delete_message"),
        ("POST", "/v1/messages/m1/thread-replies", "k2", REPLY_BODY, 201, "reply_in_thread"),
        ("POST", "/v1/channels/alerts/check", None, None, 200, "check_channel"),
        ("GET", "/v1/users?handle=alice", None, None, 200, "find_user"),
        ("POST", "/v1/direct-messages", "k3", DM_BODY, 201, "send_direct_message"),
        ("POST", "/v1/answers", "k4", ANSWER_BODY, 200, "answer_event"),
        ("GET", "/v1/events?wait=0", None, None, 200, None),
    ],
)
async def test_every_operation_is_served(
    bridge: Harness, method: str, path: str, key: str | None, body: str | None, status: int, called: str | None
) -> None:
    await prepare(bridge, path)
    response = await bridge.call(method, path, key=key, body=body)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    if called is not None:
        assert bridge.messenger.count(called) == 1
    if status != 204:
        assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("method", "path", "key", "body", "want"),
    [
        ("POST", "/v1/messages", "k1", POST_BODY, '{"message_id":"m1","truncated":false,"replayed":false}'),
        ("PATCH", "/v1/messages/m1", None, EDIT_BODY, '{"truncated":false}'),
        (
            "POST",
            "/v1/messages/m1/thread-replies",
            "k2",
            REPLY_BODY,
            '{"message_id":"r1","thread_ref":"t-m1","truncated":false,"replayed":false}',
        ),
        ("POST", "/v1/channels/alerts/check", None, None, '{"channel_id":"alerts","name":"Alerts"}'),
        (
            "GET",
            "/v1/users?handle=alice",
            None,
            None,
            '{"items":[{"external_id":"u-alice","display_name":"Alice Smith","handle":"alice"}]}',
        ),
        ("GET", "/v1/users?handle=nobody", None, None, '{"items":[]}'),
        ("POST", "/v1/direct-messages", "k3", DM_BODY, '{"message_id":"d1","replayed":false}'),
        ("POST", "/v1/answers", "k4", ANSWER_BODY, '{"shown_as":"thread","replayed":false}'),
    ],
)
async def test_answers(bridge: Harness, method: str, path: str, key: str | None, body: str | None, want: str) -> None:
    await prepare(bridge, path)
    assert (await bridge.call(method, path, key=key, body=body)).text == want


async def test_the_messenger_gets_the_validated_request(bridge: Harness) -> None:
    await bridge.call("POST", "/v1/messages", key="k1", body=with_buttons(POST_BODY, 2, "a1.k2.sig"))
    await bridge.call("GET", "/v1/users?handle=alice&handle=bob")
    post, find = bridge.messenger.calls
    assert post.bot == "main"
    assert post.payload is not None
    assert getattr(post.payload, "channel_id", None) == "alerts"
    assert [button.data for button in getattr(post.payload, "buttons", [])] == ["a1.k2.sig", "a1.k2.sig"]
    assert find.payload == "alice"


async def test_mounted_under_a_starlette_path(bridge: Harness) -> None:
    host = Starlette(routes=[Mount("/bridge", app=bridge.app)])
    headers = {"Authorization": f"Bearer {TOKEN_MAIN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://host") as client:
        assert (await client.get("/bridge/v1/info", headers=headers)).status_code == 200
        response = await client.patch(
            "/bridge/v1/messages/a%2Fb", headers={**headers, "Content-Type": "application/json"}, content=EDIT_BODY
        )
        assert response.status_code == 200, response.text
        problem_of(await client.get("/bridge/v1/nothing", headers=headers), 404, "invalid")
    assert bridge.messenger.calls[0].message_id == "a/b"


async def test_mounted_under_a_path_the_client_escapes_another_way(bridge: Harness) -> None:
    host = Starlette(routes=[Mount("/brücke~1", app=bridge.app)])
    headers = {"Authorization": f"Bearer {TOKEN_MAIN}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://host") as client:
        for prefix in ("/brücke~1", "/br%c3%bccke%7E1", "/br%C3%BCcke%7e1"):
            assert (await client.get(prefix + "/v1/info", headers=headers)).status_code == 200, prefix
            problem_of(await client.get(prefix + "/v1/nothing", headers=headers), 404, "invalid")
    # Behind a proxy that takes the prefix off, as uvicorn's --root-path has it, the path lacks the root path.
    transport = httpx.ASGITransport(app=bridge.app, root_path="/proxy")
    async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
        assert (await client.get("/v1/info", headers=headers)).status_code == 200


class ReadyServer(uvicorn.Server):
    """A uvicorn server that tells when it has started."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


async def test_uvicorn_serves_the_app(bridge: Harness) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = ReadyServer(uvicorn.Config(bridge.app, lifespan="on", log_level="warning", log_config=None))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            await server.ready.wait()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/v1/info", headers={"Authorization": f"Bearer {TOKEN_MAIN}"}
            )
            assert response.status_code == 200
            assert response.json()["protocol_version"] == "1"
            response = await client.patch(
                f"http://127.0.0.1:{port}/v1/messages/a%2Fb",
                headers={"Authorization": f"Bearer {TOKEN_MAIN}", "Content-Type": "application/json"},
                content=EDIT_BODY,
            )
            assert response.status_code == 200
    finally:
        server.should_exit = True
        await serving
        sock.close()
    assert bridge.messenger.calls[0].message_id == "a/b"


@pytest.mark.parametrize(
    ("path", "authorization"),
    [
        pytest.param("/v1/info", None, id="no header"),
        pytest.param("/v1/info", "Bearer wrong", id="wrong token"),
        pytest.param("/v1/info", f"Basic {TOKEN_MAIN}", id="Basic scheme"),
        pytest.param("/v1/info", "Bearer", id="no token"),
        pytest.param("/v1/info", "Bearer ", id="empty token"),
        pytest.param("/v1/info", TOKEN_MAIN, id="token without scheme"),
        pytest.param("/v1/nothing", "Bearer wrong", id="unknown path"),
        pytest.param("/v1/messages", "Bearer wrong", id="post"),
    ],
)
async def test_unauthorized(bridge: Harness, path: str, authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization is not None else {}
    method = "GET" if path == "/v1/info" else "POST"
    response = await bridge.call(method, path, token=None, key="k1", body=POST_BODY, headers=headers)
    problem = problem_of(response, 401, "forbidden")
    assert problem["detail"] == "The token is missing or wrong."
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert bridge.messenger.calls == []


async def test_scheme_is_case_insensitive(bridge: Harness) -> None:
    response = await bridge.call("GET", "/v1/info", token=None, headers={"Authorization": f"bearer  {TOKEN_MAIN}"})
    assert response.status_code == 200


async def test_token_selects_the_bot(bridge: Harness) -> None:
    for token, bot in ((TOKEN_MAIN, "main"), (TOKEN_OTHER, "other")):
        assert (await bridge.call("POST", "/v1/channels/alerts/check", token=token)).status_code == 200
        assert bridge.messenger.calls[-1].bot == bot


async def test_operations_not_offered_are_404_invalid(make_bridge: MakeBridge) -> None:
    capabilities = all_capabilities().model_copy(update={"direct_messages": None, "delete": None})
    bridge = await make_bridge(user_lookup="none", capabilities=capabilities, event_delivery=["push"])
    for method, path, key, body in (
        ("GET", "/v1/users?handle=alice", None, None),
        ("POST", "/v1/direct-messages", "k1", DM_BODY),
        ("DELETE", "/v1/messages/m1", None, None),
        ("GET", "/v1/events?wait=0", None, None),
    ):
        problem = problem_of(await bridge.call(method, path, key=key, body=body), 404, "invalid")
        assert problem["detail"] == "This bridge does not serve this operation."
    assert bridge.messenger.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/nothing"),
        ("PUT", "/v1/info"),
        ("POST", "/v1/info"),
        ("GET", "/v2/info"),
        ("GET", "/"),
        ("GET", "/v1/info/"),
        ("PATCH", "/v1/messages/"),
        ("PATCH", "/v1/messages/%FF"),
        ("POST", "/v1/messages/m1/thread-replies/x"),
        ("GET", "/v1/events/"),
        ("POST", "/v1/events"),
        ("GET", "/v1/answers"),
    ],
)
async def test_unknown_paths_and_methods_are_404_invalid(bridge: Harness, method: str, path: str) -> None:
    problem = problem_of(await bridge.call(method, path, body=EDIT_BODY), 404, "invalid")
    assert problem["detail"] == "This bridge does not serve this operation."
    assert bridge.messenger.calls == []


async def test_head_is_not_served(bridge: Harness) -> None:
    assert (await bridge.call("HEAD", "/v1/info")).status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "key", "body", "headers", "want"),
    [
        pytest.param(
            "POST",
            "/v1/messages",
            "k1",
            POST_BODY.replace('"kind":"root",', ""),
            None,
            "body /kind: Field required",
            id="missing member",
        ),
        pytest.param(
            "POST",
            "/v1/messages",
            "k2",
            POST_BODY.replace('"notify":true', '"notify":"SECRETX"'),
            None,
            "body /notify: Input should be a valid boolean",
            id="wrong type",
        ),
        pytest.param(
            "POST",
            "/v1/messages",
            "k3",
            POST_BODY.replace('"#d32f2f"', '"SECRETX"'),
            None,
            "body /color: String should match pattern '^#[0-9a-f]{6}$'",
            id="bad colour",
        ),
        pytest.param(
            "POST",
            "/v1/messages",
            "k4",
            with_buttons(POST_BODY, 1, "SECRET X"),
            None,
            "body /buttons/0/data: String should match pattern",
            id="bad button data",
        ),
        pytest.param(
            "POST",
            "/v1/messages",
            "k5",
            POST_BODY.replace("https://muster.example.org/alert-groups/1", "SECRETX"),
            None,
            "body /link/url: Input should be a valid URL",
            id="bad link",
        ),
        pytest.param(
            "POST",
            "/v1/messages",
            "k6",
            POST_BODY,
            {"Content-Type": "text/plain"},
            "body: header Content-Type is not application/json",
            id="text/plain",
        ),
        pytest.param("POST", "/v1/messages", "k7", '{"SECRETX', None, "body: Invalid JSON", id="not JSON"),
        pytest.param("POST", "/v1/messages", "k8", "[]", None, "body: Input should be an object", id="not an object"),
        pytest.param("POST", "/v1/messages", "k9", "", None, "body: value is required but missing", id="no body"),
        pytest.param(
            "POST",
            "/v1/messages/m1/thread-replies",
            "k10",
            REPLY_BODY.replace('"thread_ref":null,', ""),
            None,
            "body /thread_ref: Field required",
            id="missing thread_ref",
        ),
        pytest.param(
            "GET",
            "/v1/users?handle=",
            None,
            None,
            None,
            "parameter handle in query: String should have at least 1 character",
            id="empty handle",
        ),
        pytest.param("GET", "/v1/users", None, None, None, "parameter handle in query: Field required", id="no handle"),
        pytest.param(
            "POST",
            "/v1/channels/" + "S" * 513 + "/check",
            None,
            None,
            None,
            "parameter channel_id in path: String should have at most 512 characters",
            id="long channel id",
        ),
        pytest.param(
            "PATCH",
            "/v1/messages/" + "S" * 513,
            None,
            EDIT_BODY,
            None,
            "parameter message_id in path: String should have at most 512 characters",
            id="long message id",
        ),
        pytest.param(
            "POST",
            "/v1/direct-messages",
            "k11",
            '{"user_external_id":"u","body":"' + "S" * 2001 + '"}',
            None,
            "body /body: String should have at most 2000 characters",
            id="long direct message",
        ),
        pytest.param(
            "GET",
            "/v1/events?wait=61",
            None,
            None,
            None,
            "parameter wait in query: Input should be less than or equal to 60",
            id="wait 61",
        ),
        pytest.param(
            "GET",
            "/v1/events?wait=SECRETX",
            None,
            None,
            None,
            "parameter wait in query: Input should be a valid integer",
            id="wait not a number",
        ),
        pytest.param(
            "GET",
            "/v1/events?cursor=" + "S" * 1025,
            None,
            None,
            None,
            "parameter cursor in query: String should have at most 1024 characters",
            id="long cursor",
        ),
        pytest.param(
            "POST",
            "/v1/answers",
            "k12",
            '{"event_id":"SECRET X","text":"t"}',
            None,
            "body /event_id: String should match pattern '^[!-~]{1,200}$'",
            id="bad event id",
        ),
        pytest.param(
            "POST",
            "/v1/answers",
            "k13",
            '{"event_id":"e1","text":""}',
            None,
            "body /text: String should have at least 1 character",
            id="empty answer",
        ),
        pytest.param("POST", "/v1/answers", None, ANSWER_BODY, None, NO_KEY, id="answer without key"),
        pytest.param("POST", "/v1/messages", None, POST_BODY, None, NO_KEY, id="no key"),
        pytest.param("POST", "/v1/messages", "SECRET X", POST_BODY, None, NO_KEY, id="key with a space"),
        pytest.param("POST", "/v1/messages", "k" * 256, POST_BODY, None, NO_KEY, id="long key"),
    ],
)
async def test_refuses_requests_that_do_not_match_the_models(
    bridge: Harness,
    method: str,
    path: str,
    key: str | None,
    body: str | None,
    headers: dict[str, str] | None,
    want: str,
) -> None:
    response = await bridge.call(method, path, key=key, body=body, headers=headers)
    detail = problem_of(response, 400, "invalid")["detail"]
    assert want in detail
    assert detail.endswith(".")
    assert "SECRET" not in detail
    assert "SSSS" not in detail
    assert bridge.messenger.calls == []


async def test_refuses_a_body_over_1_mib(bridge: Harness) -> None:
    def padded(n: int) -> str:
        return POST_BODY + " " * (n - len(POST_BODY))

    assert (await bridge.call("POST", "/v1/messages", key="k1", body=padded(1 << 20))).status_code == 201
    response = await bridge.call("POST", "/v1/messages", key="k2", body=padded((1 << 20) + 1))
    assert (
        problem_of(response, 400, "invalid")["detail"] == "The request body exceeds 1 MiB, the most this bridge reads."
    )
    assert bridge.messenger.count("post_message") == 1


async def test_stops_reading_a_body_past_1_mib(bridge: Harness) -> None:
    chunk = 1 << 16
    received = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        """Send a body that never ends, in chunks of 64 KiB."""
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b" " * chunk, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {TOKEN_MAIN}".encode()),
            (b"idempotency-key", b"k1"),
            (b"content-type", b"application/json"),
        ],
    }
    async with asyncio.timeout(10):  # a guard against reading for ever, not a wait
        await bridge.app(scope, cast("Any", receive), cast("Any", send))
    assert received == (1 << 20) // chunk + 1
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["detail"] == "The request body exceeds 1 MiB, the most this bridge reads."
    assert bridge.messenger.count("post_message") == 0


@pytest.mark.parametrize(
    ("method", "path", "key", "body", "status"),
    [
        ("POST", "/v1/messages", "k1", POST_BODY, 201),
        ("PATCH", "/v1/messages/m1", None, EDIT_BODY, 200),
        ("POST", "/v1/messages/m1/thread-replies", "k2", REPLY_BODY, 201),
    ],
)
async def test_refuses_a_body_longer_than_max_body_length(
    bridge: Harness, method: str, path: str, key: str | None, body: str, status: int
) -> None:
    response = await bridge.call(method, path, key=key, body=with_body(body, "é" * 500))
    assert response.status_code == status, response.text
    long_key = f"{key}-long" if key is not None else None
    response = await bridge.call(method, path, key=long_key, body=with_body(body, "é" * 501))
    assert problem_of(response, 400, "invalid")["detail"] == (
        "The body is 501 code points long; this bridge takes at most 500 (limits.max_body_length)."
    )
    assert len(bridge.messenger.calls) == 1


async def test_refuses_buttons_without_the_feature(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(capabilities=all_capabilities().model_copy(update={"buttons": None}))
    response = await bridge.call("POST", "/v1/messages", key="k1", body=with_buttons(POST_BODY, 1, "d"))
    assert (
        problem_of(response, 400, "invalid")["detail"] == "This bridge does not offer buttons (capabilities.buttons)."
    )
    assert bridge.messenger.calls == []


async def test_refuses_too_many_buttons(bridge: Harness) -> None:
    assert (await bridge.call("PATCH", "/v1/messages/m1", body=with_buttons(EDIT_BODY, 3, "d"))).status_code == 200
    response = await bridge.call("PATCH", "/v1/messages/m1", body=with_buttons(EDIT_BODY, 4, "d"))
    assert problem_of(response, 400, "invalid")["detail"] == (
        "The message has 4 buttons; this bridge takes at most 3 (capabilities.buttons.max_per_message)."
    )
    assert bridge.messenger.count("edit_message") == 1


def test_check_buttons_data_max_bytes() -> None:
    # The specification caps button data at 64 bytes by pattern and data_max_bytes at no less, so only the function
    # can be given longer data.
    capabilities = CapabilitiesButtons(max_per_message=5, max_per_row=5, data_max_bytes=64)
    fits = Button(label="a", style=ButtonStyle.primary, data="d" * 64)
    assert check_buttons([fits], capabilities) is None
    assert check_buttons([], None) is None
    long = Button.model_construct(label="b", style=ButtonStyle.primary, data="d" * 65)
    assert check_buttons([fits, long], capabilities) == (
        "Button 2 carries 65 bytes of data; this bridge takes at most 64 (capabilities.buttons.data_max_bytes)."
    )


@pytest.mark.parametrize(
    ("path", "message_id"), [("a%2Fb", "a/b"), ("%2E%2E", ".."), ("x%20y", "x y"), ("%C3%A9", "é")]
)
async def test_path_values_are_decoded(bridge: Harness, path: str, message_id: str) -> None:
    response = await bridge.call("PATCH", f"/v1/messages/{path}", body=EDIT_BODY)
    assert response.status_code == 200, response.text
    assert bridge.messenger.calls[-1].message_id == message_id


async def test_routes_without_a_raw_path(bridge: Harness) -> None:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": EDIT_BODY.encode(), "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = http_scope("PATCH", "/v1/messages/a b", [(b"content-type", b"application/json")])
    del scope["raw_path"]
    await bridge.app(scope, cast("Any", receive), cast("Any", send))
    assert sent[0]["status"] == 200
    assert bridge.messenger.calls[0].message_id == "a b"


async def test_a_path_without_a_leading_slash_is_404(bridge: Harness) -> None:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await bridge.app(http_scope("OPTIONS", "*", []), cast("Any", receive), cast("Any", send))
    assert sent[0]["status"] == 404


async def test_invalid_messenger_answer(bridge: Harness, caplog: pytest.LogCaptureFixture) -> None:
    bridge.messenger.post = answering(MessagePosted.model_construct(message_id="", truncated=False, replayed=False))
    response = await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    assert problem_of(response, 500, "transient")["detail"] == DETAIL_MESSENGER
    assert (
        "ERROR the messenger's answer does not match api/bridge.yaml: operation=postMessage bot=main "
        "problem=/message_id: String should have at least 1 character"
    ) in logged(caplog)
    bridge.messenger.post = None
    response = await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert bridge.messenger.count("post_message") == 2


@pytest.mark.parametrize(
    ("method", "path", "key", "body", "override", "answer", "problem"),
    [
        pytest.param(
            "POST",
            "/v1/messages/m1/thread-replies",
            "k1",
            REPLY_BODY,
            "reply",
            ThreadReplyPosted.model_construct(message_id="r1", thread_ref="t" * 513, truncated=False, replayed=False),
            "/thread_ref: String should have at most 512 characters",
            id="long thread_ref",
        ),
        pytest.param(
            "GET",
            "/v1/users?handle=x",
            None,
            None,
            "find",
            Person.model_construct(external_id="u"),
            "/items/0/display_name: Field required",
            id="person without a name",
        ),
        pytest.param(
            "GET", "/v1/users?handle=x", None, None, "find", "alice", "find_user returned neither", id="not a person"
        ),
        pytest.param(
            "POST", "/v1/channels/alerts/check", None, None, "check", 42, "check_channel returned no str", id="no name"
        ),
        pytest.param("POST", "/v1/messages", "k2", POST_BODY, "post", None, "it is not a MessagePosted", id="None"),
        pytest.param(
            "PATCH",
            "/v1/messages/m1",
            None,
            EDIT_BODY,
            "edit",
            MessageEdited.model_construct(truncated="yes"),
            "/truncated: Input should be a valid boolean",
            id="wrong type",
        ),
        pytest.param(
            "POST",
            "/v1/direct-messages",
            "k3",
            DM_BODY,
            "dm",
            DirectMessageSent.model_construct(message_id=object(), replayed=False),
            "it cannot be encoded",
            id="not encodable",
        ),
        pytest.param(
            "POST",
            "/v1/answers",
            "k4",
            ANSWER_BODY,
            "answer",
            "banner",
            "/shown_as: Input should be 'ephemeral', 'popup', 'thread' or 'direct'",
            id="unknown place",
        ),
    ],
)
async def test_invalid_answers_of_each_operation(
    bridge: Harness,
    caplog: pytest.LogCaptureFixture,
    method: str,
    path: str,
    key: str | None,
    body: str | None,
    override: str,
    answer: object,
    problem: str,
) -> None:
    await prepare(bridge, path)
    setattr(bridge.messenger, override, answering(answer))
    response = await bridge.call(method, path, key=key, body=body)
    assert problem_of(response, 500, "transient")["detail"] == DETAIL_MESSENGER
    assert f"problem={problem}" in logged(caplog)


async def test_channel_name_is_cut_and_defaults_to_the_id(bridge: Harness) -> None:
    bridge.messenger.check = answering("é" * 250)
    assert (await bridge.call("POST", "/v1/channels/alerts/check")).json()["name"] == "é" * 200
    bridge.messenger.check = answering("")
    assert (await bridge.call("POST", "/v1/channels/ops/check")).json() == {"channel_id": "ops", "name": "ops"}


async def test_idempotency_key_reaches_the_messenger(bridge: Harness) -> None:
    await bridge.call("POST", "/v1/messages", key="key-1", body=POST_BODY)
    await bridge.call("PATCH", "/v1/messages/m1", body=EDIT_BODY)
    assert [call.key for call in bridge.messenger.calls] == ["key-1", None]
    assert idempotency_key() is None


@pytest.mark.parametrize(
    ("method", "path", "body", "called"),
    [
        ("POST", "/v1/messages", POST_BODY, "post_message"),
        ("POST", "/v1/messages/m1/thread-replies", REPLY_BODY, "reply_in_thread"),
        ("POST", "/v1/direct-messages", DM_BODY, "send_direct_message"),
        ("POST", "/v1/answers", ANSWER_BODY, "answer_event"),
    ],
)
async def test_repeated_key_replays_the_first_answer(
    bridge: Harness, method: str, path: str, body: str, called: str
) -> None:
    await prepare(bridge, path)
    first = await bridge.call(method, path, key="same", body=body)
    assert first.status_code == (200 if path == "/v1/answers" else 201)
    want = first.json()
    assert want["replayed"] is False
    for repeated in (body, '{"other":"body"}', "not even JSON"):
        again = await bridge.call(method, path, key="same", body=repeated)
        assert again.status_code == first.status_code
        assert again.json() == {**want, "replayed": True}
        assert again.headers["cache-control"] == "no-store"
    assert bridge.messenger.count(called) == 1


async def test_key_under_another_token_is_a_new_request(bridge: Harness) -> None:
    await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    response = await bridge.call("POST", "/v1/messages", token=TOKEN_OTHER, key="k1", body=POST_BODY)
    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert [call.bot for call in bridge.messenger.calls] == ["main", "other"]


async def test_key_still_running_is_transient(bridge: Harness) -> None:
    bridge.messenger.block = asyncio.Event()
    first = asyncio.create_task(bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY))
    await bridge.messenger.entered.wait()
    response = await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    assert problem_of(response, 500, "transient")["detail"] == STILL_RUNNING
    bridge.messenger.block.set()
    assert (await first).status_code == 201
    response = await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    assert response.json()["replayed"] is True


async def test_failed_request_stores_nothing(bridge: Harness) -> None:
    bridge.messenger.post = raising(Transient("down"))
    assert (await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)).status_code == 500
    bridge.messenger.post = None
    response = await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert (await bridge.call("POST", "/v1/messages", key="k2", body="{}")).status_code == 400
    response = await bridge.call("POST", "/v1/messages", key="k2", body=POST_BODY)
    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert bridge.messenger.count("post_message") == 3


async def test_keys_live_24_hours(bridge: Harness) -> None:
    await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    bridge.clock.advance(24 * 3600 - 0.000001)
    assert (await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)).json()["replayed"] is True
    bridge.clock.advance(0.000001)
    assert (await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)).json()["replayed"] is False
    assert bridge.messenger.count("post_message") == 2


async def test_stale_request_keeps_the_retrys_claim(bridge: Harness) -> None:
    # A request whose messenger call outlives the claim of its key lets a retry claim the key anew; when the first
    # request fails at last, its release must not free the retry's claim.
    gates = [asyncio.Event(), asyncio.Event()]
    entered: asyncio.Queue[int] = asyncio.Queue()
    calls = 0

    async def post(*_: object) -> MessagePosted:
        nonlocal calls
        n, calls = calls, calls + 1
        entered.put_nowait(n)
        if n < len(gates):
            await gates[n].wait()
        if n == 0:
            raise RuntimeError("the messenger never answered")
        return MessagePosted(message_id=f"m-call-{n}")

    async def send() -> httpx.Response:
        return await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)

    bridge.messenger.post = post
    first = asyncio.create_task(send())
    assert await entered.get() == 0
    bridge.clock.advance(120)
    retry = asyncio.create_task(send())
    assert await entered.get() == 1
    gates[0].set()
    assert (await first).status_code == 500
    assert problem_of(await send(), 500, "transient")["detail"] == STILL_RUNNING
    gates[1].set()
    assert (await retry).status_code == 201
    again = (await send()).json()
    assert again["replayed"] is True
    assert again["message_id"] == "m-call-1"
    assert calls == 2


async def test_exception_releases_the_key(bridge: Harness) -> None:
    bridge.messenger.post = raising(RuntimeError("boom"))
    problem_of(await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY), 500, "transient")
    bridge.messenger.post = None
    assert (await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)).status_code == 201


async def test_store_failures(make_bridge: MakeBridge, caplog: pytest.LogCaptureFixture) -> None:
    store = FailingStore()
    bridge = await make_bridge(store=store)

    async def post(key: str, body: str = POST_BODY) -> httpx.Response:
        return await bridge.call("POST", "/v1/messages", key=key, body=body)

    store.fail("claim_key", RuntimeError(f"claim failed for {TOKEN_MAIN}"))
    assert problem_of(await post("k1"), 500, "transient")["detail"] == DETAIL_STORE
    store.fail("claim_key", None)

    for claim in (
        cast("KeyClaim", {"state": KeyState.CLAIMED, "claim_id": "c1"}),
        cast("KeyClaim", KeyState.CLAIMED),
        KeyClaim(state=cast("KeyState", "weird")),
        KeyClaim(state=KeyState.ANSWERED),
        KeyClaim(state=KeyState.ANSWERED, answer=StoredAnswer(201, b"[1]")),
        KeyClaim(state=KeyState.ANSWERED, answer=StoredAnswer(201, b"not json")),
        KeyClaim(state=KeyState.ANSWERED, answer=StoredAnswer(200, b'{"message_id":"m1","replayed":false}')),
        KeyClaim(state=KeyState.ANSWERED, answer=StoredAnswer(201, b'{"message_id":"","replayed":false}')),
    ):
        store.claim_as(claim)
        assert problem_of(await post("k2"), 500, "transient")["detail"] == DETAIL_STORE
    store.claim_as(None)

    store.fail("save_key", RuntimeError("save failed"))
    assert (await post("k4")).status_code == 201
    store.fail("save_key", None)
    response = await post("k4")
    assert response.json()["replayed"] is False
    assert bridge.messenger.count("post_message") == 2

    store.fail("release_key", RuntimeError("release failed"))
    assert (await post("k5", "{}")).status_code == 400
    store.fail("release_key", None)

    await bridge.report_press("u1")
    store.fail("find_event", RuntimeError("find failed"))
    response = await bridge.call("POST", "/v1/answers", key="k6", body=ANSWER_BODY)
    assert problem_of(response, 500, "transient")["detail"] == DETAIL_STORE
    store.fail("find_event", None)

    store.fail("events_after", RuntimeError("events failed"))
    assert problem_of(await bridge.call("GET", "/v1/events"), 500, "transient")["detail"] == DETAIL_STORE
    store.fail("events_after", None)

    store.fail("append_event", RuntimeError("append failed"))
    with pytest.raises(RuntimeError, match="append failed"):
        await bridge.app.press("main", message_id="m1", user=ALICE, data="d")
    store.fail("append_event", None)
    assert len((await bridge.poll("?wait=0")).events) == 1
    assert bridge.messenger.count("answer_event") == 0

    log = logged(caplog)
    for want in (
        "ERROR the store failed: operation=postMessage bot=main error=RuntimeError: claim failed for [redacted]",
        "ERROR the store failed: operation=postMessage bot=main error=claim_key returned a dict, not a KeyClaim",
        "claim_key returned a KeyState, not a KeyClaim",
        "claim_key returned the unknown state 'weird'",
        "cannot be sent again: the store returned no answer",
        "cannot be sent again: it is not the JSON of the operation's answer",
        "cannot be sent again: api/bridge.yaml declares no 200 answer of postMessage",
        "cannot be sent again: /message_id: String should have at least 1 character",
        "ERROR cannot store the answer of an Idempotency-Key: operation=postMessage bot=main error=RuntimeError: save",
        "ERROR cannot release an Idempotency-Key: operation=postMessage bot=main error=RuntimeError: release failed",
        "ERROR the store failed: operation=answerEvent bot=main error=RuntimeError: find failed",
        "ERROR the store failed: operation=pollEvents bot=main error=RuntimeError: events failed",
    ):
        assert want in log
    assert log.count("it is not the JSON of the operation's answer") == 2
    assert TOKEN_MAIN not in log


@pytest.mark.parametrize("body", [b"not json", b'{"id":"e1","type":"press"}'])
async def test_stored_event_that_is_not_json(bridge: Harness, caplog: pytest.LogCaptureFixture, body: bytes) -> None:
    until = bridge.clock.now() + timedelta(hours=1)
    stored = StoredEvent(id="e1", type="press", body=body, chat=None, until=until)
    assert await bridge.store.append_event("main", stored, bridge.clock.now())
    response = await bridge.call("POST", "/v1/answers", key="k1", body=answer_body("e1"))
    assert problem_of(response, 500, "transient")["detail"] == DETAIL_STORE
    assert bridge.messenger.count("answer_event") == 0
    assert (
        "ERROR the store failed: operation=answerEvent bot=main error=a stored event is not the JSON of an event"
    ) in logged(caplog)


async def test_answer_event_is_idempotent_per_bot(bridge: Harness) -> None:
    bridge.messenger.answer = answering(AnswerShownShownAs.popup)
    first = await bridge.call("POST", "/v1/answers", key="k1", body=ANSWER_BODY)
    assert problem_of(first, 404, "gone")["detail"]
    await bridge.report_press("u1")
    for replayed in (False, True):
        response = await bridge.call("POST", "/v1/answers", key="k1", body=ANSWER_BODY)
        assert response.json() == {"shown_as": "popup", "replayed": replayed}
    assert bridge.messenger.count("answer_event") == 1
    assert bridge.messenger.calls[0].key == "k1"


async def test_request_without_body(bridge: Harness) -> None:
    assert (await bridge.call("POST", "/v1/channels/alerts/check")).status_code == 200


async def test_unreadable_body(bridge: Harness) -> None:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = http_scope("PATCH", "/v1/messages/m1", [(b"content-type", b"application/json")])
    await bridge.app(scope, cast("Any", receive), cast("Any", send))
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["detail"] == "Reading the request body failed."
    assert bridge.messenger.calls == []


async def test_websocket_is_refused(bridge: Harness) -> None:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await bridge.app({"type": "websocket", "path": "/v1/info"}, cast("Any", receive), cast("Any", send))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    await bridge.app({"type": "other"}, cast("Any", receive), cast("Any", send))
    assert len(sent) == 1


async def test_lifespan_messages_are_answered(bridge: Harness) -> None:
    messages = asyncio.Queue[dict[str, Any]]()
    for message in ({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}):
        messages.put_nowait(message)
    sent: list[str] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])

    await bridge.app({"type": "lifespan", "asgi": {"version": "3.0"}}, cast("Any", messages.get), cast("Any", send))
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
