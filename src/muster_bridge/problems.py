# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""The errors a bridge raises for Muster, and the problems the library answers."""

from __future__ import annotations

import json
import math
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import quote, quote_plus

from pydantic import ValidationError
from starlette.responses import Response

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

__all__ = ["BridgeError", "Forbidden", "Gone", "Invalid", "RateLimited", "Transient"]

ErrorClassName = Literal["transient", "rate_limited", "gone", "forbidden", "invalid"]


class BridgeError(Exception):
    """An answer of the messenger that the bridge classifies for Muster; raise a subclass.

    detail is shown to the Admin as the reason, as untrusted text. It must not carry a secret; the library replaces
    the bridge's tokens and push secrets in it anyway, and cuts it to 1000 characters.
    """

    error_class: ClassVar[ErrorClassName | None] = None

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        """Return the class and the detail."""
        name = self.error_class or "bridge error"
        return f"{name}: {self.detail}" if self.detail else name


class Transient(BridgeError):  # noqa: N818  (named after the protocol's class, as the story names it)
    """A temporary failure: the messenger is down, a timeout. Muster retries."""

    error_class = "transient"


class RateLimited(BridgeError):  # noqa: N818  (named after the protocol's class, as the story names it)
    """The messenger or the bridge throttles: Muster waits retry_after seconds before the next request.

    retry_after is required, as Muster waits exactly that long: the wait the messenger asks for, or the bridge's own
    estimate. The wait holds the channel, or, with scope "bot", every channel of the bot. It is sent in whole seconds,
    rounded up; a negative wait is 0.
    """

    error_class = "rate_limited"

    def __init__(self, detail: str = "", *, retry_after: float, scope: Literal["channel", "bot"] = "channel") -> None:
        if scope not in ("channel", "bot"):
            raise ValueError(f'scope must be "channel" or "bot", not {scope!r}')
        if not math.isfinite(retry_after):
            raise ValueError("retry_after must be a finite number of seconds")
        super().__init__(detail)
        self.retry_after = retry_after
        self.scope = scope


class Gone(BridgeError):  # noqa: N818  (named after the protocol's class, as the story names it)
    """The message or its Thread no longer exists.

    With edit_window_closed the message still exists, but is past the messenger's edit window; Muster then posts a
    new Root message.
    """

    error_class = "gone"

    def __init__(self, detail: str = "", *, edit_window_closed: bool = False) -> None:
        super().__init__(detail)
        self.edit_window_closed = edit_window_closed


class Forbidden(BridgeError):  # noqa: N818  (named after the protocol's class, as the story names it)
    """The channel does not exist, the bot lacks the rights there, or the person blocked the bot.

    A channel that does not exist is forbidden, never gone.
    """

    error_class = "forbidden"


class Invalid(BridgeError):  # noqa: N818  (named after the protocol's class, as the story names it)
    """The request cannot succeed as sent: too long, malformed, an unknown id."""

    error_class = "invalid"


DETAIL_MESSENGER = "The bridge could not reach its messenger."
DETAIL_STORE = "The bridge could not use its store."
DETAIL_WAIT = "The bridge could not wait for events."
MAX_DETAIL = 1000
NO_STORE = {"Cache-Control": "no-store"}

CLASS_STATUS: dict[ErrorClassName, int] = {
    "transient": HTTPStatus.INTERNAL_SERVER_ERROR,
    "rate_limited": HTTPStatus.TOO_MANY_REQUESTS,
    "gone": HTTPStatus.NOT_FOUND,
    "forbidden": HTTPStatus.FORBIDDEN,
    "invalid": HTTPStatus.BAD_REQUEST,
}
CLASS_TITLE: dict[ErrorClassName, str] = {
    "transient": "Temporary failure",
    "rate_limited": "Too many requests",
    "gone": "Gone",
    "forbidden": "Forbidden",
    "invalid": "Invalid request",
}

DECLARED: dict[str, frozenset[str]] = {
    "getInfo": frozenset({"transient"}),
    "postMessage": frozenset({"invalid", "forbidden", "rate_limited", "transient"}),
    "editMessage": frozenset({"invalid", "forbidden", "gone", "rate_limited", "transient"}),
    "deleteMessage": frozenset({"forbidden", "rate_limited", "transient"}),
    "replyInThread": frozenset({"invalid", "forbidden", "gone", "rate_limited", "transient"}),
    "checkChannel": frozenset({"forbidden", "rate_limited", "transient"}),
    "findUsers": frozenset({"invalid", "rate_limited", "transient"}),
    "sendDirectMessage": frozenset({"invalid", "forbidden", "rate_limited", "transient"}),
    "answerEvent": frozenset({"invalid", "gone", "rate_limited", "transient"}),
    "pollEvents": frozenset({"invalid", "rate_limited", "transient"}),
}
"""The classes each operation declares in api/bridge.yaml, besides 401."""

_GONE_MEANS_FORBIDDEN = frozenset({"postMessage", "sendDirectMessage", "checkChannel"})


def map_class(op_id: str, error_class: str | None) -> tuple[ErrorClassName, bool]:
    """Return the class an operation answers for a class of the messenger, and whether it is the bridge's mistake.

    The class itself when the operation declares it; else the one the protocol's meaning maps it to (a missing
    channel or account is forbidden, never gone); else transient, a mistake of the bridge. deleteMessage's gone is a
    success and handled before.
    """
    for name in CLASS_STATUS:
        if name == error_class and name in DECLARED[op_id]:
            return name, False
    if (error_class == "gone" and op_id in _GONE_MEANS_FORBIDDEN) or (
        error_class == "invalid" and op_id == "checkChannel"
    ):
        return "forbidden", False
    return "transient", True


def describe(error: BaseException) -> str:
    """Return the type and the text of an exception, for a log line; the caller redacts it."""
    return f"{type(error).__name__}: {error}"


def cut(text: str, n: int) -> str:
    """Return the first n code points of text."""
    return text[:n]


def retry_after_seconds(seconds: float) -> int:
    """Return seconds in whole seconds, rounded up; a negative wait is 0."""
    return math.ceil(max(0.0, seconds))


def encode_json(value: Any) -> bytes:
    """Encode value as compact JSON in UTF-8; NaN and infinities raise ValueError."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def json_pointer(loc: Sequence[int | str]) -> str:
    """Return the JSON pointer of a pydantic error location, "" for the document itself."""
    return "".join(f"/{part}" for part in loc)


def validation_problems(error: ValidationError, *, skip: int = 0) -> list[str]:
    """Return where and why each error of a validation is, never the value: "/limits/max_body_length: …".

    skip leaves out the first parts of the location, such as the tag of a union.
    """
    problems = []
    for detail in error.errors(include_url=False, include_input=False, include_context=False):
        pointer = json_pointer(detail["loc"][skip:])
        problems.append(f"{pointer}: {detail['msg']}" if pointer else detail["msg"])
    return problems


def validation_problem(error: ValidationError, *, skip: int = 0) -> str:
    """Return where and why the first error of a validation is, never the value."""
    return validation_problems(error, skip=skip)[0]


def check_json(model: type[BaseModel], data: bytes) -> str:
    """Return where and why the JSON data does not match the model in strict mode, "" when it does."""
    try:
        model.model_validate_json(data, strict=True)
    except ValidationError as error:
        return validation_problem(error)
    return ""


class Redactor:
    """Replaces the bot tokens and push secrets, and their escaped forms, by [redacted]."""

    def __init__(self) -> None:
        self._variants: set[str] = set()
        self._pattern: re.Pattern[str] | None = None

    def add(self, *secrets: str) -> None:
        """Redact each secret from now on; empty ones are ignored.

        A secret can reach a log line escaped, as part of a URL path or query, so each escaping is redacted as well.
        """
        before = len(self._variants)
        for secret in secrets:
            if secret:
                self._variants.update(
                    (secret, quote(secret, safe="$&+,:;=@"), quote_plus(secret, safe="")),
                )
        if len(self._variants) != before:
            # Longer variants first, so that a secret inside a longer one never spoils the longer one's replacement.
            ordered = sorted(self._variants, key=lambda v: (-len(v), v))
            self._pattern = re.compile("|".join(map(re.escape, ordered)))

    def __call__(self, text: str, *extra: str) -> str:
        """Return text with the secrets and the extra secrets replaced."""
        for secret in extra:
            if secret:
                text = text.replace(secret, "[redacted]")
        if self._pattern is None:
            return text
        return self._pattern.sub("[redacted]", text)


def problem_response(
    error_class: ErrorClassName,
    detail: str,
    *,
    status: int | None = None,
    retry_after: float = 0.0,
    scope: str = "channel",
    edit_window_closed: bool = False,
) -> Response:
    """Answer a problem of the class, with its status unless another is given.

    rate_limited adds Retry-After and its scope; gone adds the one reason the protocol defines. The caller redacts and
    cuts the detail.
    """
    status = status or CLASS_STATUS[error_class]
    body: dict[str, Any] = {"title": CLASS_TITLE[error_class], "status": status}
    if detail:
        body["detail"] = detail
    body["class"] = error_class
    headers = dict(NO_STORE)
    if error_class == "rate_limited":
        body["scope"] = "bot" if scope == "bot" else "channel"
        headers["Retry-After"] = str(retry_after_seconds(retry_after))
    if error_class == "gone" and edit_window_closed:
        body["reason"] = "edit_window_closed"
    return Response(encode_json(body), status, headers, media_type="application/problem+json")


def unauthorized_response() -> Response:
    """Answer a request without a bot's token, as the Unauthorized answer of the specification."""
    body = {"title": "Unauthorized", "status": 401, "detail": "The token is missing or wrong.", "class": "forbidden"}
    headers = {"WWW-Authenticate": "Bearer", **NO_STORE}
    return Response(encode_json(body), HTTPStatus.UNAUTHORIZED, headers, media_type="application/problem+json")
