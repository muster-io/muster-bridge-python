# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""Serve Muster's Bridge protocol, so that a bridge to a messenger is only the calls to that messenger.

BridgeApp is an ASGI application that serves api/bridge.yaml for the bots of a Config — tokens, validation,
idempotency, answers, the events the bridge reports and their pushes — and calls the bridge's Messenger. See the README
for a quick start.
"""

from muster_bridge.app import BridgeApp, ClosedError
from muster_bridge.config import Bot, Config, ConfigError, PushTarget
from muster_bridge.events import InvalidEventError, NotOfferedError, UnknownBotError
from muster_bridge.mentions import replace_mentions
from muster_bridge.messenger import (
    Deleter,
    DirectMessenger,
    EventType,
    Messenger,
    ReportedEvent,
    UserFinder,
    idempotency_key,
)
from muster_bridge.models import (
    AnswerShownShownAs,
    Button,
    ButtonStyle,
    Capabilities,
    CapabilitiesButtons,
    CapabilitiesDelete,
    CapabilitiesDirectMessages,
    CapabilitiesPrivateAnswer,
    CapabilitiesThreadMessages,
    DirectMessageInput,
    DirectMessageSent,
    InfoEventDelivery,
    InfoUserLookup,
    Limits,
    Link,
    Mention,
    MessageContent,
    MessageEdited,
    MessageInput,
    MessageKind,
    MessagePosted,
    Person,
    Status,
    ThreadReplyInput,
    ThreadReplyPosted,
)
from muster_bridge.problems import BridgeError, Forbidden, Gone, Invalid, RateLimited, Transient
from muster_bridge.store import KeyClaim, KeyState, MemoryStore, Store, StoredAnswer, StoredEvent, UnknownCursorError

__version__ = "0.1.0"

__all__ = [
    "AnswerShownShownAs",
    "Bot",
    "BridgeApp",
    "BridgeError",
    "Button",
    "ButtonStyle",
    "Capabilities",
    "CapabilitiesButtons",
    "CapabilitiesDelete",
    "CapabilitiesDirectMessages",
    "CapabilitiesPrivateAnswer",
    "CapabilitiesThreadMessages",
    "ClosedError",
    "Config",
    "ConfigError",
    "Deleter",
    "DirectMessageInput",
    "DirectMessageSent",
    "DirectMessenger",
    "EventType",
    "Forbidden",
    "Gone",
    "InfoEventDelivery",
    "InfoUserLookup",
    "Invalid",
    "InvalidEventError",
    "KeyClaim",
    "KeyState",
    "Limits",
    "Link",
    "MemoryStore",
    "Mention",
    "MessageContent",
    "MessageEdited",
    "MessageInput",
    "MessageKind",
    "MessagePosted",
    "Messenger",
    "NotOfferedError",
    "Person",
    "PushTarget",
    "RateLimited",
    "ReportedEvent",
    "Status",
    "Store",
    "StoredAnswer",
    "StoredEvent",
    "ThreadReplyInput",
    "ThreadReplyPosted",
    "Transient",
    "UnknownBotError",
    "UnknownCursorError",
    "UserFinder",
    "idempotency_key",
    "replace_mentions",
]
