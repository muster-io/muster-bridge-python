# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The Bridge protocol as an ASGI application on Starlette."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from http import HTTPStatus
from typing import TYPE_CHECKING, TypeVar, cast
from urllib.parse import quote, unquote

from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Router

from muster_bridge.config import PUSH_KEY_PREFIX, ConfigError, SystemClock, parse_push_target, prepare, to_json_object
from muster_bridge.events import (
    MAX_THREAD_TEXT,
    Events,
    NotOfferedError,
    StoredEventError,
    UnknownBotError,
    WaitError,
    account_link_token,
    stop,
)
from muster_bridge.messenger import bind_idempotency_key, unbind_idempotency_key
from muster_bridge.models import (
    AnswerInput,
    AnswerShown,
    ChannelCheck,
    CheckChannelParameters,
    DeleteMessageParameters,
    DirectMessageInput,
    DirectMessageSent,
    EditMessageParameters,
    EventBatch,
    FindUsersParameters,
    MessageContent,
    MessageEdited,
    MessageInput,
    MessagePosted,
    Person,
    PersonList,
    PollEventsParameters,
    ReplyInThreadParameters,
    ThreadReplyInput,
    ThreadReplyPosted,
)
from muster_bridge.problems import (
    DETAIL_MESSENGER,
    DETAIL_STORE,
    DETAIL_WAIT,
    MAX_DETAIL,
    NO_STORE,
    BridgeError,
    ErrorClassName,
    Gone,
    RateLimited,
    Redactor,
    check_json,
    cut,
    describe,
    encode_json,
    json_pointer,
    map_class,
    problem_response,
    unauthorized_response,
    validation_problem,
)
from muster_bridge.push import Pushers
from muster_bridge.store import KeyClaim, KeyState, StoredAnswer, UnknownCursorError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    import httpx
    from starlette.types import Receive, Scope, Send

    from muster_bridge.config import Clock, Config, PushTarget
    from muster_bridge.messenger import Deleter, DirectMessenger, EventType, Messenger, UserFinder
    from muster_bridge.models import Button, CapabilitiesButtons

__all__ = ["BridgeApp", "ClosedError"]

MAX_REQUEST_BYTES = 1 << 20
"""The library's cap on a request body, which leaves room for any max_body_length it accepts."""
MAX_CHANNEL_NAME = 200
KEY_CLAIM_TTL = timedelta(minutes=2)
KEY_RETENTION = timedelta(hours=24)

_IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,255}")
_MISMATCH = "The request does not match api/bridge.yaml: "

_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Operation:
    """One operation of the protocol: how it is routed and what it answers."""

    op_id: str
    method: str
    template: str
    """The path of the operation in api/bridge.yaml."""
    status: int
    answer_model: type[BaseModel] | None
    """The model of the answer; None for an answer without a body."""
    idempotent: bool = False
    segments: tuple[str | None, ...] = field(init=False)
    """The segments of the path after its leading slash; None for the path parameter."""
    param: str | None = field(init=False)
    """The name of the path parameter, if the path has one."""

    def __post_init__(self) -> None:
        """Split the template into segments."""
        segments = tuple(self.template.removeprefix("/").split("/"))
        names = [s[1:-1] for s in segments if s.startswith("{")]
        object.__setattr__(self, "segments", tuple(None if s.startswith("{") else s for s in segments))
        object.__setattr__(self, "param", names[0] if names else None)


OPERATIONS = (
    Operation("getInfo", "GET", "/v1/info", HTTPStatus.OK, None),
    Operation("postMessage", "POST", "/v1/messages", HTTPStatus.CREATED, MessagePosted, idempotent=True),
    Operation("editMessage", "PATCH", "/v1/messages/{message_id}", HTTPStatus.OK, MessageEdited),
    Operation("deleteMessage", "DELETE", "/v1/messages/{message_id}", HTTPStatus.NO_CONTENT, None),
    Operation(
        "replyInThread",
        "POST",
        "/v1/messages/{message_id}/thread-replies",
        HTTPStatus.CREATED,
        ThreadReplyPosted,
        idempotent=True,
    ),
    Operation("checkChannel", "POST", "/v1/channels/{channel_id}/check", HTTPStatus.OK, ChannelCheck),
    Operation("findUsers", "GET", "/v1/users", HTTPStatus.OK, PersonList),
    Operation(
        "sendDirectMessage", "POST", "/v1/direct-messages", HTTPStatus.CREATED, DirectMessageSent, idempotent=True
    ),
    Operation("answerEvent", "POST", "/v1/answers", HTTPStatus.OK, AnswerShown, idempotent=True),
    Operation("pollEvents", "GET", "/v1/events", HTTPStatus.OK, EventBatch),
)
"""The operations of api/bridge.yaml that the library serves."""


class ClosedError(RuntimeError):
    """set_push after aclose."""


class _Refusal(Exception):  # noqa: N818  (a refusal is an answer of the library, not an error)
    """An answer of the library itself, sent as it is."""

    def __init__(self, error_class: ErrorClassName, detail: str) -> None:
        super().__init__(detail)
        self.error_class: ErrorClassName = error_class
        self.detail = detail


class _AnswerFault(Exception):  # noqa: N818  (the fault of the bridge's answer, not an error of the library)
    """An answer of the messenger that does not match api/bridge.yaml."""


class _StoreFailure(Exception):  # noqa: N818  (the fault of the bridge's store, not an error of the library)
    """A failure of the store while a request runs, which it answers as transient."""


@dataclass(slots=True)
class _Call:
    """One request of an operation that calls the messenger."""

    op: Operation
    bot: str
    request: Request
    value: str | None
    """The decoded path parameter."""
    extras: list[str] = field(default_factory=list)
    """Secrets the request handles besides the tokens and push secrets, redacted from its answer and logs."""
    key: str = ""
    claim_id: str = ""
    claimed: bool = False
    saved: bool = False

    def params(self, model: type[_M]) -> _M:
        """Return the parameters of the request, validated with the model, or refuse them."""
        data: dict[str, object] = {}
        for name in model.model_fields:
            if name == self.op.param:
                data[name] = self.value
            elif values := self.request.query_params.getlist(name):
                data[name] = values[0]
        try:
            return model.model_validate(data)
        except ValidationError as error:
            first = error.errors(include_url=False, include_input=False, include_context=False)[0]
            name = str(first["loc"][0]) if first["loc"] else ""
            where = "path" if name == self.op.param else "query"
            raise _Refusal("invalid", f"{_MISMATCH}parameter {name} in {where}: {first['msg']}.") from None

    async def body(self, model: type[_M]) -> _M:
        """Return the body of the request, at most 1 MiB, validated with the model, or refuse it."""
        data = bytearray()
        too_long = False
        try:
            async for chunk in self.request.stream():
                data += chunk
                if len(data) > MAX_REQUEST_BYTES:
                    too_long = True
                    break
        except Exception:  # a client that left, or a server that could not read
            raise _Refusal("invalid", "Reading the request body failed.") from None
        if too_long:
            raise _Refusal("invalid", "The request body exceeds 1 MiB, the most this bridge reads.")
        if not data:
            raise _Refusal("invalid", f"{_MISMATCH}body: value is required but missing.")
        if self.request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            raise _Refusal("invalid", f"{_MISMATCH}body: header Content-Type is not application/json.")
        try:
            return model.model_validate_json(bytes(data), strict=True)
        except ValidationError as error:
            first = error.errors(include_url=False, include_input=False, include_context=False)[0]
            pointer = json_pointer(first["loc"])
            where = f"body {pointer}" if pointer else "body"
            raise _Refusal("invalid", f"{_MISMATCH}{where}: {first['msg']}.") from None


def check_buttons(buttons: Sequence[Button], capabilities: CapabilitiesButtons | None) -> str | None:
    """Return why the buttons are beyond what the bridge offers, or None when they are not."""
    if not buttons:
        return None
    if capabilities is None:
        return "This bridge does not offer buttons (capabilities.buttons)."
    if len(buttons) > capabilities.max_per_message:
        return (
            f"The message has {len(buttons)} buttons; this bridge takes at most {capabilities.max_per_message} "
            "(capabilities.buttons.max_per_message)."
        )
    for i, button in enumerate(buttons, start=1):
        if (n := len(button.data.encode())) > capabilities.data_max_bytes:
            return (
                f"Button {i} carries {n} bytes of data; this bridge takes at most {capabilities.data_max_bytes} "
                "(capabilities.buttons.data_max_bytes)."
            )
    return None


def _json_response(status: int, body: bytes) -> Response:
    return Response(body, status, NO_STORE, media_type="application/json")


class BridgeApp:
    """The Bridge protocol as an ASGI application on Starlette.

    It serves every operation of api/bridge.yaml under /v1/ for the bots of its Config and calls the messenger: serve
    it with an ASGI server such as uvicorn, or mount it under a path of a Starlette or FastAPI application. The bridge
    reports what happens in the messenger with press, thread_message, message_deleted and direct_message; the events
    wait for pollEvents and are pushed to the bots' push targets. It raises ConfigError for a configuration it refuses,
    naming every problem. It runs on the event loop that serves it, and on the next one when that loop ends, as with
    Starlette's TestClient without `with`: the loops take turns, never run at once in two threads. The
    leading-underscore parameters are for tests.
    """

    def __init__(
        self,
        config: Config,
        messenger: Messenger,
        *,
        _clock: Clock | None = None,
        _random: Callable[[int], bytes] | None = None,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        prepared = prepare(config, messenger)
        self._messenger = messenger
        self._info = prepared.info
        self._info_body = prepared.info_body
        self._store = prepared.store
        self._log = prepared.logger
        self._clock: Clock = _clock or SystemClock()
        self._push_offered = prepared.push_offered
        self._poll_offered = prepared.poll_offered
        # An HMAC rather than a bare hash: digests of one length make the comparison independent of the token's.
        self._hmac_key = os.urandom(32)
        self._digests = tuple((bot.name, self._digest(bot.token.encode())) for bot in prepared.bots)
        self._redact = Redactor()
        for bot in prepared.bots:
            self._redact.add(bot.token)
            if bot.push is not None:
                self._redact.add(bot.push.secret, bot.push.secret.removeprefix(PUSH_KEY_PREFIX))
        self._runners: dict[str, Callable[[_Call], Awaitable[object]]] = {
            "postMessage": self._post_message,
            "editMessage": self._edit_message,
            "deleteMessage": self._delete_message,
            "replyInThread": self._reply_in_thread,
            "checkChannel": self._check_channel,
            "findUsers": self._find_users,
            "sendDirectMessage": self._send_direct_message,
            "answerEvent": self._answer_event,
        }
        self._served = tuple(op for op in OPERATIONS if self._offers(op))
        self._bots = frozenset(bot.name for bot in prepared.bots)
        self._events = Events(self._bots, self._store, self._clock, _random or os.urandom)
        self._pushers = Pushers(
            targets=prepared.targets,
            events=self._events,
            store=self._store,
            clock=self._clock,
            logger=self._log,
            redact=self._redact,
            transport=_transport,
        )
        self._closed = False
        self._lifespan_router = Router(lifespan=self.lifespan)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve an ASGI connection: HTTP requests and the lifespan; a WebSocket is refused."""
        if scope["type"] == "http":
            response = await self._handle(Request(scope, receive))
            if response is not None:
                await response(scope, receive, send)
        elif scope["type"] == "lifespan":
            await self._lifespan_router(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})

    async def press(
        self,
        bot: str,
        *,
        message_id: str,
        user: Person,
        data: str,
        update_id: str | None = None,
        at: datetime | None = None,
        chat: str | None = None,
    ) -> None:
        """Report a press of a button of a message of the bot, already confirmed to the messenger.

        It returns once the event is queued; pushes happen in the background, so the bridge confirms the press to its
        messenger independently of Muster. message_id is the message the button belongs to and data the button's data,
        unchanged. update_id is the messenger's own id of the update: an update reported twice with it is queued once.
        at is when it happened, aware; None is now. chat is anything the bridge needs to answer the person, such as a
        chat or callback id: it comes back in ReportedEvent.chat and is never sent to Muster.

        Raises UnknownBotError, NotOfferedError without capabilities.buttons, InvalidEventError for an event that would
        not match api/bridge.yaml, and what the store raises.
        """
        self._check_bot(bot)
        if self._info.capabilities.buttons is None:
            raise NotOfferedError("press needs capabilities.buttons")
        members = {"message_id": message_id, "user": user, "data": data}
        await self._report(bot, "press", members, update_id, at, chat)

    async def thread_message(
        self,
        bot: str,
        *,
        message_id: str,
        user: Person,
        text: str,
        thread_ref: str | None = None,
        update_id: str | None = None,
        at: datetime | None = None,
        chat: str | None = None,
    ) -> None:
        """Report what a person wrote in the Thread of a message of the bot; the bot's own replies are not reported.

        message_id is the bot's message that owns the Thread, as replyInThread names it, never the id of a reply.
        thread_ref is the Thread's thread_ref, None or "" for none. text is the message as plain text, mentions of the
        bot removed; it is cut to 4000 characters. The rest is as for press; the bridge must offer
        capabilities.thread_messages.
        """
        self._check_bot(bot)
        if self._info.capabilities.thread_messages is None:
            raise NotOfferedError("thread_message needs capabilities.thread_messages")
        members = {
            "message_id": message_id,
            "thread_ref": thread_ref or None,
            "user": user,
            "text": text[:MAX_THREAD_TEXT],
        }
        await self._report(bot, "thread_message", members, update_id, at, chat)

    async def message_deleted(
        self, bot: str, *, message_id: str, update_id: str | None = None, at: datetime | None = None
    ) -> None:
        """Report the deletion of a message of the bot in the messenger.

        The rest is as for press; the bridge must offer capabilities.deletion_events.
        """
        self._check_bot(bot)
        if self._info.capabilities.deletion_events is not True:
            raise NotOfferedError("message_deleted needs capabilities.deletion_events")
        await self._report(bot, "message_deleted", {"message_id": message_id}, update_id, at, None)

    async def direct_message(
        self,
        bot: str,
        *,
        user: Person,
        text: str,
        update_id: str | None = None,
        at: datetime | None = None,
        chat: str | None = None,
    ) -> None:
        """Report a direct message from a person to the bot.

        A message whose whole text, trimmed and after an optional "/start ", is 43 characters of base64url is an
        Account link token and becomes an account_link event; any other is ignored. chat is the direct chat, so that
        answer_event answers there. Every bridge takes Account link tokens; the rest is as for press.
        """
        self._check_bot(bot)
        token = account_link_token(text)
        if token is not None:
            await self._report(bot, "account_link", {"token": token, "user": user}, update_id, at, chat)

    def set_push(self, bot: str, target: PushTarget | None) -> None:
        """Push the events of the bot to target from now on; None stops pushing.

        A pusher that runs continues where it was, towards the new target; one that this starts begins with the oldest
        event kept. While the bridge is served, call it from the thread of its event loop, as asyncio requires; from
        another thread, hand it over with loop.call_soon_threadsafe(bridge.set_push, bot, target). Raises
        UnknownBotError, ClosedError after aclose, and ConfigError for a target the bridge refuses or when
        event_delivery does not offer push.
        """
        if self._closed:
            raise ClosedError("the bridge is closed")
        self._check_bot(bot)
        parsed = None
        if target is not None:
            if not self._push_offered:
                raise ConfigError(["event_delivery does not offer push"])
            parsed, faults = parse_push_target(target)
            if faults:
                raise ConfigError([f"the push target: {'; '.join(faults)}"])
            self._redact.add(target.secret, target.secret.removeprefix(PUSH_KEY_PREFIX))
        self._pushers.set_target(bot, parsed)

    async def aclose(self) -> None:
        """Stop the pushers and close their HTTP client; calling it again does nothing.

        The application and the reports keep working, and the events stay queued for polling; none is pushed any more.
        """
        self._closed = True
        await self._pushers.aclose()

    @contextlib.asynccontextmanager
    async def lifespan(self, app: object = None) -> AsyncIterator[None]:
        """Run the bridge for the lifespan of a host application: pass it as the host's lifespan.

        It starts the pushers of the bots with a push target and stops them on shutdown. Starlette runs no lifespan of
        a mounted application, so a host that mounts the bridge passes this, or enters it from a lifespan of its own.
        Served on its own, the bridge runs it from the ASGI lifespan. A BridgeApp has one lifespan: its end is aclose,
        after which the bridge pushes no more, so a program or a test that runs a lifespan again makes a new BridgeApp.
        """
        self._pushers.start_all()
        try:
            yield
        finally:
            await self.aclose()

    def _check_bot(self, bot: str) -> None:
        if bot not in self._bots:
            raise UnknownBotError(f"no bot is named {bot!r}")

    async def _report(
        self,
        bot: str,
        event_type: EventType,
        members: Mapping[str, object],
        update_id: str | None,
        at: datetime | None,
        chat: str | None,
    ) -> None:
        if await self._events.report(bot, event_type, members, update_id=update_id, at=at, chat=chat):
            self._pushers.ensure(bot)

    def _offers(self, op: Operation) -> bool:
        capabilities = self._info.capabilities
        if op.op_id == "deleteMessage":
            return capabilities.delete is not None
        if op.op_id == "findUsers":
            return self._info.user_lookup != "none"
        if op.op_id == "sendDirectMessage":
            return capabilities.direct_messages is not None
        if op.op_id == "pollEvents":
            return self._poll_offered
        return True

    def _digest(self, token: bytes) -> bytes:
        return hmac.new(self._hmac_key, token, sha256).digest()

    async def _handle(self, request: Request) -> Response | None:
        """Answer the request; None for a long poll whose client has left, which gets no answer."""
        bot = self._bot_of(request)
        if bot is None:
            return unauthorized_response()
        routed = self._route(request)
        if routed is None:
            return problem_response(
                "invalid", "This bridge does not serve this operation.", status=HTTPStatus.NOT_FOUND
            )
        op, value = routed
        if op.op_id == "getInfo":
            return _json_response(HTTPStatus.OK, self._info_body)
        if op.op_id == "pollEvents":
            return await self._poll(_Call(op, bot, request, value))
        return await self._serve(_Call(op, bot, request, value))

    def _bot_of(self, request: Request) -> str | None:
        """Return the bot whose token the request presents, comparing its digest with every bot's in constant time."""
        scheme, _, token = request.headers.get("authorization", "").encode("latin-1").partition(b" ")
        token = token.lstrip(b" ")
        digest = self._digest(token)
        found = None
        for name, expected in self._digests:
            if hmac.compare_digest(digest, expected):
                found = name
        if scheme.lower() != b"bearer" or not token:
            return None
        return found

    def _route(self, request: Request) -> tuple[Operation, str | None] | None:
        """Return the operation of the request and its decoded path parameter, matched on the raw path."""
        scope = request.scope
        raw = scope.get("raw_path")
        path = raw.partition(b"?")[0].decode("latin-1") if raw is not None else quote(scope["path"])
        first, *segments = _below(path, scope.get("root_path", "")).split("/")
        if first:
            return None
        for op in self._served:
            if op.method != request.method or len(op.segments) != len(segments):
                continue
            values = _match(op.segments, segments)
            if values is not None:
                return op, values[0] if values else None
        return None

    async def _serve(self, call: _Call) -> Response:
        if call.op.idempotent:
            refused = await self._claim(call)
            if refused is not None:
                return refused
        try:
            return await self._run(call)
        finally:
            if call.claimed and not call.saved:
                await self._release(call)

    async def _poll(self, call: _Call) -> Response | None:
        """Answer pollEvents: the events after the cursor, at once or within wait; None when the client has left."""
        try:
            params = call.params(PollEventsParameters)
        except _Refusal as refusal:
            return self._problem(call, refusal.error_class, refusal.detail)
        left = asyncio.ensure_future(_disconnect(call.request.receive))
        try:
            polled = await self._events.poll(call.bot, params.cursor or "", params.wait, left)
        except UnknownCursorError:
            return self._problem(call, "invalid", "The cursor is not known to this bridge; poll again without one.")
        except WaitError as error:
            self._log.error(
                "the wait of a long poll failed: operation=pollEvents bot=%s error=%s",
                call.bot,
                self._redact(str(error), *call.extras),
            )
            return self._problem(call, "transient", DETAIL_WAIT)
        except Exception as error:
            return self._store_failed(call, describe(error))
        finally:
            await stop(left)
        if polled is None:
            return None
        bodies, next_cursor = polled
        body = b'{"events":[' + b",".join(bodies) + b'],"next_cursor":' + encode_json(next_cursor) + b"}"
        return _json_response(HTTPStatus.OK, body)

    async def _run(self, call: _Call) -> Response:
        """Validate the request, call the messenger and answer: its answer, or its error as a problem."""
        token = bind_idempotency_key(call.key) if call.claimed else None
        try:
            result = await self._runners[call.op.op_id](call)
        except _Refusal as refusal:
            return self._problem(call, refusal.error_class, refusal.detail)
        except _AnswerFault as fault:
            return self._answer_fault(call, str(fault))
        except _StoreFailure as failure:
            return self._store_failed(call, str(failure))
        except BridgeError as error:
            return self._bridge_error(call, error)
        except Exception as error:
            return self._failure(call, error)
        finally:
            if token is not None:
                unbind_idempotency_key(token)
        return await self._answer(call, result)

    async def _claim(self, call: _Call) -> Response | None:
        """Claim the request's Idempotency-Key, or answer: the first result again, or transient while it runs."""
        key = call.request.headers.get("idempotency-key")
        if key is None or not _IDEMPOTENCY_KEY.fullmatch(key):
            return self._problem(
                call, "invalid", "Send an Idempotency-Key header of 1 to 255 visible ASCII characters (no spaces)."
            )
        now = self._clock.now()
        try:
            claim: object = await self._store.claim_key(call.bot, key, now, now + KEY_CLAIM_TTL)
        except Exception as error:
            return self._store_failed(call, describe(error))
        # A Store of the bridge's own may answer anything: a KeyClaim, whose state is checked against every known one.
        if not isinstance(claim, KeyClaim):
            return self._store_failed(call, f"claim_key returned a {type(claim).__name__}, not a KeyClaim")
        state: object = claim.state
        if state is KeyState.ANSWERED:
            return self._replay(call, claim.answer)
        if state is KeyState.RUNNING:
            return self._problem(call, "transient", "A request with this Idempotency-Key is still running.")
        if state is not KeyState.CLAIMED:
            return self._store_failed(call, f"claim_key returned the unknown state {state!r}")
        call.key, call.claim_id, call.claimed = key, claim.claim_id, True
        return None

    def _replay(self, call: _Call, first: StoredAnswer | None) -> Response:
        """Answer the first answer of the key once more, flagged as replayed.

        The request's own body is never read: a repeated key gets the first answer whatever it carries. A stored
        answer that no longer fits the operation is a fault of the store.
        """
        body, fault = _replayed(call.op, first)
        if fault:
            return self._store_failed(
                call, f"the first answer stored for the Idempotency-Key cannot be sent again: {fault}"
            )
        return _json_response(call.op.status, body)

    async def _release(self, call: _Call) -> None:
        """Free the claimed key, so that a retry runs again; the store frees it only while this claim holds it."""
        try:
            await self._store.release_key(call.bot, call.key, call.claim_id)
        except Exception as error:
            self._log.error(
                "cannot release an Idempotency-Key: operation=%s bot=%s error=%s",
                call.op.op_id,
                call.bot,
                self._redact(describe(error)),
            )

    async def _answer(self, call: _Call, result: object) -> Response:
        """Check the messenger's answer against api/bridge.yaml, store it for the Idempotency-Key and send it."""
        model = call.op.answer_model
        if model is None:
            return Response(status_code=HTTPStatus.NO_CONTENT, headers=NO_STORE)
        if not isinstance(result, model):
            return self._answer_fault(call, f"it is not a {model.__name__}")
        try:
            answer = to_json_object(result)
            if "replayed" in model.model_fields:
                answer["replayed"] = False
            body = encode_json(answer)
        except Exception:  # an answer built around its model's checks, which pydantic or json cannot encode
            return self._answer_fault(call, "it cannot be encoded")
        if problem := check_json(model, body):
            return self._answer_fault(call, problem)
        if call.claimed:
            until = self._clock.now() + KEY_RETENTION
            try:
                await self._store.save_key(call.bot, call.key, call.claim_id, StoredAnswer(call.op.status, body), until)
            except Exception as error:
                self._log.error(
                    "cannot store the answer of an Idempotency-Key: operation=%s bot=%s error=%s",
                    call.op.op_id,
                    call.bot,
                    self._redact(describe(error)),
                )
            else:
                call.saved = True
        return _json_response(call.op.status, body)

    def _problem(
        self,
        call: _Call,
        error_class: ErrorClassName,
        detail: str,
        *,
        retry_after: float = 0.0,
        scope: str = "channel",
        edit_window_closed: bool = False,
    ) -> Response:
        """Answer a problem with the detail redacted and cut."""
        return problem_response(
            error_class,
            cut(self._redact(detail, *call.extras), MAX_DETAIL),
            retry_after=retry_after,
            scope=scope,
            edit_window_closed=edit_window_closed,
        )

    def _bridge_error(self, call: _Call, error: BridgeError) -> Response:
        """Answer a BridgeError in the class the operation declares; a class it cannot declare is transient."""
        op = call.op.op_id
        mapped, mistake = map_class(op, error.error_class)
        if mistake:
            self._log.error(
                "the messenger answered a class the operation does not declare: operation=%s bot=%s class=%s",
                op,
                call.bot,
                self._redact(str(error.error_class)),
            )
            return self._problem(call, "transient", DETAIL_MESSENGER)
        edit_window_closed = isinstance(error, Gone) and error.edit_window_closed
        if edit_window_closed and mapped != "gone":
            self._log.error(
                "the messenger gave a reason the protocol does not define for this answer; it is not sent: "
                "operation=%s bot=%s class=%s reason=edit_window_closed",
                op,
                call.bot,
                mapped,
            )
        return self._problem(
            call,
            mapped,
            error.detail,
            retry_after=error.retry_after if isinstance(error, RateLimited) else 0.0,
            scope=error.scope if isinstance(error, RateLimited) else "channel",
            edit_window_closed=edit_window_closed and mapped == "gone",
        )

    def _failure(self, call: _Call, error: Exception) -> Response:
        """Answer any other exception as transient with a generic detail, and log it redacted."""
        self._log.error(
            "messenger call failed: operation=%s bot=%s error=%s\n%s",
            call.op.op_id,
            call.bot,
            self._redact(describe(error), *call.extras),
            self._redact("".join(traceback.format_exception(error)), *call.extras).rstrip(),
        )
        return self._problem(call, "transient", DETAIL_MESSENGER)

    def _answer_fault(self, call: _Call, problem: str) -> Response:
        self._log.error(
            "the messenger's answer does not match api/bridge.yaml: operation=%s bot=%s problem=%s",
            call.op.op_id,
            call.bot,
            self._redact(problem, *call.extras),
        )
        return self._problem(call, "transient", DETAIL_MESSENGER)

    def _store_failed(self, call: _Call, text: str) -> Response:
        self._log.error(
            "the store failed: operation=%s bot=%s error=%s", call.op.op_id, call.bot, self._redact(text, *call.extras)
        )
        return self._problem(call, "transient", DETAIL_STORE)

    def _check_content(self, body: str, buttons: Sequence[Button]) -> None:
        """Refuse a body or buttons beyond the bridge's limits and capabilities."""
        most = self._info.limits.max_body_length
        if len(body) > most:
            raise _Refusal(
                "invalid",
                f"The body is {len(body)} code points long; this bridge takes at most {most} (limits.max_body_length).",
            )
        if detail := check_buttons(buttons, self._info.capabilities.buttons):
            raise _Refusal("invalid", detail)

    async def _post_message(self, call: _Call) -> object:
        message = await call.body(MessageInput)
        self._check_content(message.body, message.buttons)
        return await self._messenger.post_message(call.bot, message)

    async def _edit_message(self, call: _Call) -> object:
        message_id = call.params(EditMessageParameters).message_id
        content = await call.body(MessageContent)
        self._check_content(content.body, content.buttons)
        return await self._messenger.edit_message(call.bot, message_id, content)

    async def _delete_message(self, call: _Call) -> object:
        message_id = call.params(DeleteMessageParameters).message_id
        try:
            await cast("Deleter", self._messenger).delete_message(call.bot, message_id)
        except BridgeError as error:
            if error.error_class != "gone":
                raise
        return None

    async def _reply_in_thread(self, call: _Call) -> object:
        message_id = call.params(ReplyInThreadParameters).message_id
        reply = await call.body(ThreadReplyInput)
        self._check_content(reply.body, reply.buttons)
        return await self._messenger.reply_in_thread(call.bot, message_id, reply)

    async def _check_channel(self, call: _Call) -> object:
        channel_id = call.params(CheckChannelParameters).channel_id
        name: object = await self._messenger.check_channel(call.bot, channel_id)
        if not isinstance(name, str):
            raise _AnswerFault("check_channel returned no str")
        return ChannelCheck(
            channel_id=channel_id, name=cut(name, MAX_CHANNEL_NAME) or cut(channel_id, MAX_CHANNEL_NAME)
        )

    async def _find_users(self, call: _Call) -> object:
        handle = call.params(FindUsersParameters).handle
        person: object = await cast("UserFinder", self._messenger).find_user(call.bot, handle)
        if person is None:
            return PersonList(items=[])
        if not isinstance(person, Person):
            raise _AnswerFault("find_user returned neither a Person nor None")
        return PersonList(items=[person])

    async def _send_direct_message(self, call: _Call) -> object:
        message = await call.body(DirectMessageInput)
        return await cast("DirectMessenger", self._messenger).send_direct_message(call.bot, message)

    async def _answer_event(self, call: _Call) -> object:
        """Give the messenger the event the answer is for.

        An event the library did not issue or no longer keeps, or a deletion, which no person caused, is gone.
        """
        answer = await call.body(AnswerInput)
        try:
            found = await self._events.find(call.bot, answer.event_id)
        except StoredEventError as error:
            raise _StoreFailure(str(error)) from None
        except Exception as error:
            raise _StoreFailure(describe(error)) from None
        if found is None:
            raise _Refusal("gone", "The bridge does not know this event, or no longer keeps it.")
        event, token = found
        if token is not None:
            call.extras.append(token)
        shown: object = await self._messenger.answer_event(call.bot, event, answer.text)
        try:
            return AnswerShown.model_validate({"shown_as": shown, "replayed": False})
        except ValidationError as error:
            raise _AnswerFault(validation_problem(error)) from None


async def _disconnect(receive: Receive) -> None:
    """Return once the client of the request has left."""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


def _below(path: str, root: str) -> str:
    """Return the escaped path below the mount path root, which the client may have escaped in any way.

    A path that does not start with the segments of root is returned as it is.
    """
    if not root:
        return path
    prefix = root.split("/")
    segments = path.split("/")
    if [unquote(segment) for segment in segments[: len(prefix)]] != prefix:
        return path
    return "/" + "/".join(segments[len(prefix) :])


def _match(pattern: tuple[str | None, ...], segments: list[str]) -> list[str] | None:
    """Return the decoded path parameters of segments that match the pattern, or None when they do not.

    A parameter is one non-empty segment, percent-decoded as UTF-8.
    """
    values = []
    for want, got in zip(pattern, segments, strict=True):
        if want is not None:
            if want != got:
                return None
            continue
        if not got:
            return None
        try:
            values.append(unquote(got, errors="strict"))
        except UnicodeDecodeError:
            return None
    return values


def _replayed(op: Operation, first: StoredAnswer | None) -> tuple[bytes, str]:
    """Return the first answer of a key flagged as replayed, or why it cannot be sent again."""
    if first is None:
        return b"", "the store returned no answer"
    try:
        body = encode_json({**json.loads(first.body), "replayed": True})
    except (TypeError, ValueError):
        return b"", "it is not the JSON of the operation's answer"
    if first.status != op.status:
        return b"", f"api/bridge.yaml declares no {first.status} answer of {op.op_id}"
    return body, check_json(op.answer_model, body) if op.answer_model is not None else ""
