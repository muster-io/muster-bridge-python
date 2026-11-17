# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import json
from typing import Any

import pytest
from conftest import (
    ACTION_BODY,
    BOTH_ROLES,
    NOTIFICATION_BODY,
    TOKEN_OTHER,
    FailingStore,
    MakeBridge,
    logged,
    problem_of,
    raising,
)

from muster_bridge import (
    Forbidden,
    Invalid,
    NotificationInputLanguage,
    NotificationSent,
    NotOfferedError,
    RateLimited,
    UnknownBotError,
    UnknownNotificationError,
)
from muster_bridge.problems import DETAIL_MESSENGER

pytestmark = pytest.mark.anyio

NOTIFY = "/v1/notifications"


def body(**changes: Any) -> str:
    """Return a notification request with the members changed; a member set to None is left out."""
    notification: dict[str, Any] = json.loads(ACTION_BODY)
    notification.update(changes)
    return json.dumps({name: value for name, value in notification.items() if value is not None})


async def test_send_notification_reaches_the_gateway(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(roles=BOTH_ROLES)
    gateway = bridge.gateway
    response = await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    assert response.status_code == 201
    assert response.text == '{"notification_id":"n1","replayed":false}'
    (call,) = gateway.calls
    assert (call.bot, call.key) == ("main", "k1")
    notification = call.notification
    assert notification is not None
    assert (notification.contact, notification.text) == ("+10000000001", "HighErrorRate firing")
    assert notification.language is NotificationInputLanguage.ru
    assert notification.action is not None
    assert (notification.action.data, notification.action.label) == ("signed-data-1", "Press 1 to acknowledge")


async def test_send_notification_without_an_action(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(gateway_only=True)
    response = await bridge.call("POST", NOTIFY, key="k1", body=NOTIFICATION_BODY)
    assert response.status_code == 201
    (call,) = bridge.gateway.calls
    assert call.notification is not None
    assert call.notification.action is None
    with pytest.raises(UnknownNotificationError):
        await bridge.app.acknowledge("main", "n1")


async def test_send_notification_is_idempotent(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(gateway_only=True)
    first = await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    again = await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    other = await bridge.call("POST", NOTIFY, key="k2", body=ACTION_BODY)
    assert first.json() == {"notification_id": "n1", "replayed": False}
    assert again.status_code == 201
    assert again.json() == {"notification_id": "n1", "replayed": True}
    assert other.json() == {"notification_id": "n2", "replayed": False}
    assert bridge.gateway.count("send_notification") == 2
    assert (await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY, token=TOKEN_OTHER)).json()[
        "notification_id"
    ] == "n3"


@pytest.mark.parametrize("key", [None, "", "has space", "k" * 256])
async def test_send_notification_needs_an_idempotency_key(make_bridge: MakeBridge, key: str | None) -> None:
    bridge = await make_bridge(gateway_only=True)
    problem_of(await bridge.call("POST", NOTIFY, key=key, body=ACTION_BODY), 400, "invalid")
    assert bridge.gateway.calls == []


@pytest.mark.parametrize(
    ("request_body", "reason"),
    [
        (body(action={"data": "x" * 65, "label": "l"}), "/action/data"),
        (body(action={"data": "d" * 64, "label": "l"}), None),
        (body(action={"data": "bad data", "label": "l"}), "/action/data"),
        (body(action={"data": "", "label": "l"}), "/action/data"),
        (body(action={"data": "d", "label": ""}), "/action/label"),
        (body(text="é" * 1000), None),
        (body(text="é" * 1001), "/text"),
        (body(text=""), "/text"),
        (body(contact="1" * 201), "/contact"),
        (body(contact=""), "/contact"),
        (body(language="de"), "/language"),
        (body(language=None), "/language"),
        ('{"contact":"c"', "body"),
        ("[]", "body"),
    ],
)
async def test_send_notification_refuses_what_the_specification_refuses(
    make_bridge: MakeBridge, request_body: str, reason: str | None
) -> None:
    bridge = await make_bridge(gateway_only=True)
    response = await bridge.call("POST", NOTIFY, key="k1", body=request_body)
    if reason is None:
        assert response.status_code == 201
        return
    problem = problem_of(response, 400, "invalid")
    assert reason in problem["detail"]
    assert bridge.gateway.calls == []


async def test_a_bridge_without_the_role_refuses_the_call(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge()
    response = await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    assert problem_of(response, 400, "invalid")["detail"] == "This bridge does not have the role gateway."
    assert bridge.gateway.calls == []
    problem_of(await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY, token="wrong"), 401, "forbidden")


async def test_a_gateway_without_the_role_messenger_serves_no_messenger_operation(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(gateway_only=True)
    for method, path in (
        ("POST", "/v1/messages"),
        ("PATCH", "/v1/messages/m1"),
        ("DELETE", "/v1/messages/m1"),
        ("POST", "/v1/messages/m1/thread-replies"),
        ("POST", "/v1/channels/alerts/check"),
        ("GET", "/v1/users?handle=alice"),
        ("POST", "/v1/direct-messages"),
    ):
        response = await bridge.call(method, path, key="k1", body="{}")
        problem_of(response, 404, "invalid")
    assert (await bridge.call("GET", "/v1/info")).status_code == 200
    assert (await bridge.call("GET", "/v1/events?wait=0")).status_code == 200
    assert (await bridge.call("POST", "/v1/answers", key="k1", body='{"event_id":"e","text":"t"}')).status_code == 404


@pytest.mark.parametrize(
    ("error", "status", "error_class"),
    [
        (Invalid("The contact cannot be called."), 400, "invalid"),
        (Forbidden("The provider refused the credentials."), 403, "forbidden"),
        (RateLimited("Slow down.", retry_after=2, scope="bot"), 429, "rate_limited"),
        (RuntimeError("the provider's socket broke"), 500, "transient"),
    ],
)
async def test_the_gateways_errors_are_answered_by_class(
    make_bridge: MakeBridge, error: BaseException, status: int, error_class: str
) -> None:
    bridge = await make_bridge(gateway_only=True)
    bridge.gateway.send = raising(error)
    response = await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    problem = problem_of(response, status, error_class)
    if error_class == "transient":
        assert problem["detail"] == DETAIL_MESSENGER
    else:
        assert problem["detail"] == error.detail  # type: ignore[attr-defined]
    bridge.gateway.send = None
    assert (await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)).json()["replayed"] is False


async def test_the_contact_and_the_text_never_reach_the_answer_or_the_log(
    make_bridge: MakeBridge, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = await make_bridge(gateway_only=True)
    for key, error, status in (
        ("k1", Invalid("cannot call +10000000001"), 400),
        ("k2", RuntimeError("cannot read out HighErrorRate firing to +10000000001"), 500),
    ):
        bridge.gateway.send = raising(error)
        response = await bridge.call("POST", NOTIFY, key=key, body=ACTION_BODY)
        assert response.status_code == status
        assert "+10000000001" not in response.text
        assert "HighErrorRate" not in response.text
    log = logged(caplog)
    assert "messenger call failed" in log
    assert "+10000000001" not in log
    assert "HighErrorRate" not in log


async def test_an_answer_that_is_not_a_notification_sent_is_transient(
    make_bridge: MakeBridge, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = await make_bridge(gateway_only=True)

    async def wrong(*_: object) -> Any:
        return NotificationSent.model_construct(notification_id="", replayed=False)

    bridge.gateway.send = wrong
    problem = problem_of(await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY), 500, "transient")
    assert problem["detail"] == DETAIL_MESSENGER
    assert "does not match api/bridge.yaml" in logged(caplog)
    with pytest.raises(UnknownNotificationError):
        await bridge.app.acknowledge("main", "")


async def test_the_action_is_kept_for_24_hours(make_bridge: MakeBridge) -> None:
    bridge = await make_bridge(gateway_only=True)
    await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    await bridge.call("POST", NOTIFY, key="k2", body=ACTION_BODY)
    bridge.clock.advance(24 * 3600 - 1)
    await bridge.app.acknowledge("main", "n1")
    bridge.clock.advance(1)
    with pytest.raises(UnknownNotificationError):
        await bridge.app.acknowledge("main", "n2")
    with pytest.raises(UnknownNotificationError):
        await bridge.app.acknowledge("other", "n1")


async def test_a_store_that_cannot_keep_the_action_does_not_fail_the_call(
    make_bridge: MakeBridge, caplog: pytest.LogCaptureFixture
) -> None:
    store = FailingStore()
    store.fail("save_notification", RuntimeError("down +10000000001"))
    bridge = await make_bridge(store=store, gateway_only=True)
    response = await bridge.call("POST", NOTIFY, key="k1", body=ACTION_BODY)
    assert response.status_code == 201
    assert "cannot keep the action of a notification" in logged(caplog)
    assert "+10000000001" not in logged(caplog)
    with pytest.raises(UnknownNotificationError):
        await bridge.app.acknowledge("main", "n1")
    store.fail("save_notification", None)
    store.fail("find_notification", RuntimeError("down"))
    with pytest.raises(RuntimeError, match="down"):
        await bridge.app.acknowledge("main", "n1")


async def test_acknowledge_needs_the_role_and_a_bot(make_bridge: MakeBridge) -> None:
    messenger_only = await make_bridge()
    with pytest.raises(NotOfferedError):
        await messenger_only.app.acknowledge("main", "n1")
    gateway = await make_bridge(gateway_only=True)
    with pytest.raises(UnknownBotError):
        await gateway.app.acknowledge("nobody", "n1")
