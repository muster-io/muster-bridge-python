# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import json
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import pytest
from conftest import (
    ALICE,
    DM_BODY,
    EDIT_BODY,
    LINK_TOKEN,
    POST_BODY,
    REPLY_BODY,
    TOKEN_MAIN,
    Callbacks,
    FailingStore,
    FakeMessenger,
    Harness,
    MakeBridge,
    all_capabilities,
    answer_body,
    event_id_of,
    raising,
    two_bots,
    with_body,
)
from openapi_core import Config, OpenAPI
from openapi_core.datatypes import RequestParameters
from openapi_core.testing import MockRequest, MockResponse
from openapi_core.validation.request.exceptions import MissingRequiredParameter
from openapi_core.validation.response.exceptions import InvalidData

from muster_bridge import (
    ButtonStyle,
    CapabilitiesDelete,
    CapabilitiesPrivateAnswer,
    Forbidden,
    Gone,
    Invalid,
    KeyClaim,
    KeyState,
    Limits,
    Person,
    RateLimited,
)
from muster_bridge.app import OPERATIONS
from muster_bridge.problems import CLASS_STATUS, DECLARED

pytestmark = pytest.mark.anyio

SPEC_PATH = Path(__file__).parents[1] / "api" / "bridge.yaml"
# Problems are application/problem+json, which openapi-core reads as JSON only when told to.
SPEC = OpenAPI.from_file_path(
    str(SPEC_PATH), config=Config(extra_media_type_deserializers={"application/problem+json": json.loads})
)
DOCUMENT: dict[str, Any] = SPEC.spec.read_value()
STATUS_CLASS = {"400": "invalid", "403": "forbidden", "404": "gone", "429": "rate_limited", "500": "transient"}
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
TEMPLATES = {op.op_id: op.template for op in OPERATIONS}
IDEMPOTENT = {op.op_id for op in OPERATIONS if op.idempotent}

POST = "/v1/messages"
EDIT = "/v1/messages/m1"
REPLY = "/v1/messages/m1/thread-replies"
CHECK = "/v1/channels/alerts/check"
USERS = "/v1/users"
DM = "/v1/direct-messages"
ANSWERS = "/v1/answers"
EVENTS = "/v1/events"
ANSWER = answer_body(event_id_of("main", "press", "u1"))
"""The body of an answer to the press reported before every answerEvent exchange."""


def spec_operations() -> dict[str, tuple[str, str, dict[str, Any]]]:
    """Return the operations of api/bridge.yaml by operationId: method, path and the operation."""
    found = {}
    for path, item in DOCUMENT["paths"].items():
        for method in METHODS:
            if method in item:
                operation = item[method]
                found[operation["operationId"]] = (method.upper(), path, operation)
    return found


def forbidden() -> BaseException:
    return Forbidden("The bot has not joined this channel.")


def limited() -> BaseException:
    return RateLimited("Slow down.", retry_after=1.5, scope="bot")


def gone() -> BaseException:
    return Gone("That message is gone.")


def too_old() -> BaseException:
    return Gone("The message can no longer be edited.", edit_window_closed=True)


def invalid() -> BaseException:
    return Invalid("This handle cannot be searched for.")


def broken() -> BaseException:
    return RuntimeError("the messenger's socket broke")


Setup = Callable[[Harness, FailingStore], Awaitable[None]]


def replay(path: str, body: str) -> Setup:
    """Return a setup that sends the request once with the Idempotency-Key k1, so that the exchange replays it."""

    async def setup(bridge: Harness, _: FailingStore) -> None:
        response = await bridge.call("POST", path, key="k1", body=body)
        assert response.is_success, response.text

    return setup


async def running_key(_: Harness, store: FailingStore) -> None:
    store.claim_as(KeyClaim(state=KeyState.RUNNING))


async def failing_events(_: Harness, store: FailingStore) -> None:
    store.fail("events_after", RuntimeError("down"))


async def every_event(bridge: Harness, _: FailingStore) -> None:
    app = bridge.app
    bob = Person(external_id="u-bob", display_name="Bob")
    await app.press("main", update_id="1", message_id="m1", user=ALICE, data="d")
    await app.thread_message("main", update_id="2", message_id="m1", user=ALICE, text="ack")
    await app.thread_message("main", update_id="3", message_id="m1", thread_ref="t-m1", user=bob, text="ack")
    await app.direct_message("main", update_id="4", user=ALICE, text=LINK_TOKEN)
    await app.message_deleted("main", update_id="5", message_id="m1")


@dataclass(frozen=True)
class Exchange:
    """One request of an operation and the status its answer must have."""

    op: str
    name: str
    method: str
    path: str
    status: int
    body: str | None = None
    token: str = TOKEN_MAIN
    error: Callable[[], BaseException] | None = None
    setup: Setup | None = None
    invalid: bool = False
    """The request does not match the specification, so only the answer is validated."""


EXCHANGES = [
    Exchange("getInfo", "200", "GET", "/v1/info", 200),
    Exchange("getInfo", "401", "GET", "/v1/info", 401, token="wrong"),
    Exchange("postMessage", "201", "POST", POST, 201, POST_BODY),
    Exchange("postMessage", "201 replayed", "POST", POST, 201, POST_BODY, setup=replay(POST, POST_BODY)),
    Exchange("postMessage", "400 spec", "POST", POST, 400, "{}", invalid=True),
    Exchange("postMessage", "400 limit", "POST", POST, 400, with_body(POST_BODY, "x" * 501)),
    Exchange("postMessage", "401", "POST", POST, 401, POST_BODY, token="wrong"),
    Exchange("postMessage", "403", "POST", POST, 403, POST_BODY, error=forbidden),
    Exchange("postMessage", "403 from gone", "POST", POST, 403, POST_BODY, error=gone),
    Exchange("postMessage", "429", "POST", POST, 429, POST_BODY, error=limited),
    Exchange("postMessage", "500", "POST", POST, 500, POST_BODY, error=broken),
    Exchange("postMessage", "500 running", "POST", POST, 500, POST_BODY, setup=running_key),
    Exchange("editMessage", "200", "PATCH", EDIT, 200, EDIT_BODY),
    Exchange("editMessage", "400", "PATCH", EDIT, 400, '{"status":"x"}', invalid=True),
    Exchange("editMessage", "401", "PATCH", EDIT, 401, EDIT_BODY, token="wrong"),
    Exchange("editMessage", "403", "PATCH", EDIT, 403, EDIT_BODY, error=forbidden),
    Exchange("editMessage", "404", "PATCH", EDIT, 404, EDIT_BODY, error=gone),
    Exchange("editMessage", "404 edit window", "PATCH", EDIT, 404, EDIT_BODY, error=too_old),
    Exchange("editMessage", "429", "PATCH", EDIT, 429, EDIT_BODY, error=limited),
    Exchange("editMessage", "500", "PATCH", EDIT, 500, EDIT_BODY, error=broken),
    Exchange("deleteMessage", "204", "DELETE", EDIT, 204),
    Exchange("deleteMessage", "204 gone", "DELETE", EDIT, 204, error=gone),
    Exchange("deleteMessage", "401", "DELETE", EDIT, 401, token="wrong"),
    Exchange("deleteMessage", "403", "DELETE", EDIT, 403, error=forbidden),
    Exchange("deleteMessage", "429", "DELETE", EDIT, 429, error=limited),
    Exchange("deleteMessage", "500", "DELETE", EDIT, 500, error=broken),
    Exchange("replyInThread", "201", "POST", REPLY, 201, REPLY_BODY),
    Exchange("replyInThread", "400", "POST", REPLY, 400, "{}", invalid=True),
    Exchange("replyInThread", "401", "POST", REPLY, 401, REPLY_BODY, token="wrong"),
    Exchange("replyInThread", "403", "POST", REPLY, 403, REPLY_BODY, error=forbidden),
    Exchange("replyInThread", "404", "POST", REPLY, 404, REPLY_BODY, error=gone),
    Exchange("replyInThread", "429", "POST", REPLY, 429, REPLY_BODY, error=limited),
    Exchange("replyInThread", "500", "POST", REPLY, 500, REPLY_BODY, error=broken),
    Exchange("checkChannel", "200", "POST", CHECK, 200),
    Exchange("checkChannel", "401", "POST", CHECK, 401, token="wrong"),
    Exchange("checkChannel", "403", "POST", CHECK, 403, error=forbidden),
    Exchange("checkChannel", "403 from gone", "POST", CHECK, 403, error=gone),
    Exchange("checkChannel", "403 from invalid", "POST", CHECK, 403, error=invalid),
    Exchange("checkChannel", "429", "POST", CHECK, 429, error=limited),
    Exchange("checkChannel", "500", "POST", CHECK, 500, error=broken),
    Exchange("findUsers", "200 found", "GET", USERS + "?handle=alice", 200),
    Exchange("findUsers", "200 nobody", "GET", USERS + "?handle=nobody", 200),
    Exchange("findUsers", "400 spec", "GET", USERS + "?handle=", 400, invalid=True),
    Exchange("findUsers", "400 finder", "GET", USERS + "?handle=a%20b", 400, error=invalid),
    Exchange("findUsers", "401", "GET", USERS + "?handle=alice", 401, token="wrong"),
    Exchange("findUsers", "429", "GET", USERS + "?handle=alice", 429, error=limited),
    Exchange("findUsers", "500", "GET", USERS + "?handle=alice", 500, error=broken),
    Exchange("sendDirectMessage", "201", "POST", DM, 201, DM_BODY),
    Exchange("sendDirectMessage", "400", "POST", DM, 400, '{"body":""}', invalid=True),
    Exchange("sendDirectMessage", "401", "POST", DM, 401, DM_BODY, token="wrong"),
    Exchange("sendDirectMessage", "403", "POST", DM, 403, DM_BODY, error=forbidden),
    Exchange("sendDirectMessage", "429", "POST", DM, 429, DM_BODY, error=limited),
    Exchange("sendDirectMessage", "500", "POST", DM, 500, DM_BODY, error=broken),
    Exchange("answerEvent", "200", "POST", ANSWERS, 200, ANSWER),
    Exchange("answerEvent", "400", "POST", ANSWERS, 400, '{"text":""}', invalid=True),
    Exchange("answerEvent", "401", "POST", ANSWERS, 401, ANSWER, token="wrong"),
    Exchange("answerEvent", "404", "POST", ANSWERS, 404, answer_body("nope")),
    Exchange("answerEvent", "429", "POST", ANSWERS, 429, ANSWER, error=limited),
    Exchange("answerEvent", "500", "POST", ANSWERS, 500, ANSWER, error=broken),
    Exchange("pollEvents", "200 every event", "GET", EVENTS + "?wait=0", 200, setup=every_event),
    Exchange("pollEvents", "200 empty", "GET", EVENTS + "?wait=0", 200),
    Exchange("pollEvents", "400 cursor", "GET", EVENTS + "?cursor=x", 400),
    Exchange("pollEvents", "400 wait", "GET", EVENTS + "?wait=61", 400, invalid=True),
    Exchange("pollEvents", "401", "GET", EVENTS, 401, token="wrong"),
    Exchange("pollEvents", "500", "GET", EVENTS + "?wait=0", 500, setup=failing_events),
]


def path_params(template: str, path: str) -> dict[str, str]:
    """Return the decoded path parameters of an escaped path that the template of api/bridge.yaml matches."""
    params = {}
    for name, value in zip(template.split("/"), path.split("/"), strict=True):
        if name.startswith("{"):
            params[name.strip("{}")] = unquote(value)
    return params


def check_exchange(op: str, response: httpx.Response, *, valid_request: bool) -> None:
    """Validate the request, when it is meant to be valid, and its answer against api/bridge.yaml."""
    url = urlsplit(str(response.request.url))
    template = TEMPLATES[op]
    request = MockRequest(
        f"{url.scheme}://{url.netloc}",
        response.request.method.lower(),
        url.path,
        path_pattern=template,
        view_args=path_params(template, url.path),
        args=dict(parse_qsl(url.query, keep_blank_values=True)),
        headers=dict(response.request.headers),
        data=response.request.content or None,
        content_type=response.request.headers.get("content-type", "application/json"),
    )
    if valid_request:
        SPEC.validate_request(request)
    answer = MockResponse(
        response.content,
        status_code=response.status_code,
        headers=dict(response.headers),
        content_type=response.headers.get("content-type", "").partition(";")[0] or "application/json",
    )
    SPEC.validate_response(request, answer)


def test_the_copy_is_the_3_1_document_under_mit() -> None:
    assert str(DOCUMENT["openapi"]).startswith("3.1")
    assert DOCUMENT["info"]["license"]["identifier"] == "MIT"


def test_every_operation_of_the_specification_is_routed() -> None:
    spec = sorted(f"{method} {path} {op_id}" for op_id, (method, path, _) in spec_operations().items())
    table = sorted(f"{op.method} {op.template} {op.op_id}" for op in OPERATIONS)
    assert spec == table
    assert set(DECLARED) == {op.op_id for op in OPERATIONS}


def test_declared_classes_match_the_specification() -> None:
    for op_id, (_, _, operation) in spec_operations().items():
        statuses = set(operation["responses"])
        unknown = {s for s in statuses if s not in STATUS_CLASS and s != "401" and not s.startswith("2")}
        assert not unknown, f"{op_id} declares {unknown}, which no class has"
        classes = {STATUS_CLASS[s] for s in statuses if s in STATUS_CLASS}
        assert classes == DECLARED[op_id], op_id
        for error_class, status in CLASS_STATUS.items():
            if error_class in DECLARED[op_id]:
                assert str(status) in statuses, f"{op_id} answers {error_class} with {status}, undeclared"


def test_every_operation_has_an_exchange() -> None:
    assert {exchange.op for exchange in EXCHANGES} == {op.op_id for op in OPERATIONS}


@pytest.mark.parametrize("exchange", EXCHANGES, ids=[f"{e.op} {e.name}" for e in EXCHANGES])
async def test_requests_and_answers_match_the_specification(make_bridge: MakeBridge, exchange: Exchange) -> None:
    store = FailingStore()
    messenger = FakeMessenger()
    bridge = await make_bridge(messenger=messenger, store=store)
    if exchange.op == "answerEvent":
        await bridge.report_press("u1")
    if exchange.setup is not None:
        await exchange.setup(bridge, store)
    if exchange.error is not None:
        failure = raising(exchange.error())
        for name in ("post", "edit", "reply", "check", "answer", "find", "dm", "delete"):
            setattr(messenger, name, failure)
    key = "k1" if exchange.op in IDEMPOTENT else None
    response = await bridge.call(exchange.method, exchange.path, token=exchange.token, key=key, body=exchange.body)
    assert response.status_code == exchange.status, response.text
    check_exchange(exchange.op, response, valid_request=not exchange.invalid and exchange.token == TOKEN_MAIN)


async def test_info_with_every_member_matches_the_specification(make_bridge: MakeBridge) -> None:
    capabilities = all_capabilities()
    assert capabilities.buttons is not None
    capabilities.buttons.styles = list(ButtonStyle)
    capabilities.private_answers = [CapabilitiesPrivateAnswer.ephemeral, CapabilitiesPrivateAnswer.popup]
    capabilities.delete = CapabilitiesDelete(max_age_seconds=86400)
    bridge = await make_bridge(
        capabilities=capabilities,
        link_url_template="https://chat.example.org/bots/muster?start={token}",
        limits=Limits(
            max_body_length=100_000,
            edit_max_age_seconds=172800,
            send_per_second=30,
            send_per_second_per_channel=0.5,
        ),
    )
    response = await bridge.call("GET", "/v1/info")
    info = response.json()
    assert set(info) == {
        "protocol_version",
        "bridge",
        "messenger",
        "capabilities",
        "limits",
        "user_lookup",
        "event_delivery",
        "link_url_template",
    }
    assert len(info["capabilities"]) == len(type(capabilities).model_fields)
    assert len(info["limits"]) == len(Limits.model_fields)
    check_exchange("getInfo", response, valid_request=True)


class Headers(Mapping[str, str]):
    """Request headers whose names match in any case, as openapi-core looks them up."""

    def __init__(self, items: Iterable[tuple[str, str]]) -> None:
        self._items = {name.lower(): value for name, value in items}

    def __getitem__(self, name: str) -> str:
        return self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class Push:
    """A push as openapi-core validates a webhook request."""

    body: bytes
    parameters: RequestParameters
    name: str = "bridgeEvent"
    method: str = "post"
    content_type: str = "application/json"


async def test_pushes_match_the_webhook(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    await every_event(bridge, FailingStore())
    types = set()
    for _ in range(5):
        pushed = await callback.next()
        headers = Headers(list(pushed.headers.items()))
        SPEC.validate_webhook_request(Push(pushed.body, RequestParameters(header=headers)))
        types.add(json.loads(pushed.body)["type"])
    assert types == {"press", "thread_message", "account_link", "message_deleted"}


async def test_a_push_without_its_signature_does_not_match(make_bridge: MakeBridge, callbacks: Callbacks) -> None:
    callback = callbacks.add()
    bridge = await make_bridge(bots=two_bots(main=callback.target()))
    await bridge.report_press("u1")
    pushed = await callback.next()
    headers = Headers([(k, v) for k, v in pushed.headers.items() if k != "webhook-signature"])
    with pytest.raises(MissingRequiredParameter, match="webhook-signature"):
        SPEC.validate_webhook_request(Push(pushed.body, RequestParameters(header=headers)))


@pytest.mark.parametrize(
    ("status", "body", "media_type"),
    [
        (201, {"message_id": "", "truncated": False, "replayed": False}, "application/json"),
        (201, {"message_id": "m1", "truncated": "no", "replayed": False}, "application/json"),
        (403, {"title": "Forbidden", "status": 403, "class": "weird"}, "application/problem+json"),
    ],
)
def test_an_answer_that_does_not_match_is_found(status: int, body: dict[str, Any], media_type: str) -> None:
    request = httpx.Request("POST", "http://bridge" + POST, headers={"Content-Type": "application/json"}, content=b"{}")
    response = httpx.Response(status, json=body, headers={"Content-Type": media_type}, request=request)
    with pytest.raises(InvalidData):
        check_exchange("postMessage", response, valid_request=False)
