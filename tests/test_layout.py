# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import EDIT_BODY, POST_BODY, FakeMessenger, Harness, MakeBridge

from muster_bridge import (
    Layout,
    LayoutAlerts,
    LayoutAlertValue,
    LayoutField,
    LayoutLabel,
    LayoutLink,
    LayoutPerson,
    LayoutStatus,
    MessageContent,
    MessageEdited,
    MessageInput,
    MessagePosted,
)

pytestmark = pytest.mark.anyio

LAYOUT: dict[str, Any] = {
    "number": 42,
    "title": "HighErrorRate",
    "status": {"key": "firing", "word": "Firing"},
    "fields": [
        {"name": "Severity", "value": "critical"},
        {"name": "Route", "value": "payments"},
        {"name": "Started", "value": "2026-10-09 14:03 UTC"},
    ],
    "summary": "Error rate above 5 %",
    "description": "",
    "labels": [{"key": "namespace", "value": "payments"}, {"key": "pod", "value": "3 values"}],
    "alerts": {
        "label": "pod",
        "values": [{"value": "api-7f9c", "resolved": False}, {"value": "api-5d2a", "resolved": True}],
        "total": 9,
    },
    "notices": ["Reopened \u00d72"],
    "assignees": [],
    "owner": {"name": "Alice Smith", "ref": "m1", "team": {"name": "Payments"}},
    "links": [
        {"label": "Open in Muster", "url": "https://muster.example.org/alert-groups/AGK7M3QX9P2RTA"},
        {"label": "Runbook", "url": "https://wiki.example.org/runbooks/höhe"},
    ],
}
"""The layout of the examples of api/bridge.yaml, with an Owner who is mentioned and Alerts that are not all listed."""

MENTION = ',"mentions":[{"ref":"m1","external_id":"u-alice","display_name":"Alice Smith"}]'

RENDERED = "\n".join(
    [
        "<b>Firing</b> #42 HighErrorRate",
        "Severity: critical",
        "Route: payments",
        "Started: 2026-10-09 14:03 UTC",
        "Error rate above 5 %",
        "namespace: payments",
        "pod: 3 values",
        "pod: api-7f9c, <s>api-5d2a</s> and 7 more",
        "Reopened \u00d72",
        "Owner: <@u-alice>",
        '<a href="https://muster.example.org/alert-groups/AGK7M3QX9P2RTA">Open in Muster</a>',
        '<a href="https://wiki.example.org/runbooks/höhe">Runbook</a>',
    ]
)


def with_layout(body: str, layout: dict[str, Any] | str | None = None) -> str:
    """Return a request body with the layout added, LAYOUT unless one is given."""
    value = json.dumps(LAYOUT if layout is None else layout) if not isinstance(layout, str) else layout
    return body.removesuffix("}") + ',"layout":' + value + "}"


def readme_render() -> Any:
    """Return the function render that the README's example for rendering the layout defines."""
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Rendering the layout", 1)[1].split("\n## ", 1)[0]
    match = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert match, "README.md has no example under Rendering the layout"
    scope: dict[str, Any] = {}
    exec(compile(match.group(1), "README.md", "exec"), scope)  # noqa: S102 - the repository's own example
    return scope["render"]


def test_the_models_hold_the_layout_of_the_specification() -> None:
    layout = Layout.model_validate(LAYOUT, strict=False)
    assert isinstance(layout.status, LayoutStatus)
    assert isinstance(layout.fields[0], LayoutField)
    assert isinstance(layout.labels[0], LayoutLabel)
    assert isinstance(layout.alerts, LayoutAlerts)
    assert isinstance(layout.alerts.values[0], LayoutAlertValue)
    assert isinstance(layout.owner, LayoutPerson)
    assert isinstance(layout.links[0], LayoutLink)
    assert layout.model_dump(mode="json", exclude_none=True) == LAYOUT


def test_the_link_of_a_layout_stays_a_string_as_muster_sends_it() -> None:
    link = LayoutLink(label="Runbook", url="https://wiki.example.org/runbooks/höhe?q=a b")
    assert link.url == "https://wiki.example.org/runbooks/höhe?q=a b"


def test_a_message_has_no_layout_unless_muster_sends_one() -> None:
    assert MessageContent.model_validate_json(EDIT_BODY).layout is None
    assert MessageInput.model_validate_json(POST_BODY).layout is None
    assert MessageContent.model_validate_json(with_layout(EDIT_BODY)).layout == Layout.model_validate(LAYOUT)


async def test_the_messenger_gets_the_layout_unchanged(bridge: Harness) -> None:
    posted = await bridge.call("POST", "/v1/messages", key="k1", body=with_layout(POST_BODY))
    assert posted.status_code == 201, posted.text
    edited = await bridge.call("PATCH", "/v1/messages/m1", body=with_layout(EDIT_BODY))
    assert edited.status_code == 200, edited.text
    for call in bridge.messenger.calls:
        assert isinstance(call.payload, MessageContent)
        assert call.payload.layout is not None
        assert call.payload.layout.model_dump(mode="json") == LAYOUT


async def test_a_bridge_renders_the_layout_and_the_body_when_there_is_none(make_bridge: MakeBridge) -> None:
    render = readme_render()
    rendered: list[str] = []

    async def post(_: str, message: MessageInput) -> MessagePosted:
        rendered.append(render(message))
        return MessagePosted(message_id="m1")

    async def edit(_: str, __: str, content: MessageContent) -> MessageEdited:
        rendered.append(render(content))
        return MessageEdited()

    messenger = FakeMessenger()
    messenger.post = post
    messenger.edit = edit
    bridge = await make_bridge(messenger=messenger)
    request = with_layout(POST_BODY.replace(',"mentions":[]', MENTION))
    assert (await bridge.call("POST", "/v1/messages", key="k1", body=request)).status_code == 201
    assert (await bridge.call("POST", "/v1/messages", key="k2", body=POST_BODY)).status_code == 201
    assert (await bridge.call("PATCH", "/v1/messages/m1", body=with_layout(EDIT_BODY))).status_code == 200
    assert rendered[0] == RENDERED
    assert rendered[1] == "b"
    assert rendered[2] == RENDERED.replace("Owner: <@u-alice>", "Owner: Alice Smith")


def test_the_example_counts_more_alerts_from_the_total_and_marks_a_test() -> None:
    render = readme_render()
    layout = {
        **LAYOUT,
        "notices": ["🧪 Test message"],
        "alerts": {"label": "pod", "values": [{"value": "a", "resolved": False}], "total": 1},
        "owner": None,
    }
    body = POST_BODY.replace('"kind":"root"', '"kind":"test"')
    lines = render(MessageInput.model_validate_json(with_layout(body, layout))).split("\n")
    assert lines[0] == "🧪 Test message"
    assert lines.count("🧪 Test message") == 1
    assert "pod: a" in lines
    assert not any(line.startswith("Owner") for line in lines)
    root = render(MessageInput.model_validate_json(with_layout(POST_BODY, layout))).split("\n")
    assert root[0].startswith("<b>Firing</b>")
    assert "🧪 Test message" in root


async def test_a_layout_that_does_not_match_is_dropped_and_the_message_accepted(
    bridge: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    broken = {k: v for k, v in LAYOUT.items() if k != "number"}
    future = {**LAYOUT, "status": {"key": "burning", "word": "x"}}
    no_ref = {**LAYOUT, "owner": {"name": "Alice Smith", "team": {"name": "Payments"}}}
    for i, (path, method, body, at) in enumerate(
        (
            ("/v1/messages", "POST", with_layout(POST_BODY, broken), "/layout/number"),
            ("/v1/messages/m1", "PATCH", with_layout(EDIT_BODY, broken), "/layout/number"),
            ("/v1/messages", "POST", with_layout(POST_BODY, future), "/layout/status/key"),
            ("/v1/messages", "POST", with_layout(POST_BODY, no_ref), "/layout/owner/ref"),
            ("/v1/messages", "POST", with_layout(POST_BODY, "[]"), "/layout"),
        )
    ):
        caplog.clear()
        response = await bridge.call(method, path, key=f"k{i}", body=body)
        assert response.status_code in (200, 201), response.text
        payload = bridge.messenger.calls[-1].payload
        assert isinstance(payload, MessageContent)
        assert payload.layout is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        text = warnings[0].getMessage()
        assert "layout" in text
        assert at in text
        for content in ("HighErrorRate", "Error rate above 5", "Alice Smith", "payments"):
            assert content not in text
    assert len(bridge.messenger.calls) == 5


async def test_a_layout_that_is_null_is_dropped_and_the_message_accepted(
    bridge: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    response = await bridge.call("POST", "/v1/messages", key="k1", body=with_layout(POST_BODY, "null"))
    assert response.status_code == 201, response.text
    payload = bridge.messenger.calls[-1].payload
    assert isinstance(payload, MessageContent)
    assert payload.layout is None
    assert "/layout: must be absent, not null" in next(
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    )


async def test_a_team_that_is_null_drops_the_layout(bridge: Harness, caplog: pytest.LogCaptureFixture) -> None:
    for i, layout in enumerate(
        (
            {**LAYOUT, "owner": {"name": "Alice Smith", "ref": "m1", "team": None}},
            {**LAYOUT, "assignees": [{"name": "Bob", "ref": None, "team": None}]},
        )
    ):
        caplog.clear()
        response = await bridge.call("POST", "/v1/messages", key=f"k{i}", body=with_layout(POST_BODY, layout))
        assert response.status_code == 201, response.text
        payload = bridge.messenger.calls[-1].payload
        assert isinstance(payload, MessageContent)
        assert payload.layout is None
        assert "team: must be absent, not null" in "\n".join(r.getMessage() for r in caplog.records)


async def test_a_ref_and_an_owner_that_are_null_stay_valid(bridge: Harness) -> None:
    layout = {**LAYOUT, "alerts": None, "owner": None, "assignees": [{"name": "Bob", "ref": None}]}
    response = await bridge.call("POST", "/v1/messages", key="k1", body=with_layout(POST_BODY, layout))
    assert response.status_code == 201, response.text
    payload = bridge.messenger.calls[-1].payload
    assert isinstance(payload, MessageContent)
    assert payload.layout is not None
    assert payload.layout.owner is None


async def test_a_body_that_does_not_match_is_still_refused_beside_a_good_layout(
    bridge: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    for method, path, body in (
        ("POST", "/v1/messages", with_layout(POST_BODY.replace('"body":', '"bodyx":', 1))),
        ("PATCH", "/v1/messages/m1", with_layout(EDIT_BODY.replace('"title":', '"titlex":', 1))),
    ):
        response = await bridge.call(method, path, key="k1", body=body)
        assert response.status_code == 400, response.text
        assert response.json()["class"] == "invalid"
    broken_both = with_layout(POST_BODY.replace('"body":', '"bodyx":', 1), "[]")
    assert (await bridge.call("POST", "/v1/messages", key="k2", body=broken_both)).status_code == 400
    assert not bridge.messenger.calls
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


async def test_a_request_of_2_5_mb_is_accepted(bridge: Harness) -> None:
    body = with_layout(POST_BODY)
    padded = body.removesuffix("}") + " " * (2_500_000 - len(body)) + "}"
    assert len(padded) == 2_500_000
    response = await bridge.call("POST", "/v1/messages", key="k1", body=padded)
    assert response.status_code == 201, response.text
