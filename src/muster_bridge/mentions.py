# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

"""Mention markers of a body, rendered for the messenger."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from muster_bridge.models import Mention

__all__ = ["replace_mentions"]

# A Mention marker is [@Display Name](muster-mention:ref); _TAIL follows its link text, and _REF matches from there.
_TAIL = "](muster-mention:"
_REF = re.compile(r"\]\(muster-mention:([a-z0-9]{1,16})\)")


def replace_mentions(body: str, mentions: Sequence[Mention], render: Callable[[Mention], str] | None) -> str:
    """Replace each Mention marker [@name](muster-mention:ref) of body by what render makes of its entry of mentions.

    render makes the messenger's own mention for a bridge with native_mentions, or the plain name. A marker whose ref
    names no entry, or every marker when render is None, becomes its link text, the @name as the body escapes it. The
    first entry of a ref wins. The rest of body is unchanged. It takes time linear in the length of body.
    """
    if _TAIL not in body:
        return body
    by_ref: dict[str, Mention] = {}
    for mention in mentions:
        by_ref.setdefault(mention.ref, mention)
    parts: list[str] = []
    done = 0
    for start, end, text, ref in _markers(body):
        found = by_ref.get(ref)
        parts += (body[done:start], render(found) if found is not None and render is not None else text)
        done = end
    parts.append(body[done:])
    return "".join(parts)


def _markers(body: str) -> Iterator[tuple[int, int, str, str]]:
    r"""Yield the Mention markers of body, leftmost first and apart, as their start, end, link text and ref.

    They are what re.finditer finds with \[(@(?:\\.|[^\\\]\n])*)\]\(muster-mention:([a-z0-9]{1,16})\), the pattern
    of a marker whose name Muster escaped for CommonMark, but in linear time: Python's backtracking re takes quadratic
    time over a line with many [@ and no marker that completes. The link text after a [ ends at its first ] that no
    backslash escapes, so where it ends is worked out once for every position, from the end of body.
    """
    n = len(body)
    # close[i]: where a link text that goes on at i ends, at its first unescaped ], or -1 when a line break, a
    # backslash before one, or the end of body comes first.
    close = [-1] * (n + 2)
    for i in range(n - 1, -1, -1):
        char = body[i]
        if char == "]":
            close[i] = i
        elif char == "\\":
            close[i] = close[i + 2] if i + 1 < n and body[i + 1] != "\n" else -1
        elif char != "\n":
            close[i] = close[i + 1]
    start = body.find("[@")
    while start >= 0:
        end = close[start + 2]
        tail = _REF.match(body, end) if end >= 0 else None
        if tail is None:
            start = body.find("[@", start + 1)
            continue
        yield start, tail.end(), body[start + 1 : end], tail.group(1)
        start = body.find("[@", tail.end())
