# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import dataclasses
import logging
import tomllib
from datetime import UTC
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from conftest import TEST_SECRET, TOKEN_MAIN, FakeMessenger, MinimalMessenger, make_config

import muster_bridge
from muster_bridge import (
    Bot,
    BridgeApp,
    ButtonStyle,
    Capabilities,
    CapabilitiesButtons,
    CapabilitiesDelete,
    CapabilitiesPrivateAnswer,
    Config,
    ConfigError,
    Limits,
    MemoryStore,
    Messenger,
    PushTarget,
)
from muster_bridge.config import ParsedTarget, SystemClock

pytestmark = pytest.mark.anyio

PYPROJECT = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())


async def get_info(app: BridgeApp, token: str = TOKEN_MAIN) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as client:
        return await client.get("/v1/info", headers={"Authorization": f"Bearer {token}"})


def refusal(config: Config, messenger: object | None = None) -> str:
    """Return the ConfigError of the configuration, with a FakeMessenger unless another messenger is given."""
    with pytest.raises(ConfigError) as caught:
        BridgeApp(config, cast("Messenger", FakeMessenger() if messenger is None else messenger))
    assert str(caught.value) == "\n".join(caught.value.problems)
    return str(caught.value)


def minimal_config(**changes: Any) -> Config:
    config = Config(
        name="n",
        version="v",
        messenger_name="Chat",
        bots=[Bot(name="main", token="t")],
        limits=Limits(max_body_length=4000, edit_max_age_seconds=None),
    )
    return dataclasses.replace(config, **changes)


def test_pyproject_declares_the_distribution() -> None:
    project = PYPROJECT["project"]
    assert project["name"] == "muster-bridge"
    assert project["license"] == "MIT"
    assert project["requires-python"] == ">=3.11"
    names = {dependency.split(">")[0].split("=")[0] for dependency in project["dependencies"]}
    assert names == {"httpx", "pydantic", "starlette"}
    assert PYPROJECT["build-system"]["build-backend"] == "hatchling.build"


def test_version_matches_pyproject() -> None:
    assert muster_bridge.__version__ == PYPROJECT["project"]["version"]


async def test_builds_info() -> None:
    response = await get_info(BridgeApp(minimal_config(), MinimalMessenger()), "t")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == (
        b'{"protocol_version":"1","bridge":{"name":"n","version":"v"},"messenger":{"name":"Chat"},'
        b'"capabilities":{},"limits":{"max_body_length":4000,"edit_max_age_seconds":null},'
        b'"user_lookup":"none","event_delivery":["poll","push"]}'
    )


async def test_info_with_every_member() -> None:
    config = make_config(
        link_url_template="https://chat.example.org/bot?start={token}",
        event_delivery=["push"],
        limits=Limits(max_body_length=500, edit_max_age_seconds=3600, send_per_second=30),
    )
    info = (await get_info(BridgeApp(config, FakeMessenger()))).json()
    assert info["link_url_template"] == "https://chat.example.org/bot?start={token}"
    assert info["user_lookup"] == "login"
    assert info["event_delivery"] == ["push"]
    assert info["limits"] == {"max_body_length": 500, "edit_max_age_seconds": 3600, "send_per_second": 30.0}
    assert info["capabilities"] == {
        "buttons": {"max_per_message": 3, "max_per_row": 3, "data_max_bytes": 64, "styles": ["primary", "danger"]},
        "thread_messages": "all",
        "direct_messages": {"bot_can_start": True},
        "private_answers": ["ephemeral"],
        "native_mentions": True,
        "quiet_messages": True,
        "status_color": True,
        "delete": {"max_age_seconds": None},
        "deletion_events": True,
    }


async def test_later_changes_of_the_config_models_change_nothing() -> None:
    bots = [Bot(name="main", token=TOKEN_MAIN)]
    config = make_config(bots=bots)
    app = BridgeApp(config, FakeMessenger())
    before = (await get_info(app)).content
    assert config.capabilities.buttons is not None
    config.capabilities.buttons.max_per_message = 99
    config.limits.max_body_length = 9000
    bots[0] = Bot(name="main", token="changed")
    response = await get_info(app)
    assert response.status_code == 200, "the original token was refused"
    assert response.content == before


@pytest.mark.parametrize(
    ("bots", "want"),
    [
        pytest.param([], ["bots is empty: a bridge needs at least one bot"], id="no bot"),
        pytest.param([Bot(name="", token="t")], ["bots[0].name is empty"], id="empty name"),
        pytest.param([Bot(name=" a", token="t")], ["bots[0].name has white space around it"], id="name with spaces"),
        pytest.param([Bot(name="a", token="")], ["bots[0].token is empty"], id="empty token"),
        pytest.param([Bot(name="a", token="t\n")], ["bots[0].token has white space around it"], id="spaced token"),
        pytest.param(
            [Bot(name="a", token="t1"), Bot(name="a", token="t2")],
            ['bots[1].name "a" is the name of bots[0] too'],
            id="same name",
        ),
        pytest.param(
            [Bot(name="a", token="same-token"), Bot(name="b", token="same-token")],
            ["bots[1].token is the token of bots[0] too"],
            id="same token",
        ),
        pytest.param([Bot(name="", token="")], ["bots[0].name is empty", "bots[0].token is empty"], id="two problems"),
    ],
)
def test_refuses_bots(bots: list[Bot], want: list[str]) -> None:
    error = refusal(make_config(bots=bots))
    for text in want:
        assert text in error
    assert "same-token" not in error


def test_refuses_features_without_their_protocols() -> None:
    error = refusal(make_config(), MinimalMessenger())
    assert 'user_lookup "login" needs a messenger that implements UserFinder (find_user)' in error
    assert (
        "capabilities.direct_messages needs a messenger that implements DirectMessenger (send_direct_message)" in error
    )
    assert "capabilities.delete needs a messenger that implements Deleter (delete_message)" in error
    capabilities = make_config().capabilities.model_copy(update={"direct_messages": None, "delete": None})
    BridgeApp(make_config(user_lookup="none", capabilities=capabilities), MinimalMessenger())


class NotAsync:
    """A messenger whose post_message is not a coroutine function and which lacks edit_message."""

    def post_message(self, bot: str, message: object) -> object:
        return None

    async def reply_in_thread(self, bot: str, message_id: str, reply: object) -> object:
        return None

    async def check_channel(self, bot: str, channel_id: str) -> str:
        return channel_id

    async def answer_event(self, bot: str, event: object, text: str) -> object:
        return None

    def find_user(self, bot: str, handle: str) -> object:
        return None


def test_refuses_methods_that_are_not_coroutines() -> None:
    error = refusal(make_config(), NotAsync())
    assert (
        "post_message of the messenger is not a coroutine function: define it with async def, or mark a wrapper "
        "that returns a coroutine with inspect.markcoroutinefunction"
    ) in error
    assert "the messenger lacks edit_message" in error
    assert 'user_lookup "login" needs a messenger that implements UserFinder (find_user)' in error
    assert "the messenger lacks reply_in_thread" not in error
    with pytest.raises(ConfigError, match="the messenger is None"):
        BridgeApp(make_config(), cast("Messenger", None))


def limits(**values: Any) -> Limits:
    """Return limits built around their checks, as a caller may build them."""
    return Limits.model_construct(**{"max_body_length": 500, "edit_max_age_seconds": None, **values})


def buttons(**values: Any) -> Capabilities:
    """Return capabilities whose buttons are built around their checks."""
    built = CapabilitiesButtons.model_construct(
        **{"max_per_message": 3, "max_per_row": 3, "data_max_bytes": 64, "styles": [], **values}
    )
    return Capabilities(buttons=built)


def nan_limits() -> Limits:
    built = Limits(max_body_length=500, edit_max_age_seconds=None)
    built.send_per_second = float("nan")
    return built


@pytest.mark.parametrize(
    ("changes", "want"),
    [
        pytest.param({"name": ""}, "/bridge/name: String should have at least 1 character", id="empty name"),
        pytest.param({"name": "x" * 101}, "/bridge/name: String should have at most 100 characters", id="long name"),
        pytest.param({"version": ""}, "/bridge/version: String should have at least 1 character", id="empty version"),
        pytest.param({"messenger_name": ""}, "/messenger/name: String", id="empty messenger name"),
        pytest.param(
            {"limits": limits(max_body_length=499)},
            "/limits/max_body_length: Input should be greater than or equal to 500",
            id="max_body_length 499",
        ),
        pytest.param(
            {"limits": limits(max_body_length=100_001)},
            "limits.max_body_length is above 100000, the most this library accepts",
            id="max_body_length 100001",
        ),
        pytest.param(
            {"limits": limits(edit_max_age_seconds=299)},
            "/limits/edit_max_age_seconds: Input should be greater than or equal to 300",
            id="edit_max_age_seconds 299",
        ),
        pytest.param({"limits": limits(send_per_second=0.0)}, "/limits/send_per_second: Input", id="send_per_second 0"),
        pytest.param({"limits": nan_limits()}, "the info this configuration makes cannot be encoded", id="NaN"),
        pytest.param(
            {"capabilities": buttons(data_max_bytes=63)},
            "/capabilities/buttons/data_max_bytes: Input should be greater than or equal to 64",
            id="data_max_bytes 63",
        ),
        pytest.param(
            {"capabilities": buttons(max_per_message=1)},
            "/capabilities/buttons/max_per_message: Input should be greater than or equal to 2",
            id="max_per_message 1",
        ),
        pytest.param(
            {"capabilities": buttons(max_per_row=0)},
            "/capabilities/buttons/max_per_row: Input should be greater than or equal to 1",
            id="max_per_row 0",
        ),
        pytest.param(
            {"capabilities": buttons(styles=[ButtonStyle.primary, ButtonStyle.primary])},
            "/capabilities/buttons/styles: the items are not unique",
            id="duplicate style",
        ),
        pytest.param(
            {"capabilities": buttons(styles=["pink"])},
            "/capabilities/buttons/styles/0: Input should be 'primary', 'danger', 'neutral' or 'success'",
            id="unknown style",
        ),
        pytest.param(
            {
                "capabilities": Capabilities(
                    private_answers=[CapabilitiesPrivateAnswer.popup, CapabilitiesPrivateAnswer.popup]
                )
            },
            "/capabilities/private_answers: the items are not unique",
            id="duplicate private answer",
        ),
        pytest.param(
            {"capabilities": Capabilities(delete=CapabilitiesDelete.model_construct(max_age_seconds=0))},
            "/capabilities/delete/max_age_seconds: Input should be greater than or equal to 1",
            id="delete max_age_seconds 0",
        ),
        pytest.param({"event_delivery": []}, "/event_delivery: List should have at least 1 item", id="no delivery"),
        pytest.param(
            {"event_delivery": ["poll", "poll"]}, "/event_delivery: the items are not unique", id="duplicate delivery"
        ),
        pytest.param({"event_delivery": ["pigeon"]}, "/event_delivery/0: Input should be", id="unknown delivery"),
        pytest.param(
            {"link_url_template": "ftp://x/{token}"},
            "/link_url_template: String should match pattern",
            id="link without http",
        ),
        pytest.param({"link_url_template": "https://x/start"}, "/link_url_template: String", id="link without token"),
        pytest.param(
            {"link_url_template": "https://x/{token}" + "a" * 1010},
            "/link_url_template: String should have at most 1024 characters",
            id="link too long",
        ),
        pytest.param({"user_lookup": "phone"}, "/user_lookup: Input should be", id="unknown user_lookup"),
        pytest.param(
            {"capabilities": "buttons"}, "capabilities is not a muster_bridge.Capabilities", id="capabilities type"
        ),
        pytest.param({"limits": {"max_body_length": 500}}, "limits is not a muster_bridge.Limits", id="limits type"),
    ],
)
def test_refuses_info_against_the_specification(changes: dict[str, Any], want: str) -> None:
    error = refusal(make_config(**changes))
    assert want in error
    assert "the info this configuration makes" in error or "is not a muster_bridge" in error or "above" in error


async def test_button_styles_default_to_none_shown() -> None:
    capabilities = Capabilities(buttons=CapabilitiesButtons(max_per_message=2, max_per_row=2, data_max_bytes=64))
    info = (await get_info(BridgeApp(minimal_config(capabilities=capabilities), MinimalMessenger()), "t")).json()
    assert info["capabilities"] == {
        "buttons": {"max_per_message": 2, "max_per_row": 2, "data_max_bytes": 64, "styles": []}
    }


@pytest.mark.parametrize(
    ("bots", "event_delivery", "want"),
    [
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="/callback", secret=TEST_SECRET))],
            None,
            "bots[0].push: the url needs the http or https scheme, a host and no user information",
            id="relative url",
        ),
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="ftp://muster/cb", secret=TEST_SECRET))],
            None,
            "bots[0].push: the url",
            id="ftp url",
        ),
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="https://u:p@muster/cb", secret=TEST_SECRET))],
            None,
            "bots[0].push: the url",
            id="user info",
        ),
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="https://muster:port/cb", secret=TEST_SECRET))],
            None,
            "bots[0].push: the url",
            id="bad port",
        ),
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="https://muster/cb", secret=TEST_SECRET[6:]))],
            None,
            "bots[0].push: the secret needs the whsec_ prefix and then a non-empty key in standard base64",
            id="no prefix",
        ),
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="https://muster/cb", secret="whsec_not base64!"))],
            None,
            "bots[0].push: the secret",
            id="not base64",
        ),
        pytest.param(
            [Bot(name="a", token="t", push=PushTarget(url="https://muster/cb", secret="whsec_"))],
            None,
            "bots[0].push: the secret",
            id="empty key",
        ),
        pytest.param(
            [
                Bot(name="a", token="t"),
                Bot(name="b", token="u", push=PushTarget(url="https://muster/cb", secret=TEST_SECRET)),
            ],
            ["poll"],
            "bots[1].push is set, but event_delivery does not offer push",
            id="poll only",
        ),
    ],
)
def test_refuses_push_targets(bots: list[Bot], event_delivery: list[str] | None, want: str) -> None:
    error = refusal(make_config(bots=bots, event_delivery=event_delivery))
    assert want in error
    assert TEST_SECRET[6:] not in error
    assert "not base64!" not in error


def test_accepts_push_targets() -> None:
    bots = [Bot(name="main", token="t", push=PushTarget(url="https://muster.example.org:8443/cb", secret=TEST_SECRET))]
    BridgeApp(make_config(bots=bots), FakeMessenger())


def test_accepts_a_minimal_config() -> None:
    app = BridgeApp(minimal_config(), MinimalMessenger())
    assert isinstance(app._store, MemoryStore)
    assert app._log is logging.getLogger("muster_bridge")


def test_config_error_lists_every_problem() -> None:
    with pytest.raises(ConfigError) as caught:
        BridgeApp(minimal_config(name="", bots=[]), MinimalMessenger())
    assert caught.value.problems == (
        "bots is empty: a bridge needs at least one bot",
        "the info this configuration makes does not match api/bridge.yaml: /bridge/name: "
        "String should have at least 1 character",
    )
    assert str(caught.value) == "\n".join(caught.value.problems)
    assert isinstance(caught.value, ValueError)


def test_reprs_hide_tokens_and_secrets() -> None:
    target = PushTarget(url="https://muster.example.org/cb", secret=TEST_SECRET)
    bot = Bot(name="main", token="s3cret-token", push=target)
    for text in (repr(bot), repr(minimal_config(bots=[bot])), repr(ParsedTarget(target.url, b"s3cret-key"))):
        assert "s3cret" not in text
        assert TEST_SECRET not in text
        assert "muster.example.org" in text


async def test_system_clock() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is UTC
    await clock.sleep(0)
