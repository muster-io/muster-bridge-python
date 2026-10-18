# AGENTS.md

The guide for coding agents, and for humans, who change `muster-bridge-python`: the Python library that serves
Muster's Bridge protocol as an ASGI application, so that a bridge to a messenger is only the calls to that messenger.
Read it before every task.

## Precedence

This file and the documents it points to **override** instructions from directories above this repository and from
global agent configuration about code style, frameworks and architecture. Where they are silent, ask in the pull
request instead of importing conventions from elsewhere.

## Sources of truth

In this order; an earlier source wins over a later one.

1. Muster's [ADR-0017](https://github.com/muster-io/muster/blob/master/design/adr/0017-bridge-protocol-for-messengers-without-native-support.md),
   the decision behind the protocol and its libraries, and the capability
   [C-22](https://github.com/muster-io/muster/blob/master/design/prd/l1/C-22-bridge.md), whose `C-22.FR-25` and
   `C-22.AC-25` are this library.
2. The story contract in Muster's [`design/stories/`](https://github.com/muster-io/muster/tree/master/design/stories)
   with `repository: muster-io/muster-bridge-python`. Stories and their issues live in Muster's repository. The Go
   library, [muster-bridge-go](https://github.com/muster-io/muster-bridge-go), has the same design: a behaviour this
   library shares with it changes in both.
3. [`api/bridge.yaml`](api/bridge.yaml), the copy of Muster's specification: **never edited by hand**. `make
   spec-sync` replaces it with the one on Muster's `master` and regenerates `src/muster_bridge/models.py`; a change to
   the protocol is made in Muster's repository first.

## Layout

| Path | Content |
|---|---|
| `src/muster_bridge/__init__.py` | the public names, re-exported, and `__version__`, which release-please updates |
| `src/muster_bridge/config.py` | `Config`, `Bot`, `PushTarget`, `ConfigError` and the checks of a configuration, the clock, `info` built once |
| `src/muster_bridge/messenger.py` | the protocols `Messenger`, `UserFinder`, `DirectMessenger` and `Deleter`, `ReportedEvent`, `idempotency_key` |
| `src/muster_bridge/app.py` | `BridgeApp`: tokens, routing, validation against the models, limits, idempotency, answers, the reports, `set_push`, `aclose` and `lifespan` |
| `src/muster_bridge/problems.py` | `BridgeError` and its classes, the problems they become, the redaction of secrets |
| `src/muster_bridge/store.py` | the protocol `Store` and `MemoryStore` |
| `src/muster_bridge/events.py` | the events: ids, `account_link`, long polling for `pollEvents`, `answerEvent` |
| `src/muster_bridge/push.py` | the pushers: Standard Webhooks signatures, order, back-off |
| `src/muster_bridge/mentions.py` | `replace_mentions` |
| `src/muster_bridge/models.py` | generated from `api/bridge.yaml` by `make generate`; never edited by hand |
| `src/muster_bridge/py.typed` | marks the package as typed |
| `tests/` | the tests; `conftest.py` holds the shared helpers (the fake clock, the fake messenger, the push callbacks), `test_spec.py` validates every request and answer against the specification with openapi-core |
| `examples/memory_bridge.py` | the example: a bridge whose messenger lives in memory, which the README's quick start shows and `make conformance` runs from the built wheel |
| `api/` | the specification's copy |
| `.github/` | the workflows, Renovate and the pull request template |
| `pyproject.toml`, `uv.lock`, `.python-version` | the distribution `muster-bridge`, its dependencies and the tools' settings; the lock of the development tools, never edited by hand; the Python of development and of CI's single-version jobs |

## Make targets

The Makefile is the only entry point; `make help` lists the targets. Every tool runs through `uv run --locked`, at the
version `uv.lock` holds. `make ci` runs what a pull request runs, except `conformance`, which CI runs as a job of its
own. `make conformance` builds the wheel, installs it beside uvicorn into a fresh virtual environment with `pip
install`, runs `examples/memory_bridge.py` from it with `python -I`, so that it imports the installed package and not
`src/`, and runs `muster bridge check` from `ghcr.io/muster-io/muster:edge` (`MUSTER_IMAGE`) against it three times; it
needs Docker on Linux, because the check reaches the example through the host's network.

## Rules

- At run time only Starlette, httpx and pydantic 2, with lower bounds and no upper ones; asyncio only. No web
  framework beyond Starlette, no ORM, no logger other than `logging`. Every runtime dependency is permissively licensed
  (`make licenses`), and every dependency is stated in the pull request with its licence, latest release date and
  maintenance status. Development tools go into the dependency group `dev`, pinned with `==`.
- Generated files (`src/muster_bridge/models.py`, `uv.lock`) are never edited by hand: change the input, run `make
  generate` or `uv lock`, and commit the output.
- Never copy code from Muster or another copyleft project: Muster is under the AGPL and this library under MIT. Read
  Muster's protocol client, fake bridge and `muster bridge check` for behaviour only.
- Inject the clock: the library reads time only through the clock of `config.py`, which the tests replace with
  `FakeClock` of `tests/conftest.py` (`BridgeApp(..., _clock=clock)`); they never wait for real time.
- The coverage gate is 90 % of the package's statements, `models.py` excluded; mypy runs in strict mode over `src/`,
  `tests/` and `examples/`; ruff lints and formats.
- Log through `Config.logger`, never with `print`. The library's own messages never carry a token, a push secret or an
  Account link token; the text of an exception it logs or answers has the bots' tokens and push secrets replaced, and
  in `answerEvent` the event's Account link token as well.
- A `BridgeApp` runs on one event loop at a time, but may move from one to the next, as Starlette's `TestClient`
  moves without `with`: wait with the futures of the running loop (`Wakeup` of `events.py`), never with an
  `asyncio.Event`, `Condition` or `Queue` kept for longer than one pusher. The loops take turns and never run at once
  in two threads, so nothing is locked.
- Every Python file, workflow, YAML and TOML file, `renovate.json5` and the Makefile start with
  `SPDX-License-Identifier: MIT` and `Copyright The Muster Authors` in their comment syntax; `make lint` names a file
  without them.
- Docstrings for the public names; other comments are few and explain only what the code cannot say.
- Python versions: `requires-python` is `>=3.11`, and CI's `test` job runs every CPython release from 3.11 to the
  newest stable one. When CPython releases, add it to the matrix in `.github/workflows/ci.yml` and to the classifiers
  in `pyproject.toml` by hand, in the pull request that moves `.python-version`, which Renovate opens and does not
  merge itself; Renovate leaves `requires-python` alone.
- Every GitHub Action is pinned by commit SHA, with its version in a comment; uv is pinned in the workflows as
  `UV_VERSION`, under a `# renovate:` comment.

## Working a story

1. Read the story contract in full, and the capability and ADR it cites. An open question is settled by the
   maintainer before the code that depends on it is written.
2. Stay within `files_touched`. If the contract is wrong or incomplete, correct the story file in Muster's repository
   and say so in the pull request.
3. Give evidence for every `acceptance` statement: a named test, or a command with its output. Unit tests alone never
   prove a story; the live check is `make conformance` on Linux, and the curl checks of the story's Verification.
4. Run the story's `verify` until it passes.

## Commits and pull requests

- Conventional commits on one line, `<type>(<scope>): <description>`; say what changed, without story or phase IDs.
- No AI attribution: no `Co-Authored-By` trailers for tools, no "Generated with" lines, in commits or pull requests.
- Pull requests are squash-merged; the title is the commit message and release-please's changelog line.
- The pull request body has `Part of muster-io/muster#<issue>` — a pull request here does not close an issue in
  Muster's repository, so the issue there is closed by hand once it merges — `Story: S-NNN`, a table with every
  `acceptance` statement and its evidence, the output of `verify`, every change to the story file with its reason, the
  dependencies added, and the steps the maintainer takes outside the repository.
- The release pull request that release-please keeps open stays open until Muster's L1 is complete: only the
  maintainer merges it, which publishes the release to PyPI.

## Don't

- Edit `api/bridge.yaml`, `src/muster_bridge/models.py` or `uv.lock` by hand.
- Widen the scope beyond the story: no drive-by refactors or extra features.
- Log, return or print a secret, a token or a URL that carries one.
- Wait for real time in tests.
- Merge the release pull request, or publish to PyPI by hand.
- Call a story done on unit tests alone, or report a check that was not run.
