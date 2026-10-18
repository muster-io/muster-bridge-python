# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import math
from typing import NoReturn
from urllib.parse import quote, quote_plus

import pytest
from conftest import (
    DM_BODY,
    EDIT_BODY,
    POST_BODY,
    TOKEN_MAIN,
    TOKEN_OTHER,
    Harness,
    MakeBridge,
    answer_body,
    answering,
    logged,
    problem_of,
    raising,
)

from muster_bridge import Bot, BridgeError, Forbidden, Gone, Invalid, MessagePosted, RateLimited, Transient
from muster_bridge.problems import DECLARED, DETAIL_MESSENGER, Redactor, cut, map_class, retry_after_seconds

pytestmark = pytest.mark.anyio

SECRET_TOKEN = "s3cret/token+x?y"
"""A token whose escaped forms differ from it."""


class UnknownClassError(BridgeError):
    error_class = "weird"  # type: ignore[assignment]  # a class the protocol does not define, on purpose


@pytest.mark.parametrize(
    ("error", "status", "retry_after", "scope", "reason"),
    [
        pytest.param(Transient("The messenger is down."), 500, None, None, None, id="transient"),
        pytest.param(RateLimited("Slow down.", retry_after=1.2), 429, "2", "channel", None, id="rate_limited"),
        pytest.param(RateLimited("Slow down.", retry_after=30, scope="bot"), 429, "30", "bot", None, id="bot"),
        pytest.param(RateLimited("Slow down.", retry_after=0), 429, "0", "channel", None, id="rate_limited no wait"),
        pytest.param(RateLimited("x", retry_after=-1), 429, "0", "channel", None, id="rate_limited negative"),
        pytest.param(Gone("That message is gone."), 404, None, None, None, id="gone"),
        pytest.param(Gone("Too old.", edit_window_closed=True), 404, None, None, "edit_window_closed", id="window"),
        pytest.param(Forbidden("The bot has not joined this channel."), 403, None, None, None, id="forbidden"),
        pytest.param(Invalid("Too long for this messenger."), 400, None, None, None, id="invalid"),
    ],
)
async def test_error_classes_become_problems(
    bridge: Harness, error: BridgeError, status: int, retry_after: str | None, scope: str | None, reason: str | None
) -> None:
    bridge.messenger.edit = raising(error)
    response = await bridge.call("PATCH", "/v1/messages/m1", body=EDIT_BODY)
    assert error.error_class is not None
    problem = problem_of(response, status, error.error_class)
    assert problem["detail"] == error.detail
    assert response.headers.get("retry-after") == retry_after
    assert problem.get("scope") == scope
    assert problem.get("reason") == reason
    assert response.headers["cache-control"] == "no-store"


REQUESTS = {
    "post": ("POST", "/v1/messages", "k1", POST_BODY),
    "check": ("POST", "/v1/channels/alerts/check", None, None),
    "delete": ("DELETE", "/v1/messages/m1", None, None),
    "dm": ("POST", "/v1/direct-messages", "k1", DM_BODY),
    "users": ("GET", "/v1/users?handle=alice", None, None),
    "answer": ("POST", "/v1/answers", "k1", None),
}


@pytest.mark.parametrize(
    ("request_name", "error_type", "status", "want"),
    [
        pytest.param("delete", Gone, 204, None, id="deleteMessage gone"),
        pytest.param("post", Gone, 403, "forbidden", id="postMessage gone"),
        pytest.param("dm", Gone, 403, "forbidden", id="sendDirectMessage gone"),
        pytest.param("check", Gone, 403, "forbidden", id="checkChannel gone"),
        pytest.param("check", Invalid, 403, "forbidden", id="checkChannel invalid"),
        pytest.param("delete", Invalid, 500, "transient", id="deleteMessage invalid"),
        pytest.param("users", Forbidden, 500, "transient", id="findUsers forbidden"),
        pytest.param("users", Gone, 500, "transient", id="findUsers gone"),
        pytest.param("answer", Forbidden, 500, "transient", id="answerEvent forbidden"),
        pytest.param("answer", Gone, 404, "gone", id="answerEvent gone"),
        pytest.param("post", BridgeError, 500, "transient", id="no class"),
        pytest.param("post", UnknownClassError, 500, "transient", id="unknown class"),
    ],
)
async def test_undeclared_classes_are_mapped(
    bridge: Harness,
    caplog: pytest.LogCaptureFixture,
    request_name: str,
    error_type: type[BridgeError],
    status: int,
    want: str | None,
) -> None:
    error = error_type("The account is missing.")
    m = bridge.messenger
    m.post = m.check = m.delete = m.dm = m.find = m.answer = raising(error)
    method, path, key, body = REQUESTS[request_name]
    if request_name == "answer":
        body = answer_body(await bridge.report_press("u1"))
    response = await bridge.call(method, path, key=key, body=body)
    if want is None:
        assert response.status_code == status
        assert response.content == b""
        return
    problem = problem_of(response, status, want)
    log = logged(caplog)
    if want == "transient":
        assert problem["detail"] == DETAIL_MESSENGER
        assert "ERROR the messenger answered a class the operation does not declare: " in log
        assert f"class={error_type.error_class}" in log
    else:
        assert problem["detail"] == "The account is missing."
        assert "ERROR" not in log


def test_map_class_keeps_declared_classes() -> None:
    for op_id, classes in DECLARED.items():
        for error_class in classes:
            assert map_class(op_id, error_class) == (error_class, False)


@pytest.mark.parametrize("chained", [False, True])
async def test_other_exceptions_are_transient_and_redacted(
    make_bridge: MakeBridge, caplog: pytest.LogCaptureFixture, chained: bool
) -> None:
    bridge = await make_bridge(bots=[Bot(name="main", token=SECRET_TOKEN)])
    text = (
        f"dial https://chat.example.org/bot{SECRET_TOKEN}/send?token={quote_plus(SECRET_TOKEN, safe='')}"
        f"&p={quote(SECRET_TOKEN, safe='$&+,:;=@')} failed"
    )

    def dial() -> NoReturn:
        raise ConnectionError(text)

    async def post(*_: object) -> MessagePosted:
        if not chained:
            dial()
        try:
            dial()
        except ConnectionError as cause:
            raise RuntimeError("posting failed") from cause

    bridge.messenger.post = post
    response = await bridge.call("POST", "/v1/messages", token=SECRET_TOKEN, key="k1", body=POST_BODY)
    problem = problem_of(response, 500, "transient")
    assert problem["detail"] == DETAIL_MESSENGER
    assert "dial" not in response.text
    assert "s3cret" not in response.text
    log = logged(caplog)
    assert "ERROR messenger call failed: operation=postMessage bot=main error=" in log
    assert "dial https://chat.example.org/bot[redacted]/send?token=[redacted]&p=[redacted] failed" in log
    assert "Traceback (most recent call last)" in log
    assert ("The above exception was the direct cause" in log) is chained
    assert "s3cret" not in log


async def test_detail_is_cut_and_redacted(bridge: Harness) -> None:
    bridge.messenger.check = raising(Forbidden("é" * 1001))
    response = await bridge.call("POST", "/v1/channels/alerts/check")
    assert problem_of(response, 403, "forbidden")["detail"] == "é" * 1000
    bridge.messenger.check = raising(Forbidden(f"The token {TOKEN_MAIN} or {TOKEN_OTHER} was refused."))
    response = await bridge.call("POST", "/v1/channels/alerts/check")
    assert problem_of(response, 403, "forbidden")["detail"] == "The token [redacted] or [redacted] was refused."
    bridge.messenger.check = raising(Forbidden())
    response = await bridge.call("POST", "/v1/channels/alerts/check")
    assert "detail" not in problem_of(response, 403, "forbidden")


def test_error_string() -> None:
    assert str(Transient("down")) == "transient: down"
    assert str(Gone()) == "gone"
    assert str(BridgeError()) == "bridge error"
    assert str(BridgeError("x")) == "bridge error: x"
    assert RateLimited(retry_after=2).scope == "channel"
    assert RateLimited(retry_after=2).retry_after == 2
    assert Gone().edit_window_closed is False
    assert isinstance(Invalid("bad"), BridgeError)
    assert Invalid("bad").detail == "bad"


def test_rate_limited_needs_its_wait_and_a_known_scope() -> None:
    with pytest.raises(TypeError, match="retry_after"):
        RateLimited("Slow down.")  # type: ignore[call-arg]  # without the wait, on purpose
    with pytest.raises(ValueError, match='scope must be "channel" or "bot"'):
        RateLimited(retry_after=1, scope="everything")  # type: ignore[arg-type]  # a scope the protocol lacks, on purpose
    for wait in (math.inf, math.nan):
        with pytest.raises(ValueError, match="retry_after must be a finite number of seconds"):
            RateLimited(retry_after=wait)


async def test_undefined_reason_is_not_sent(bridge: Harness, caplog: pytest.LogCaptureFixture) -> None:
    bridge.messenger.edit = raising(Gone("Too old to edit.", edit_window_closed=True))
    response = await bridge.call("PATCH", "/v1/messages/m1", body=EDIT_BODY)
    assert problem_of(response, 404, "gone")["reason"] == "edit_window_closed"
    bridge.messenger.post = raising(Gone("The channel is gone.", edit_window_closed=True))
    response = await bridge.call("POST", "/v1/messages", key="k1", body=POST_BODY)
    problem = problem_of(response, 403, "forbidden")
    assert "reason" not in problem
    assert problem["detail"] == "The channel is gone."
    log = logged(caplog)
    assert log.count("a reason the protocol does not define") == 1
    assert (
        "ERROR the messenger gave a reason the protocol does not define for this answer; it is not sent: "
        "operation=postMessage bot=main class=forbidden reason=edit_window_closed"
    ) in log


async def test_a_bridge_error_without_detail(bridge: Harness) -> None:
    bridge.messenger.find = raising(RateLimited(retry_after=0.5, scope="bot"))
    response = await bridge.call("GET", "/v1/users?handle=alice")
    problem = problem_of(response, 429, "rate_limited")
    assert "detail" not in problem
    assert problem["scope"] == "bot"
    assert response.headers["retry-after"] == "1"
    bridge.messenger.find = answering(None)
    assert (await bridge.call("GET", "/v1/users?handle=alice")).json() == {"items": []}


def test_cut_and_retry_after_seconds() -> None:
    assert cut("abc", 5) == "abc"
    assert cut("ééé", 2) == "éé"
    assert cut("ab", 0) == ""
    assert retry_after_seconds(1.0) == 1
    assert retry_after_seconds(1.001) == 2
    assert retry_after_seconds(-3) == 0


def test_redactor() -> None:
    redact = Redactor()
    assert redact("nothing to hide", "") == "nothing to hide"
    redact.add("abc", "", "abcdef")
    redact.add("abc")
    assert redact("abcdef abc ab") == "[redacted] [redacted] ab"
    assert redact("a/b x", "x") == "a/b [redacted]"
    redact.add("a/b c")
    assert redact("a/b c, a%2Fb%20c, a%2Fb+c") == "[redacted], [redacted], [redacted]"
