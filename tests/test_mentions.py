# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

import random
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from muster_bridge import Mention, MentionKind, replace_mentions

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


GROUP = Mention(ref="g1", kind=MentionKind.MentionKindGroup, external_id="oncall", display_name="oncall")
EVERYONE = Mention(ref="e1", kind=MentionKind.MentionKindEveryone, external_id="here", display_name="here")


def test_a_mention_without_kind_is_a_user() -> None:
    assert (
        Mention.model_validate_json('{"ref":"m1","external_id":"u","display_name":"U"}').kind
        == MentionKind.MentionKindUser
    )
    assert Mention(ref="m1", external_id="u", display_name="U").kind == MentionKind.MentionKindUser


def test_render_gets_each_entry_with_its_kind() -> None:
    seen: list[tuple[MentionKind, str, str]] = []

    def render(mention: Mention) -> str:
        seen.append((mention.kind, mention.external_id, mention.display_name))
        return f"<{mention.kind.value}>"

    body = "[@Alice Smith](muster-mention:m1) [@oncall](muster-mention:g1) [@here](muster-mention:e1)"
    got = replace_mentions(body, [*MENTIONS, GROUP, EVERYONE], render)
    assert got == "<user> <group> <everyone>"
    assert seen == [
        (MentionKind.MentionKindUser, "u-alice", "Alice Smith"),
        (MentionKind.MentionKindGroup, "oncall", "oncall"),
        (MentionKind.MentionKindEveryone, "here", "here"),
    ]


def test_an_undeclared_kind_is_shown_as_the_link_text_of_its_marker() -> None:
    def render(mention: Mention) -> str | None:
        return native(mention) if mention.kind == MentionKind.MentionKindUser else None

    body = "[@Alice Smith](muster-mention:m1) [@oncall](muster-mention:g1) [@here](muster-mention:e1)"
    assert replace_mentions(body, [*MENTIONS, GROUP, EVERYONE], render) == "<@u-alice> @oncall @here"


def readme_example() -> dict[str, Any]:
    """Run the README's example for group mentions and mentions of everyone in the chat and return its names."""
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Mentions of groups and of everyone in the chat", 1)[1].split("\n## ", 1)[0]
    match = re.search(r"```python\n(.*?)```", section, re.DOTALL)
    assert match, "README.md has no example under Mentions of groups and of everyone in the chat"
    scope: dict[str, Any] = {}
    exec(compile(match.group(1), "README.md", "exec"), scope)  # noqa: S102 - the repository's own example
    return scope


def test_the_readme_example_renders_a_user_a_group_and_here() -> None:
    scope = readme_example()
    assert scope["text"] == "<@u-alice> <!group:oncall> <!here> @all"
    capabilities = scope["capabilities"]
    assert capabilities.group_mentions is True
    assert [name.value for name in capabilities.everyone_mentions] == ["here"]
