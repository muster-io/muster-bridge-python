# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The configuration of a bridge, and its checks."""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from muster_bridge.models import Capabilities, Info, InfoEventDelivery, InfoUserLookup, Limits
from muster_bridge.problems import encode_json, validation_problems
from muster_bridge.store import MemoryStore, Store

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Bot", "Config", "ConfigError", "PushTarget"]

MAX_BODY_LENGTH_CAP = 100_000
PUSH_KEY_PREFIX = "whsec_"
"""Opens every push secret; the signing key follows it, in base64."""

_MESSENGER_METHODS = ("post_message", "edit_message", "reply_in_thread", "check_channel", "answer_event")
_SPEC_MISMATCH = "the info this configuration makes does not match api/bridge.yaml: "


@dataclass(frozen=True, slots=True, kw_only=True)
class PushTarget:
    """Where the events of a bot are pushed, as the Bridge Connection in Muster shows it."""

    url: str
    """The callback address of the Bridge Connection, with the http or https scheme and a host."""
    secret: str = field(repr=False)
    """The push secret as the Bridge Connection shows it: whsec_ and the base64 of the signing key."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Bot:
    """One bot of the bridge; each is a Bridge Connection in Muster."""

    name: str
    """Names the bot in the messenger calls and the reports, such as "main"."""
    token: str = field(repr=False)
    """The token Muster sends for this bot, as saved on its Bridge Connection."""
    push: PushTarget | None = None
    """Where the bot's events are pushed; None for none."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """A bridge: what it tells Muster in info, its bots and where it keeps its state."""

    name: str
    """The bridge's name, as info shows it: 1 to 100 characters."""
    version: str
    """The bridge's version, as info shows it: 1 to 100 characters."""
    messenger_name: str
    """The messenger's name as people know it: 1 to 100 characters."""
    bots: Sequence[Bot]
    """The bots the bridge serves, at least one."""
    limits: Limits
    """The bridge's limits; max_body_length from 500 to 100000, edit_max_age_seconds None when edits never expire."""
    capabilities: Capabilities = field(default_factory=Capabilities)
    """The optional features; a member left None is a feature the bridge does not offer."""
    user_lookup: InfoUserLookup | Literal["email", "login", "username", "user_id", "none"] = "none"
    """What findUsers searches by."""
    event_delivery: Sequence[InfoEventDelivery | Literal["poll", "push"]] | None = None
    """The event delivery modes the bridge offers; None is both, poll and push."""
    link_url_template: str | None = None
    """An http or https link with {token}, which opens the bot with an Account link token."""
    store: Store | None = None
    """Keeps the Idempotency-Keys and the events; None is a MemoryStore."""
    logger: logging.Logger | None = None
    """Receives the library's logs; None is the logger "muster_bridge"."""


class ConfigError(ValueError):
    """A configuration the library refuses, with every problem it found."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


class Clock(Protocol):
    """Where the library reads the time and waits: the system's, or a test's."""

    def now(self) -> datetime:
        """Return the time, aware and in UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Return after seconds."""
        ...


class SystemClock:
    """The system's clock."""

    def now(self) -> datetime:
        """Return the time, aware and in UTC."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Return after seconds."""
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class ParsedTarget:
    """A PushTarget that passed its checks, holding the HMAC key the secret encodes."""

    url: str
    key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Prepared:
    """A configuration that passed its checks, with its defaults filled in."""

    info: Info
    info_body: bytes
    bots: tuple[Bot, ...]
    targets: dict[str, ParsedTarget | None]
    store: Store
    logger: logging.Logger
    poll_offered: bool
    push_offered: bool


def parse_push_target(target: PushTarget) -> tuple[ParsedTarget | None, list[str]]:
    """Return the URL and key a pusher uses, or the faults of the target; a fault never quotes the secret."""
    faults = []
    if not _callback_url_usable(target.url):
        faults.append("the url needs the http or https scheme, a host and no user information")
    key = _secret_key(target.secret)
    if key is None:
        faults.append("the secret needs the whsec_ prefix and then a non-empty key in standard base64")
    if faults or key is None:
        return None, faults
    return ParsedTarget(target.url, key), []


def _callback_url_usable(raw: str) -> bool:
    """Report whether raw is a URL a pusher can post to: http or https, with a host and without credentials."""
    try:
        parts = urlsplit(raw)
        _ = parts.port  # reading the port checks it
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https") and bool(parts.hostname) and parts.username is None and parts.password is None
    )


def _secret_key(secret: str) -> bytes | None:
    """Return the HMAC key of a push secret: the bytes of the base64 text after the whsec_ prefix.

    Standard Webhooks asks for at least 24 random bytes; any non-empty key is taken, as Muster takes it.
    """
    if not secret.startswith(PUSH_KEY_PREFIX):
        return None
    try:
        key = base64.b64decode(secret.removeprefix(PUSH_KEY_PREFIX), validate=True)
    except ValueError:  # binascii.Error, or text that is not ASCII
        return None
    return key or None


def to_json_object(model: BaseModel) -> dict[str, Any]:
    """Return the JSON object of a model as the specification has it.

    An optional member that is None is left out, as the specification makes such members absent rather than null; a
    required member that is None stays null.
    """
    return _prune(model, model.model_dump(mode="json", by_alias=True, warnings=False))


def _prune(model: BaseModel, data: dict[str, Any]) -> dict[str, Any]:
    for name, info in type(model).model_fields.items():
        key = info.alias or name
        if key not in data:
            continue
        value = getattr(model, name, None)
        if value is None and not info.is_required():
            del data[key]
        elif isinstance(value, BaseModel):
            data[key] = _prune(value, data[key])
        elif isinstance(value, list) and all(isinstance(item, BaseModel) for item in value):
            data[key] = [_prune(item, item_data) for item, item_data in zip(value, data[key], strict=True)]
    return data


def prepare(config: Config, messenger: object) -> Prepared:
    """Check the configuration with its messenger and return it prepared; raise ConfigError naming every problem."""
    problems = _check_messenger(config, messenger) + _check_bots(config)
    types = [
        f"{name} is not a muster_bridge.{model.__name__}"
        for name, value, model in (
            ("capabilities", config.capabilities, Capabilities),
            ("limits", config.limits, Limits),
        )
        if not isinstance(value, model)
    ]
    info: Info | None = None
    info_body = b""
    if types:
        problems += types
    else:
        info_body, info, info_problems = _build_info(config)
        problems += info_problems
    if problems or info is None:
        raise ConfigError(problems)
    targets: dict[str, ParsedTarget | None] = {}
    for bot in config.bots:
        targets[bot.name] = parse_push_target(bot.push)[0] if bot.push is not None else None
    return Prepared(
        info=info,
        info_body=info_body,
        bots=tuple(config.bots),
        targets=targets,
        store=config.store if config.store is not None else MemoryStore(),
        logger=config.logger if config.logger is not None else logging.getLogger("muster_bridge"),
        poll_offered="poll" in info.event_delivery,
        push_offered="push" in info.event_delivery,
    )


def _has_coroutine(messenger: object, name: str) -> bool:
    return inspect.iscoroutinefunction(getattr(messenger, name, None))


def _check_messenger(config: Config, messenger: object) -> list[str]:
    if messenger is None:
        return ["the messenger is None"]
    problems = []
    for name in _MESSENGER_METHODS:
        if not hasattr(messenger, name):
            problems.append(f"the messenger lacks {name}")
        elif not _has_coroutine(messenger, name):
            problems.append(
                f"{name} of the messenger is not a coroutine function: define it with async def, or mark a wrapper "
                "that returns a coroutine with inspect.markcoroutinefunction"
            )
    if config.user_lookup != "none" and not _has_coroutine(messenger, "find_user"):
        problems.append(f'user_lookup "{config.user_lookup}" needs a messenger that implements UserFinder (find_user)')
    # getattr, because capabilities is checked to be a Capabilities only after this.
    if getattr(config.capabilities, "direct_messages", None) is not None and not _has_coroutine(
        messenger, "send_direct_message"
    ):
        problems.append(
            "capabilities.direct_messages needs a messenger that implements DirectMessenger (send_direct_message)"
        )
    if getattr(config.capabilities, "delete", None) is not None and not _has_coroutine(messenger, "delete_message"):
        problems.append("capabilities.delete needs a messenger that implements Deleter (delete_message)")
    return problems


def _check_bots(config: Config) -> list[str]:
    if not config.bots:
        return ["bots is empty: a bridge needs at least one bot"]
    problems = []
    push_offered = config.event_delivery is None or "push" in config.event_delivery
    names: dict[str, int] = {}
    tokens: dict[str, int] = {}
    for i, bot in enumerate(config.bots):
        if not bot.name:
            problems.append(f"bots[{i}].name is empty")
        elif bot.name.strip() != bot.name:
            problems.append(f"bots[{i}].name has white space around it")
        elif bot.name in names:
            problems.append(f'bots[{i}].name "{bot.name}" is the name of bots[{names[bot.name]}] too')
        else:
            names[bot.name] = i
        if not bot.token:
            problems.append(f"bots[{i}].token is empty")
        elif bot.token.strip() != bot.token:
            problems.append(f"bots[{i}].token has white space around it")
        elif bot.token in tokens:
            problems.append(f"bots[{i}].token is the token of bots[{tokens[bot.token]}] too")
        else:
            tokens[bot.token] = i
        if bot.push is None:
            continue
        if not push_offered:
            problems.append(f"bots[{i}].push is set, but event_delivery does not offer push")
        _, faults = parse_push_target(bot.push)
        if faults:
            problems.append(f"bots[{i}].push: {'; '.join(faults)}")
    return problems


def _build_info(config: Config) -> tuple[bytes, Info | None, list[str]]:
    """Return info as served, and as checked against the specification, or the problems that stop it."""
    problems = []
    if config.limits.max_body_length > MAX_BODY_LENGTH_CAP:
        problems.append(
            f"limits.max_body_length is above {MAX_BODY_LENGTH_CAP}, the most this library accepts",
        )
    info: dict[str, Any] = {
        "protocol_version": "1",
        "bridge": {"name": config.name, "version": config.version},
        "messenger": {"name": config.messenger_name},
        "capabilities": to_json_object(config.capabilities),
        "limits": to_json_object(config.limits),
        "user_lookup": config.user_lookup,
        "event_delivery": list(config.event_delivery) if config.event_delivery is not None else ["poll", "push"],
    }
    if config.link_url_template is not None:
        info["link_url_template"] = config.link_url_template
    try:
        body = encode_json(info)
    except (TypeError, ValueError) as error:
        return b"", None, [*problems, f"the info this configuration makes cannot be encoded: {error}"]
    try:
        checked = Info.model_validate_json(body, strict=True)
    except ValidationError as error:
        return body, None, [*problems, *(_SPEC_MISMATCH + problem for problem in validation_problems(error))]
    for pointer, items in (
        ("/event_delivery", checked.event_delivery),
        ("/capabilities/buttons/styles", checked.capabilities.buttons.styles if checked.capabilities.buttons else []),
        ("/capabilities/private_answers", checked.capabilities.private_answers or []),
    ):
        if len(set(items)) != len(items):
            problems.append(f"{_SPEC_MISMATCH}{pointer}: the items are not unique")
    return body, checked, problems
