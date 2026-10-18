# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import random
import re
import time
from collections.abc import Callable, Sequence

import pytest

from muster_bridge import Mention, replace_mentions

MENTIONS = [
    Mention(ref="m1", external_id="u-alice", display_name="Alice Smith"),
    Mention(ref="m2", external_id="u-bob", display_name="Bob [ops]"),
    Mention(ref="m1", external_id="u-other", display_name="Not the first"),
]


def native(mention: Mention) -> str:
    return f"<@{mention.external_id}>"


def plain(mention: Mention) -> str:
    return mention.display_name


@pytest.mark.parametrize(
    ("body", "mentions", "render", "want"),
    [
        pytest.param(
            "[@Alice Smith](muster-mention:m1) please look", MENTIONS, native, "<@u-alice> please look", id="one"
        ),
        pytest.param(
            "[@Alice Smith](muster-mention:m1), [@Bob](muster-mention:m2)",
            MENTIONS,
            native,
            "<@u-alice>, <@u-bob>",
            id="several",
        ),
        pytest.param(
            "[@Carol](muster-mention:m9) and [@Alice Smith](muster-mention:m1)",
            MENTIONS,
            native,
            "@Carol and <@u-alice>",
            id="unknown ref",
        ),
        pytest.param(r"[@Bob \[ops\]](muster-mention:m2)", MENTIONS, native, "<@u-bob>", id="escaped name"),
        pytest.param(r"[@Bob \[ops\]](muster-mention:m3)", MENTIONS, native, r"@Bob \[ops\]", id="escaped name kept"),
        pytest.param(
            "[@Alice Smith](muster-mention:m1) [Runbook](https://wiki.example.org/x)",
            MENTIONS,
            native,
            "<@u-alice> [Runbook](https://wiki.example.org/x)",
            id="other links untouched",
        ),
        pytest.param("**prod** · started 14:03", MENTIONS, native, "**prod** · started 14:03", id="no markers"),
        pytest.param(
            "[@A](muster-mention:abcdefghijklmnopq)",
            MENTIONS,
            native,
            "[@A](muster-mention:abcdefghijklmnopq)",
            id="ref of 17 characters",
        ),
        pytest.param("[@A](muster-mention:M1)", MENTIONS, native, "[@A](muster-mention:M1)", id="upper-case ref"),
        pytest.param("[Alice](muster-mention:m1)", MENTIONS, native, "[Alice](muster-mention:m1)", id="no @"),
        pytest.param("[@Alice Smith](muster-mention:m1)", [], native, "@Alice Smith", id="no mentions"),
        pytest.param("[@Alice Smith](muster-mention:m1)!", MENTIONS, plain, "Alice Smith!", id="plain names"),
        pytest.param("[@Alice Smith](muster-mention:m1)", MENTIONS, None, "@Alice Smith", id="no render"),
        pytest.param("[@Alice Smith](muster-mention:m1)", MENTIONS, native, "<@u-alice>", id="first entry wins"),
        pytest.param(
            "[@Alice\nSmith](muster-mention:m1)",
            MENTIONS,
            native,
            "[@Alice\nSmith](muster-mention:m1)",
            id="no line break in a name",
        ),
        pytest.param("", MENTIONS, native, "", id="empty"),
    ],
)
def test_replace_mentions(
    body: str, mentions: Sequence[Mention], render: Callable[[Mention], str] | None, want: str
) -> None:
    assert replace_mentions(body, mentions, render) == want


# The pattern of a marker as the Go library finds it, with Go's linear regex engine; the library must find the same
# markers.
REFERENCE = re.compile(r"\[(@(?:\\.|[^\\\]\n])*)\]\(muster-mention:([a-z0-9]{1,16})\)")


def reference(body: str, mentions: Sequence[Mention], render: Callable[[Mention], str] | None) -> str:
    by_ref: dict[str, Mention] = {}
    for mention in mentions:
        by_ref.setdefault(mention.ref, mention)

    def replace(marker: re.Match[str]) -> str:
        mention = by_ref.get(marker.group(2))
        return render(mention) if mention is not None and render is not None else marker.group(1)

    return REFERENCE.sub(replace, body)


def test_replace_mentions_finds_what_the_pattern_finds() -> None:
    pieces = ["[", "@", "]", "\\", "\n", "(", ")", "x", " ", "é", "[@", "](muster-mention:", "m1", "m2", "M1", "\\]"]
    rng = random.Random(84)  # noqa: S311  (reproducible test input, not a secret)
    for _ in range(5000):
        body = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 24)))
        render = rng.choice([native, None])
        assert replace_mentions(body, MENTIONS, render) == reference(body, MENTIONS, render), repr(body)


ADVERSARIAL = "[@" * 50_000
ESCAPED = r"\[@" * 33_333


@pytest.mark.parametrize(
    ("body", "want"),
    [
        pytest.param(ADVERSARIAL, ADVERSARIAL, id="no marker"),
        pytest.param(ADVERSARIAL + "]x](muster-mention:m1)", ADVERSARIAL + "]x](muster-mention:m1)", id="near marker"),
        pytest.param(ADVERSARIAL + "\n[@Bob](muster-mention:m2)", ADVERSARIAL + "\n<@u-bob>", id="marker after"),
        pytest.param(ADVERSARIAL + "[@Bob](muster-mention:m2)", "<@u-bob>", id="one long link text"),
        pytest.param(ESCAPED + "]x](muster-mention:m1)", ESCAPED + "]x](muster-mention:m1)", id="escaped"),
    ],
)
def test_replace_mentions_takes_linear_time(body: str, want: str) -> None:
    started = time.perf_counter()
    assert replace_mentions(body, MENTIONS, native) == want
    # A quadratic scan takes minutes over 100 000 characters, a linear one milliseconds; 5 s leaves room for any CI.
    assert time.perf_counter() - started < 5
