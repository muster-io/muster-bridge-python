# muster-bridge-python

The Python library for writing a bridge between [Muster](https://github.com/muster-io/muster) and a messenger it does
not support natively: the library serves Muster's bridge protocol (`api/bridge.yaml`) as an asynchronous ASGI
application — tokens, validation, idempotency, events and signed push — and the bridge author writes only the calls to
their messenger. MIT licensed.

A bridge is a small HTTP service that Muster calls to post, edit and reply to messages, and that reports what people do
in the messenger — button presses, messages in Threads, Account link tokens — as events. Why the protocol exists and
how it is versioned is Muster's
[ADR-0017](https://github.com/muster-io/muster/blob/master/design/adr/0017-bridge-protocol-for-messengers-without-native-support.md);
the protocol itself is [`api/bridge.yaml`](https://github.com/muster-io/muster/blob/master/api/bridge.yaml), of which
this repository keeps a byte-for-byte copy.

## Install

```sh
pip install muster-bridge
```

Python 3.11 or newer. The distribution is `muster-bridge`, the import package `muster_bridge`; it depends on
Starlette, httpx and pydantic 2 only. Versions stay `0.x` until Muster 1.0. Every `0.x` release speaks protocol
version 1; a breaking protocol change would be a new protocol version, not a new library version.

## Quick start

[`examples/memory_bridge.py`](https://github.com/muster-io/muster-bridge-python/blob/master/examples/memory_bridge.py) is a whole bridge in one file: its messenger lives in memory
and writes every message the bot posts to the log. Run it with a token, the one a Bridge Connection in Muster would
hold. In a clone, with [uv](https://docs.astral.sh/uv/):

```sh
printf example-token > /tmp/bt
uv run python examples/memory_bridge.py --token-file /tmp/bt
```

or with the package installed beside uvicorn (`pip install muster-bridge uvicorn`), as
`python examples/memory_bridge.py --token-file /tmp/bt`.

It listens on `127.0.0.1:18095` (`--listen`), knows the channels `alerts` and `ops` (`--channels`) and the people
`alice` and `bob` (`--users`), and offers every feature of the protocol, or only those that `BRIDGE_FEATURES` lists:
`buttons`, `thread_messages`, `direct_messages` (which also turns on `user_lookup` `login`), `private_answers`,
`native_mentions`, `group_mentions`, `everyone_mentions`, `quiet_messages`, `status_color`, `deletion_events` and
`delete`. With `--push-url` and `--push-secret-file` it pushes its events as well. In another terminal:

```sh
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:18095/v1/info          # 401: no token
curl -s -H 'Authorization: Bearer example-token' 127.0.0.1:18095/v1/info \
  | jq -c '{protocol_version, user_lookup}'                                 # {"protocol_version":"1","user_lookup":"login"}
```

Then let Muster's conformance check run every step of the protocol against it, from Muster's development image. The
check reaches the example on the host's loopback, which `--network host` gives only on Linux:

```sh
docker run --rm --network host -v /tmp/bt:/token:ro ghcr.io/muster-io/muster:latest bridge check \
  http://127.0.0.1:18095 --token-file /token --channel alerts --forbidden-channel nobot --handle alice \
  --no-interactive
```

```text
PASS info
PASS channel_check
PASS post
PASS replayed_key
PASS buttons
PASS edit
PASS thread_reply
PASS thread_ref
PASS forbidden_channel
PASS user_lookup
PASS direct_message
PASS delete
PASS poll
SKIP press: --no-interactive
SKIP push: --no-interactive
PASS answer
```

The heart of the example is a `Config`, a messenger and the application, mounted in a Starlette application beside a
route of its own and served by uvicorn:

```python
config = Config(
    name="muster-bridge-python memory example",
    version=muster_bridge.__version__,
    messenger_name="Memory example",
    bots=[Bot(name="main", token=token)],
    limits=Limits(max_body_length=4000, edit_max_age_seconds=None),
    capabilities=Capabilities(
        buttons=CapabilitiesButtons(
            max_per_message=5,
            max_per_row=5,
            data_max_bytes=64,
            styles=[ButtonStyle.primary, ButtonStyle.success, ButtonStyle.neutral, ButtonStyle.danger],
        ),
        thread_messages=CapabilitiesThreadMessages.all,
        direct_messages=CapabilitiesDirectMessages(bot_can_start=True),
        delete=CapabilitiesDelete(max_age_seconds=None),
    ),
    user_lookup="login",
)
bridge = BridgeApp(config, chat)  # chat implements Messenger, UserFinder, DirectMessenger and Deleter

app = Starlette(
    routes=[
        # Outside the protocol: press the first button of a message as a person, for scripts.
        Route("/demo/press", demo_press, methods=["POST"]),
        Mount("/", app=bridge),
    ],
    lifespan=bridge.lifespan,
)
uvicorn.run(app, host="127.0.0.1", port=18095)
```

`POST /demo/press` `{"message_id": "4f0c1a-m2", "user": "alice"}` presses the first button of message `4f0c1a-m2` as
`alice`, so that a script can play the person of an interactive check. It has no authentication of its own: keep it
on loopback. Message ids start with a prefix that is random for each start of the example, so that a restarted example
never reuses an id that Muster still holds.

## Serving the bridge

`BridgeApp` is an ASGI application: serve it with uvicorn or another ASGI server, on its own or mounted under a path of
a Starlette or FastAPI application; the base URL of the Bridge Connection in Muster then ends with that path. Starlette
runs the lifespan of the top-level application only, so a host that mounts the bridge passes the bridge's on, which
stops the pushers on shutdown:

```python
bridge = BridgeApp(config, messenger)
app = FastAPI(lifespan=bridge.lifespan)
app.mount("/bridge", bridge)
```

A host with a lifespan of its own enters the bridge's in it:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with bridge.lifespan(app):
        yield
```

A `BridgeApp` has one lifespan: its end stops the pushers for good. A program or a test that starts the host again,
such as a second `with TestClient(app)`, makes a new `BridgeApp`. Without a lifespan, as with `TestClient(app)` without
`with`, the bridge runs on each request's event loop in turn, and a pusher starts on the next event.

`pollEvents` holds a request for up to 60 seconds. uvicorn sets no timeout that cuts such a request short; a proxy in
front of the bridge needs a read timeout above 75 seconds, or it cuts a poll off without an answer, so that its events
reach Muster late.

## Writing a bridge

1. Implement the protocol [`Messenger`](https://github.com/muster-io/muster-bridge-python/blob/master/src/muster_bridge/messenger.py): the coroutines `post_message`,
   `edit_message`, `reply_in_thread`, `check_channel` and `answer_event`, each given the name of the bot whose token
   Muster sent. Define them with `async def`; a method wrapped by a decorator that returns a coroutine is taken once the
   wrapper is marked with `inspect.markcoroutinefunction` (Python 3.12 or newer). For the features you declare, also
   `UserFinder` (`find_user`, for a `user_lookup` other than `none`), `DirectMessenger` (`send_direct_message`, for
   `capabilities.direct_messages`) and `Deleter` (`delete_message`, for `capabilities.delete`). `BridgeApp` raises
   `ConfigError` for a configuration whose features lack their methods, or whose `info` would not match the protocol,
   naming every reason. The library validates every request against the models generated from the protocol before it
   calls a method, and every answer before it sends it. A `layout` that does not match the protocol — a `status.key` Muster adds later, a missing `ref`, a `null` where the
   protocol allows only an absent field — does not refuse the message: the library drops it, so that `message.layout` is
   `None` and the bridge renders `body`, and logs one warning with the place and the reason, never the message's text.
   An error anywhere else is still answered as `invalid`. The library reads a request of at most 4 MiB.
2. Answer the messenger's failures by raising `Transient`, `RateLimited` (with the wait `retry_after` in seconds, which
   it requires because Muster waits exactly that long, and `scope` `channel` or `bot`), `Gone` (with
   `edit_window_closed=True` for an edit the messenger refuses only for the message's age), `Forbidden` or `Invalid`,
   each with a `detail` that Muster shows the Admin: never put a secret in it. Each becomes its problem, with
   `Retry-After` for `RateLimited`; a class the operation does not declare is mapped — `Gone` from `post_message`,
   `send_direct_message` and `check_channel` and `Invalid` from `check_channel` answer `forbidden`, any other
   `transient` — and logged. Any other exception is answered as `transient` with a generic detail that carries none of
   its text, and logged without the token.
3. Serve the application as _Serving the bridge_ above says.
4. Report what the messenger tells you, from the event loop that serves the bridge: `await bridge.press(...)`,
   `thread_message(...)`, `message_deleted(...)` and `direct_message(...)`. Each queues the event and returns at once,
   so you confirm a press to your messenger within its deadline whatever Muster does; give the messenger's own update
   id as `update_id`, so that an update it delivers twice is queued once, and in `chat` whatever you need to answer
   the person, which `answer_event` gets back in `ReportedEvent.chat` and Muster never sees. A direct message that is
   an Account link token becomes an `account_link` event; any other direct message is ignored. A report raises
   `UnknownBotError`, `NotOfferedError` for a feature the bridge does not offer and `InvalidEventError` for an event
   that would not match the protocol. From another thread, hand the coroutine to the loop with
   `asyncio.run_coroutine_threadsafe`.
5. Turn Mention markers into native mentions or plain names with `replace_mentions(body, mentions, render)`; see
   _Mentions of groups and of everyone in the chat_ below.
6. For pushes, give a bot a `PushTarget` — the callback URL and the push secret that its Bridge Connection in Muster
   shows — in `Bot(push=...)` or later with `bridge.set_push(bot, target)`, from the thread of the event loop that
   serves the bridge (from another thread, `loop.call_soon_threadsafe(bridge.set_push, bot, target)`). The pushers
   are tasks of the running event loop that post each event, signed as Standard Webhooks specifies, one at a time and
   in order, retrying with back-off for at least an hour; `aclose()` or the end of `lifespan` stops them for good.
   Events stay queued for polling either way.
7. `MemoryStore`, the default `Store`, forgets Idempotency-Keys and queued events on a restart, and holds a day of
   events and answers in memory; implement the protocol `Store` over your database to keep them. Long polls and pushes
   are woken in the process that queued an event, so with several processes over one store, run each bot in one
   process only.

A few more things to know:

- `Person` and the other models are pydantic models that validate on construction: cut a display name to 200
  characters before you build the `Person` of a report.
- `Limits` needs `edit_max_age_seconds`, `None` when edits never expire, and `CapabilitiesDelete` needs
  `max_age_seconds`, `None` for no limit.
- A repeated `Idempotency-Key` under the same token answers the first result without calling the messenger again. A
  messenger with an idempotency mechanism of its own can be given the key: `idempotency_key()`, called inside
  `post_message`, `reply_in_thread`, `send_direct_message` or `answer_event`, returns it, and `None` elsewhere.
- The library logs through `Config.logger`, by default the logger `muster_bridge`. Its own messages never carry a
  token, a push secret or an Account link token, and it replaces the bots' tokens and push secrets in the text of
  every exception it logs; keep anything else secret, such as an event's Account link token, out of the exceptions of
  your messenger and your store.

## The role gateway

A bridge can be a Notification gateway: it reaches people outside messengers, by phone call or SMS for example, and
Muster calls it with `sendNotification`. Declare the role and give the bridge its `Gateway`; a bridge whose only role is
`gateway` takes no `Messenger`, and its capabilities stay empty and its `user_lookup` `none`:

```python
config = Config(
    name="my gateway",
    version="1.0.0",
    messenger_name="My phone provider",
    bots=[Bot(name="main", token=token)],
    limits=Limits(max_body_length=4000, edit_max_age_seconds=None),
    roles=["gateway"],  # ["messenger", "gateway"] for a bridge of both, with a Messenger as before
    gateway=MyGateway(),
)
bridge = BridgeApp(config)
```

1. Implement the protocol `Gateway`: `send_notification(bot, notification)`, which reaches `notification.contact`, reads
   out or sends `notification.text` in `notification.language`, and answers `NotificationSent(notification_id=...,
   replayed=False)`; and `answer(bot, event, text)`, which shows Muster's answer to the person who acknowledged,
   during the call when it is a call. Raise `Invalid` for a contact the provider refuses for good and `Forbidden` for
   credentials it refuses. `BridgeApp` raises `ConfigError` when `roles` and what each role needs do not fit.
2. The library keeps the idempotency of the call: a repeated `Idempotency-Key` answers the first `notification_id`
   with `replayed` `true` without calling the gateway, and refuses a request beyond the protocol — `action.data` over 64
   bytes, a text over 1,000 code points — as `invalid` before the gateway is called. Neither the contact nor the text
   reaches the library's logs or answers.
3. When the person acknowledges, as `notification.action.label` told them, call `await bridge.acknowledge(bot,
   notification_id)` with the id `send_notification` answered. It queues `notification_acknowledged` with the
   `action.data` of that notification, which the library keeps for 24 hours (`MemoryStore` does; a `Store` of your own
   also implements `NotificationStore`), delivered by polling and by push. An id the library did not issue, or a
   notification without `action`, raises `UnknownNotificationError`, a `ValueError`. Pass `chat`, such as a call id,
   to get it back in `AcknowledgedNotification.chat`.
4. Muster answers the event with `answerEvent` while the person waits, and the library hands it to `Gateway.answer`.

[`examples/gateway_bridge.py`](https://github.com/muster-io/muster-bridge-python/blob/master/examples/gateway_bridge.py)
is a whole gateway in one file: it prints each notification and acknowledges it from `POST /demo/acknowledge`
`{"notification_id": "n1"}`. Let Muster's check run against it, with a contact, interactively:

```sh
python examples/gateway_bridge.py --token-file /tmp/bt &
docker run --rm --network host -v /tmp/bt:/token:ro ghcr.io/muster-io/muster:edge bridge check \
  http://127.0.0.1:18095 --token-file /token --contact +10000000001
# WAIT notification_acknowledged: …, notification_id n1 → in another terminal:
curl -s -X POST -d '{"notification_id":"n1"}' -H 'Content-Type: application/json' 127.0.0.1:18095/demo/acknowledge
```

## Mentions of groups and of everyone in the chat

A `Mention` of the message names a person (`kind` `user`, which a Mention without `kind` is), a group of the messenger
(`group`) or everyone in the chat (`everyone`). For a group, `external_id` and `display_name` both carry the name an
Admin typed, which the bridge maps to its messenger's group; for everyone in the chat they carry the symbolic name:
`channel`, `here`, `all` or `everyone`. Declare what the messenger has in `Capabilities`: `group_mentions=True`, and
`everyone_mentions` with the symbolic names, each once, as `CapabilitiesEveryoneMention` members. A name outside the
four fails when `Capabilities` is built (pydantic's `ValidationError`), and a repeated one makes `BridgeApp` raise
`ConfigError`. Muster sends a group or everyone mention only while its copy of
`info` declares it, whether or not the bridge has `native_mentions`.

`replace_mentions` hands `render` the whole entry, with its `kind`. The library never refuses a Mention for its kind,
because Muster decides from a copy of `info` that may be older than the bridge's own: when `render` returns `None` for
a kind the bridge did not declare, the marker becomes its link text.
The protocol's own models stay closed, so a `kind` beyond `user`, `group` and `everyone` is answered `invalid`, as the
specification says; Muster sends no other kind in protocol version 1.

```python
from muster_bridge import Capabilities, CapabilitiesEveryoneMention, Mention, MentionKind, replace_mentions

capabilities = Capabilities(
    native_mentions=True,
    group_mentions=True,
    everyone_mentions=[CapabilitiesEveryoneMention.EveryoneMentionHere],
)


def render_mention(mention: Mention) -> str | None:
    """Render a Mention for a messenger that writes <@id> for a person, <!group:name> and <!here>."""
    if mention.kind == MentionKind.MentionKindGroup and capabilities.group_mentions:
        return f"<!group:{mention.external_id}>"
    if mention.kind == MentionKind.MentionKindEveryone:
        names = [name.value for name in capabilities.everyone_mentions or []]
        return f"<!{mention.external_id}>" if mention.external_id in names else None
    if mention.kind == MentionKind.MentionKindUser:
        return f"<@{mention.external_id}>"
    return None


body = "[@Alice](muster-mention:m1) [@oncall](muster-mention:m2) [@here](muster-mention:m3) [@all](muster-mention:m4)"
mentions = [
    Mention(ref="m1", external_id="u-alice", display_name="Alice"),
    Mention(ref="m2", kind=MentionKind.MentionKindGroup, external_id="oncall", display_name="oncall"),
    Mention(ref="m3", kind=MentionKind.MentionKindEveryone, external_id="here", display_name="here"),
    Mention(ref="m4", kind=MentionKind.MentionKindEveryone, external_id="all", display_name="all"),
]
text = replace_mentions(body, mentions, render_mention)
assert text == "<@u-alice> <!group:oncall> <!here> @all"  # all is not declared: its link text
```

## Rendering the layout

A Root message and a test message carry their content twice: as CommonMark in `body`, and as plain-text data in
`message.layout`, a `Layout`. A bridge renders either `body` or `layout`, never both. A bridge that ignores `layout`
works as before, and a message without one — a Storm summary, a Thread reply, a Root message that a template of the
Route renders — is rendered from `body`, so `layout` is `None` and the bridge always keeps that path. Every value of
`layout` is plain text, never CommonMark: the bridge escapes it for its messenger. A test message has the mark of a test
as the first of its `notices`. `LayoutLink.url` is a `str`, as Muster sends it, and may be an IRI. The Owner carries the
`ref` of its entry in `message.mentions` when the message mentions them, and `None` in an edit.

```python
from html import escape

from muster_bridge import LayoutPerson, MessageContent, MessageInput, MessageKind


def render(message: MessageContent | MessageInput) -> str:
    """Render a message for a messenger that reads HTML, from its layout, or from its body when it has none."""
    layout = message.layout
    if layout is None:
        return message.body

    def person(who: LayoutPerson) -> str:
        mention = mentions.get(who.ref) if who.ref else None
        return f"<@{escape(mention.external_id)}>" if mention else escape(who.name)

    mentions = {m.ref: m for m in message.mentions} if isinstance(message, MessageInput) else {}
    is_test = isinstance(message, MessageInput) and message.kind == MessageKind.test
    lines = [escape(layout.notices[0])] if is_test and layout.notices else []
    notices = layout.notices[len(lines) :]
    lines.append(f"<b>{escape(layout.status.word)}</b> #{layout.number} {escape(layout.title)}")
    lines += [f"{escape(f.name)}: {escape(f.value)}" for f in layout.fields]
    lines += [escape(text) for text in (layout.summary, layout.description) if text]
    lines += [f"{escape(label.key)}: {escape(label.value)}" for label in layout.labels]
    if layout.alerts:
        shown = ", ".join(f"<s>{escape(v.value)}</s>" if v.resolved else escape(v.value) for v in layout.alerts.values)
        more = layout.alerts.total - len(layout.alerts.values)
        lines.append(f"{escape(layout.alerts.label)}: {shown}" + (f" and {more} more" if more > 0 else ""))
    lines += [escape(notice) for notice in notices]
    if layout.assignees:
        lines.append("Paged: " + ", ".join(person(who) for who in layout.assignees))
    if layout.owner:
        lines.append("Owner: " + person(layout.owner))
    lines += [f'<a href="{escape(link.url)}">{escape(link.label)}</a>' for link in layout.links]
    return "\n".join(lines)
```

A status key that this version of the library does not know — a future spec may add one — makes it answer `invalid`
to that request, so upgrade the library when Muster's protocol copy changes. The number is a field of its own, because `#N` in text becomes a hashtag in some messengers; `total` is how many Alerts
the Alert Group has, so "and K more" counts the ones the message does not list. The buttons stay in `message.buttons`.

## Checking a bridge

`muster bridge check <url> --token-file <file> --channel <id>` runs every step of the protocol against a bridge and
prints `PASS`, `FAIL` or `SKIP` for each; `--forbidden-channel` and `--handle` enable the steps that need them. Without
`--no-interactive` it also waits for a person to press a button (`press`) and, for a bridge that pushes, receives the
push (`push`): it prints `PUSH <url>` once its listener (`--push-listen`) is up — point the bridge's push target there,
with the secret of `--push-secret-file` — and `WAIT press: …, message_id <id>` before it waits. `make conformance`
plays that person through the example's `POST /demo/press`; do the same in your bridge's tests with your messenger's
own means. A bridge in the role `gateway` runs the steps `notification` and `notification_acknowledged` instead, with
`--contact`: see _The role gateway_.

## The protocol copy

[`api/bridge.yaml`](https://github.com/muster-io/muster-bridge-python/blob/master/api/bridge.yaml) is a copy of Muster's, never edited here:
[`src/muster_bridge/models.py`](https://github.com/muster-io/muster-bridge-python/blob/master/src/muster_bridge/models.py), the pydantic models the library validates requests with
and builds answers from, is generated from it by datamodel-code-generator. `make spec-check` compares it with the one on
Muster's `master` byte for byte, every day in CI; `make spec-sync` takes the new one and regenerates the models.

## Development

Development needs [uv](https://docs.astral.sh/uv/) 0.13 or newer, which installs the Python of `.python-version` and
the tools locked in `uv.lock`, GNU make and curl; `make conformance` also needs Linux with Docker. The Makefile is the
only entry point; `make help` lists the targets.

| Target | Does |
|---|---|
| `make fmt` | formats the code and sorts the imports (ruff) |
| `make lint` | checks the licence headers, `uv.lock`, and the code with ruff (lint and format) and mypy in strict mode |
| `make test` | runs the tests with the coverage gate of 90 % of the package (`models.py` excluded) |
| `make generate`, `make generate-check` | generates `src/muster_bridge/models.py` from `api/bridge.yaml`; fails when it is stale |
| `make spec-check`, `make spec-sync` | compares `api/bridge.yaml` with Muster's; replaces it and regenerates |
| `make licenses` | fails on a runtime dependency outside MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0 and MPL-2.0 |
| `make build` | builds the sdist and the wheel into `dist/` |
| `make ci` | `lint`, `generate-check`, `spec-check`, `licenses`, `test` and `build`, as a pull request runs them |
| `make conformance` | installs the built wheel and uvicorn into a fresh virtual environment, runs the example from it with `python -I`, and runs `muster bridge check` from `ghcr.io/muster-io/muster:edge` (`MUSTER_IMAGE`) against it three times: every feature, only `thread_messages`, and interactively with push; then runs the check against `examples/gateway_bridge.py` with `--contact`, interactively |
| `make clean` | removes `bin/`, `dist/`, `.venv/`, the caches and the coverage data |

## Contributing, security and licence

[CONTRIBUTING.md](https://github.com/muster-io/muster-bridge-python/blob/master/CONTRIBUTING.md) says how to contribute, [AGENTS.md](https://github.com/muster-io/muster-bridge-python/blob/master/AGENTS.md) is the guide for everyone who
changes the code, and [SECURITY.md](https://github.com/muster-io/muster-bridge-python/blob/master/SECURITY.md) how to report a vulnerability privately. The library is under the
[MIT licence](https://github.com/muster-io/muster-bridge-python/blob/master/LICENSE); contributions are accepted under it, without a CLA.
