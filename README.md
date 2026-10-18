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
Starlette, httpx and pydantic 2 only. The package is not on PyPI before its first release; until then, install it from
a clone (`pip install .`). Versions stay `0.x` until Muster's first release; then `1.x` speaks protocol version 1.

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
`native_mentions`, `quiet_messages`, `status_color`, `deletion_events` and `delete`. With `--push-url` and
`--push-secret-file` it pushes its events as well. In another terminal:

```sh
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:18095/v1/info          # 401: no token
curl -s -H 'Authorization: Bearer example-token' 127.0.0.1:18095/v1/info \
  | jq -c '{protocol_version, user_lookup}'                                 # {"protocol_version":"1","user_lookup":"login"}
```

Then let Muster's conformance check run every step of the protocol against it, from Muster's development image. The
check reaches the example on the host's loopback, which `--network host` gives only on Linux:

```sh
docker run --rm --network host -v /tmp/bt:/token:ro ghcr.io/muster-io/muster:edge bridge check \
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
`alice`, so that a script can play the person of an interactive check. Like the rest of the example it has no
authentication of its own: keep it on loopback. Message ids start with a prefix that is random for each start of the
example, so that a restarted example never reuses an id that Muster still holds.

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
   calls a method, and every answer before it sends it.
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
5. Turn Mention markers into native mentions or plain names with `replace_mentions(body, mentions, render)`.
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

## Checking a bridge

`muster bridge check <url> --token-file <file> --channel <id>` runs every step of the protocol against a bridge and
prints `PASS`, `FAIL` or `SKIP` for each; `--forbidden-channel` and `--handle` enable the steps that need them. Without
`--no-interactive` it also waits for a person to press a button (`press`) and, for a bridge that pushes, receives the
push (`push`): it prints `PUSH <url>` once its listener (`--push-listen`) is up — point the bridge's push target there,
with the secret of `--push-secret-file` — and `WAIT press: …, message_id <id>` before it waits. `make conformance`
plays that person through the example's `POST /demo/press`; do the same in your bridge's tests with your messenger's
own means.

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
| `make conformance` | installs the built wheel and uvicorn into a fresh virtual environment, runs the example from it with `python -I`, and runs `muster bridge check` from `ghcr.io/muster-io/muster:edge` (`MUSTER_IMAGE`) against it three times: every feature, only `thread_messages`, and interactively with push |
| `make clean` | removes `bin/`, `dist/`, `.venv/`, the caches and the coverage data |

## Contributing, security and licence

[CONTRIBUTING.md](https://github.com/muster-io/muster-bridge-python/blob/master/CONTRIBUTING.md) says how to contribute, [AGENTS.md](https://github.com/muster-io/muster-bridge-python/blob/master/AGENTS.md) is the guide for everyone who
changes the code, and [SECURITY.md](https://github.com/muster-io/muster-bridge-python/blob/master/SECURITY.md) how to report a vulnerability privately. The library is under the
[MIT licence](https://github.com/muster-io/muster-bridge-python/blob/master/LICENSE); contributions are accepted under it, without a CLA.
