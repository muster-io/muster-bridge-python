# Contributing to muster-bridge-python

Thank you for helping. This guide covers how work is proposed, how a pull request is built and what it must prove.
[`AGENTS.md`](AGENTS.md) is the detailed guide for everyone who changes the code, people and coding agents alike; read
it before your first pull request.

## Licence of contributions

The library is under the [MIT licence](LICENSE), and contributions are accepted under the same licence: by opening a
pull request you agree that your contribution is licensed under MIT. There is no Contributor License Agreement.

## Proposing work

Open an issue for a bug or a change you would like to see. Planned work is described as story contracts in Muster's
repository, in [`design/stories/`](https://github.com/muster-io/muster/tree/master/design/stories), with
`repository: muster-io/muster-bridge-python` in their frontmatter; their issues, titled `S-NNN: <title>`, live in
[muster-io/muster](https://github.com/muster-io/muster/issues). A change to the protocol itself belongs in Muster's
repository: [`api/bridge.yaml`](api/bridge.yaml) here is only its copy.

## Development setup

You need [uv](https://docs.astral.sh/uv/) 0.13 or newer (CI pins 0.13.1), GNU make, curl and git. uv installs the
Python of `.python-version`, the newest release the library supports, and the development tools locked in `uv.lock`
into `.venv/`; the library itself runs on Python 3.11 or newer. `make conformance` also needs Linux with Docker, since
the check reaches the example through the host's network, and a `python3` with `venv`, which makes the fresh virtual
environment the built wheel is installed into.

The Makefile is the only entry point:

| Target | Does |
|---|---|
| `make help` | lists the targets (the default goal) |
| `make fmt` | formats the code and sorts the imports (ruff) |
| `make lint` | checks the licence headers, that `uv.lock` is current, and the code with ruff (lint and format) and mypy in strict mode |
| `make test` | runs the tests with pytest and the coverage gate: 90 % of the package's statements, `models.py` excluded |
| `make generate` | generates `src/muster_bridge/models.py` from `api/bridge.yaml` with datamodel-code-generator |
| `make generate-check` | regenerates and fails when `src/muster_bridge/models.py` was not current |
| `make spec-check` | compares `api/bridge.yaml` byte for byte with the one on Muster's `master` (`MUSTER_SPEC_URL`) and prints the difference |
| `make spec-sync` | replaces `api/bridge.yaml` with Muster's and runs `make generate` |
| `make licenses` | fails on a runtime dependency whose licence is not allowed (pip-licenses over an environment with the runtime dependencies only) |
| `make build` | builds the sdist and the wheel into `dist/` with `uv build` |
| `make ci` | runs `lint`, `generate-check`, `spec-check`, `licenses`, `test` and `build` |
| `make conformance` | installs the built wheel beside uvicorn into a fresh virtual environment, runs `examples/memory_bridge.py` from it with `python -I`, and runs `muster bridge check` from Muster's development image against it three times |
| `make clean` | removes `bin/`, `dist/`, `.venv/`, the caches and the coverage data |

In CI, the `ci` workflow runs the jobs `lint`, `test` (on every Python release from 3.11 to 3.15), `generate`, `spec`,
`licenses`, `build` and `conformance` on every pull request, on every push to `master`, daily and by hand; the job `ci`
passes only when all of them passed, and the ruleset of `master` requires it. The `codeql` and `scorecard` workflows
analyse the code and the repository's practices.

## Branches, commits and pull requests

- Branch from `master`; a story's branch is `s-NNN-<slug>`, with the slug of its story file name.
- Commit messages are [conventional commits](https://www.conventionalcommits.org/) on one line:
  `<type>(<scope>): <description>`, with `feat`, `fix`, `docs`, `build`, `ci`, `test`, `refactor`, `perf` or `chore`.
  Say what changed; leave out story and phase IDs. `build` means a dependency change, the type Renovate uses: it shows
  under "Dependencies" in the changelog and starts a release. Work on the build system or the tooling uses `chore` or
  `ci`.
- No AI attribution: no `Co-Authored-By` trailers for tools and no "Generated with" lines, in commits or pull requests.
- Pull requests are squash-merged, so the title becomes the commit message, and release-please writes the changelog
  from it.
- The [pull request template](.github/pull_request_template.md) asks for the issue in Muster's repository
  (`Part of muster-io/muster#<issue>`: a pull request here does not close it, so it is closed by hand once the pull
  request merges), the story ID, a table with every acceptance statement and its evidence, the output of the story's
  `verify`, every change to the story file with its reason, the dependencies added and the maintainer's steps.

## Licence header

Every Python file, workflow, YAML and TOML file, the Renovate configuration and the Makefile start with these two
lines in their own comment syntax (`//` in `renovate.json5`):

```python
# SPDX-License-Identifier: MIT
# Copyright The Muster Authors
```

Markdown, JSON, `uv.lock`, `.python-version`, `.gitignore`, `py.typed`, the generated `src/muster_bridge/models.py`
(marked `# generated by datamodel-codegen`) and `api/bridge.yaml`, which states its licence in `info.license`, are
exempt. `make lint` names every file that lacks the header.

## Dependencies and third-party code

- At run time the library depends on Starlette, httpx and pydantic 2 only, with lower bounds and no upper ones. Every
  runtime dependency, transitive ones included, is under MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC,
  PSF-2.0 or MPL-2.0 unmodified; `make licenses` checks it. Development and build tools, in the dependency group `dev`
  and locked in `uv.lock`, may use any OSI-approved licence.
- A pull request that adds a dependency states its **licence**, its **latest release date** and its **maintenance
  status**. Prefer the standard library: the library needs no web framework beyond Starlette, no ORM and no logger
  other than `logging`.
- Never copy code from copyleft projects — Muster included, which is under the AGPL — into this repository; read them
  for behaviour only.

## Releases

release-please keeps a release pull request open, with the changelog written from the conventional commits on
`master`; it updates the version in `pyproject.toml`, `src/muster_bridge/__init__.py` and the project's entry in
`uv.lock`. Merging it creates the release and its `vX.Y.Z` tag; the workflow then builds the sdist and the wheel and
publishes them to PyPI by trusted publishing from the environment `pypi`, with attestations and no stored token.
Versions stay `0.x` until Muster 1.0.
