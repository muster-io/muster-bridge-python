# SPDX-License-Identifier: MIT
# Copyright The Muster Authors

.DEFAULT_GOAL := help

UV ?= uv
DOCKER ?= docker
CURL ?= curl
PYTHON3 ?= python3
RUN := $(UV) run --locked
BIN := bin

MUSTER_SPEC_URL ?= https://raw.githubusercontent.com/muster-io/muster/master/api/bridge.yaml
MUSTER_IMAGE ?= ghcr.io/muster-io/muster:edge
ALLOWED_LICENSES := MIT;MIT-0;BSD-2-Clause;BSD-3-Clause;Apache-2.0;ISC;PSF-2.0;MPL-2.0
MODELS := src/muster_bridge/models.py

EXAMPLE_LISTEN := 127.0.0.1:18095
PUSH_LISTEN := 127.0.0.1:18096
CHECK_FLAGS := --token-file /token --channel alerts --forbidden-channel nobot --handle alice
STEPS := info channel_check post replayed_key buttons edit thread_reply thread_ref forbidden_channel user_lookup \
	direct_message delete poll press push answer
RUN1_SKIP := press push
RUN2_SKIP := buttons user_lookup direct_message delete press push
RUN1_PASS := $(filter-out $(RUN1_SKIP),$(STEPS))
RUN2_PASS := $(filter-out $(RUN2_SKIP),$(STEPS))

# The options of datamodel-code-generator: pydantic v2 models for Python 3.11, with the constraints of the
# specification as Field arguments, names from the schemas' paths, the parameters of the operations as models of their
# own, and False as the default of truncated and replayed, which every answer carries. --ignore-pyproject keeps a
# [tool.datamodel-codegen] table from changing the output.
DATAMODEL_CODEGEN_FLAGS := --ignore-pyproject --input api/bridge.yaml --input-file-type openapi --output $(MODELS) \
	--output-model-type pydantic_v2.BaseModel --target-python-version 3.11 --formatters builtin \
	--disable-timestamp --field-constraints --use-double-quotes --use-default-kwarg --strict-nullable \
	--use-default --deserialize-default-values enum --naming-strategy full-path --use-operation-id-as-name \
	--openapi-scopes schemas parameters paths --include-path-parameters \
	--model-name-map '{"InfoEventDeliveryEnum":"InfoEventDelivery"}' \
	--default-values '{"CapabilitiesButtons.styles":[],"MessagePosted.truncated":false,"MessagePosted.replayed":false,"MessageEdited.truncated":false,"ThreadReplyPosted.truncated":false,"ThreadReplyPosted.replayed":false,"DirectMessageSent.replayed":false,"AnswerShown.replayed":false}'

.PHONY: help fmt lint test generate generate-check spec-check spec-sync licenses build ci conformance clean

help: ## List the targets
	@awk 'BEGIN { FS = ":.*## " } /^[a-z][a-z-]*:.*## / { printf "  %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

fmt: ## Format the code and sort the imports (ruff)
	$(RUN) ruff check --select I --fix .
	$(RUN) ruff format .

lint: ## Check the licence headers, uv.lock, and the code with ruff (lint and format) and mypy (strict)
	@failed=0; \
	sh -c "$$HEADER_CHECK_SH" || failed=1; \
	$(UV) lock --check || failed=1; \
	$(RUN) ruff check . || failed=1; \
	$(RUN) ruff format --check . || failed=1; \
	$(RUN) mypy || failed=1; \
	exit $$failed

test: ## Run the tests with the coverage gate of 90 % of the package's statements, models.py excluded
	$(RUN) coverage erase
	$(RUN) coverage run -m pytest
	$(RUN) coverage report

generate: ## Generate src/muster_bridge/models.py from api/bridge.yaml
	$(RUN) datamodel-codegen $(DATAMODEL_CODEGEN_FLAGS)

generate-check: ## Fail when src/muster_bridge/models.py is not what make generate makes of api/bridge.yaml
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	cp $(MODELS) "$$tmp" 2>/dev/null || : > "$$tmp"; \
	$(MAKE) --no-print-directory generate || exit 1; \
	if cmp -s "$$tmp" $(MODELS); then \
		echo "generated files are current"; \
	else \
		printf 'stale generated files:\n  $(MODELS)\nrun make generate and commit the result\n' >&2; \
		exit 1; \
	fi

spec-check: ## Compare api/bridge.yaml byte for byte with the one on Muster's master (MUSTER_SPEC_URL)
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	if ! $(CURL) -fsSL --retry 3 "$(MUSTER_SPEC_URL)" -o "$$tmp"; then \
		echo "cannot download $(MUSTER_SPEC_URL)" >&2; \
		exit 1; \
	fi; \
	if cmp -s "$$tmp" api/bridge.yaml; then \
		echo "api/bridge.yaml is the copy of $(MUSTER_SPEC_URL)"; \
		exit 0; \
	fi; \
	diff -u --label "$(MUSTER_SPEC_URL)" --label api/bridge.yaml "$$tmp" api/bridge.yaml; \
	echo "api/bridge.yaml differs from $(MUSTER_SPEC_URL): run make spec-sync" >&2; \
	exit 1

spec-sync: ## Replace api/bridge.yaml with the one on Muster's master (MUSTER_SPEC_URL) and regenerate
	@tmp=$$(mktemp); \
	if ! $(CURL) -fsSL --retry 3 "$(MUSTER_SPEC_URL)" -o "$$tmp"; then \
		rm -f "$$tmp"; \
		echo "cannot download $(MUSTER_SPEC_URL)" >&2; \
		exit 1; \
	fi; \
	chmod 0644 "$$tmp" && mkdir -p api && mv "$$tmp" api/bridge.yaml
	@$(MAKE) --no-print-directory generate

licenses: ## Fail on a runtime dependency whose licence is not in ALLOWED_LICENSES (pip-licenses)
	@tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	$(UV) export --locked --quiet --no-dev --no-emit-project --no-hashes --format requirements.txt \
		-o "$$tmp/requirements.txt" && \
	$(UV) venv --quiet "$$tmp/venv" && \
	$(UV) pip install --quiet --python "$$tmp/venv/bin/python" -r "$$tmp/requirements.txt" && \
	$(RUN) pip-licenses --python "$$tmp/venv/bin/python" --from=meta --format=plain \
		--allow-only="$(ALLOWED_LICENSES)"

build: ## Build the sdist and the wheel into dist/
	rm -rf dist
	$(UV) build --out-dir dist

ci: lint generate-check spec-check licenses test build ## The checks of a pull request, locally (conformance runs in its own CI job)

conformance: ## Run muster bridge check from MUSTER_IMAGE against examples/memory_bridge.py three times (Linux, Docker)
	@sh -c "$$CONFORMANCE_SH"

clean: ## Remove bin/, dist/, .venv/, the caches and the coverage data
	rm -rf $(BIN) dist .venv .mypy_cache .ruff_cache .pytest_cache .coverage

# The licence header check of lint: every Python file, workflow, YAML file, TOML file, Renovate configuration and the
# Makefile start with the MIT header; the generated models and the copy api/bridge.yaml are exempt.
define HEADER_CHECK_SH
missing=$$(find . \( -path ./.git -o -path ./bin -o -path ./dist -o -path ./.venv -o -name __pycache__ \
	-o -name .mypy_cache -o -name .ruff_cache -o -name .pytest_cache \) -prune -o -type f \
	\( -name '*.py' -o -name '*.yml' -o -name '*.yaml' -o -name '*.toml' -o -name '*.json5' -o -name Makefile \) \
	-print | sort |
	while IFS= read -r f; do
		[ "$$f" = ./api/bridge.yaml ] && continue
		head -n 5 "$$f" | grep -q '^# generated by datamodel-codegen' && continue
		if ! head -n 5 "$$f" | grep -q 'SPDX-License-Identifier: MIT' ||
			! head -n 5 "$$f" | grep -q 'Copyright The Muster Authors'; then
			echo "$$f"
		fi
	done)
if [ -n "$$missing" ]; then
	echo "files without the licence header (SPDX-License-Identifier: MIT, Copyright The Muster Authors):"
	echo "$$missing" | sed 's/^/  /'
	exit 1
fi
endef
export HEADER_CHECK_SH

# The conformance check: the built wheel, installed with pip into a fresh virtual environment beside the uvicorn of
# uv.lock, runs examples/memory_bridge.py with python -I, so that it imports the installed package and not src/, against
# muster bridge check of MUSTER_IMAGE, three times, every run with the steps it must pass and skip. Docker's host network
# makes it Linux only.
define CONFORMANCE_SH
unset BRIDGE_FEATURES
dir=$(abspath $(BIN))/conformance
rm -rf "$$dir" && mkdir -p "$$dir" && chmod 0755 "$$dir" || exit 1
failed=0
pid=
check=
cleanup() {
	if [ -n "$$check" ]; then kill "$$check" 2>/dev/null; wait "$$check" 2>/dev/null; fi
	if [ -n "$$pid" ]; then kill "$$pid" 2>/dev/null; wait "$$pid" 2>/dev/null; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

$(MAKE) --no-print-directory build || exit 1
set -- dist/muster_bridge-*-py3-none-any.whl
if [ "$$#" -ne 1 ] || [ ! -f "$$1" ]; then echo "conformance: dist/ holds no single wheel"; exit 1; fi
wheel=$(abspath .)/$$1
uvicorn=$$($(PYTHON3) -I -c 'import sys, tomllib; print(next(p["version"] for p in tomllib.load(open(sys.argv[1], "rb"))["package"] if p["name"] == "uvicorn"))' uv.lock) || exit 1
$(PYTHON3) -m venv "$$dir/venv" || exit 1
"$$dir/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$$wheel" "uvicorn==$$uvicorn" || exit 1
"$$dir/venv/bin/python" -I -c 'import muster_bridge, uvicorn; print("muster_bridge", muster_bridge.__version__, "from", muster_bridge.__file__, "uvicorn", uvicorn.__version__)' || exit 1
python=$$dir/venv/bin/python
example=$(abspath examples/memory_bridge.py)
$(DOCKER) pull --quiet $(MUSTER_IMAGE) || exit 1
echo "muster image: $$($(DOCKER) image inspect --format '{{index .RepoDigests 0}}' $(MUSTER_IMAGE))"
$(DOCKER) run --rm $(MUSTER_IMAGE) version || exit 1

printf 'muster-bridge-python-%s\n' "$$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" > "$$dir/token"
printf 'whsec_%s\n' "$$(head -c 32 /dev/urandom | base64 | tr -d '\n')" > "$$dir/push-secret"
chmod 0644 "$$dir/token" "$$dir/push-secret"

# start <name> <command…>: start the example — after env and variables if any — with the token file and the listen
# address appended, and wait until it answers.
start() {
	name=$$1
	shift
	"$$@" --token-file "$$dir/token" --listen $(EXAMPLE_LISTEN) > "$$dir/$$name-example.log" 2>&1 &
	pid=$$!
	n=0
	while [ "$$($(CURL) -s -o /dev/null -w '%{http_code}' http://$(EXAMPLE_LISTEN)/v1/info)" != 401 ]; do
		n=$$((n + 1))
		if [ "$$n" -ge 150 ]; then
			echo "conformance $$name: the example did not start"
			cat "$$dir/$$name-example.log"
			return 1
		fi
		sleep 0.1
	done
}

stop() {
	if [ -n "$$pid" ]; then kill "$$pid" 2>/dev/null; wait "$$pid" 2>/dev/null; fi
	pid=
}

sorted() { printf '%s\n' $$1 | sed '/^$$/d' | sort | tr '\n' ' '; }

# verify <name> <status> <pass> <skip>: compare the check's lines with the steps it must pass and skip.
verify() {
	name=$$1
	status=$$2
	gotpass=$$(sorted "$$(sed -n 's/^PASS \([a-z_]*\)$$/\1/p' "$$dir/$$name.out")")
	gotskip=$$(sorted "$$(sed -n 's/^SKIP \([a-z_]*\): .*$$/\1/p' "$$dir/$$name.out")")
	wantpass=$$(sorted "$$3")
	wantskip=$$(sorted "$$4")
	if [ "$$status" -ne 0 ] || grep -q '^FAIL ' "$$dir/$$name.out" ||
		[ "$$gotpass" != "$$wantpass" ] || [ "$$gotskip" != "$$wantskip" ]; then
		echo "conformance $$name: exit $$status; PASS [$$gotpass] want [$$wantpass]; SKIP [$$gotskip] want [$$wantskip]"
		echo "--- muster bridge check, stderr:"
		cat "$$dir/$$name.err"
		echo "--- the example's log:"
		cat "$$dir/$$name-example.log"
		failed=1
	else
		echo "conformance $$name: passed"
	fi
}

# unattended <name> <pass> <skip>: run the check with --no-interactive and verify its lines.
unattended() {
	$(DOCKER) run --rm --network host -v "$$dir/token:/token:ro" $(MUSTER_IMAGE) bridge check \
		http://$(EXAMPLE_LISTEN) $(CHECK_FLAGS) --no-interactive > "$$dir/$$1.out" 2> "$$dir/$$1.err"
	status=$$?
	cat "$$dir/$$1.out"
	verify "$$1" "$$status" "$$2" "$$3"
}

# interactive <name>: run the check with a person and its push listener; the person presses the message that the WAIT
# press line names, as alice, through the example's POST /demo/press. Every step must pass.
interactive() {
	$(DOCKER) run --rm --network host -v "$$dir/token:/token:ro" -v "$$dir/push-secret:/push-secret:ro" \
		$(MUSTER_IMAGE) bridge check http://$(EXAMPLE_LISTEN) $(CHECK_FLAGS) \
		--push-listen $(PUSH_LISTEN) --push-secret-file /push-secret > "$$dir/$$1.out" 2> "$$dir/$$1.err" &
	check=$$!
	id=
	n=0
	while [ "$$n" -lt 1200 ]; do
		id=$$(sed -n 's/^WAIT press: .*, message_id \(.*\)$$/\1/p' "$$dir/$$1.out")
		if [ -n "$$id" ] || ! kill -0 "$$check" 2>/dev/null; then break; fi
		n=$$((n + 1))
		sleep 0.1
	done
	if [ -n "$$id" ]; then
		code=$$($(CURL) -sS -X POST -H 'Content-Type: application/json' \
			-d "$$(printf '{"message_id":"%s","user":"alice"}' "$$id")" \
			-o "$$dir/$$1.press" -w '%{http_code}' http://$(EXAMPLE_LISTEN)/demo/press)
		case $$code in
		2??) ;;
		*) echo "conformance $$1: the press answered $$code: $$(cat "$$dir/$$1.press" 2>/dev/null)" ;;
		esac
	else
		echo "conformance $$1: no WAIT press line"
	fi
	wait "$$check"
	status=$$?
	check=
	cat "$$dir/$$1.out"
	verify "$$1" "$$status" "$(STEPS)" ""
}

echo "== memory 1: every feature, --no-interactive"
if start memory-all "$$python" -I "$$example"; then
	unattended memory-all "$(RUN1_PASS)" "$(RUN1_SKIP)"
else
	failed=1
fi
stop

echo "== memory 2: BRIDGE_FEATURES=thread_messages, --no-interactive"
if start memory-thread-messages env BRIDGE_FEATURES=thread_messages "$$python" -I "$$example"; then
	unattended memory-thread-messages "$(RUN2_PASS)" "$(RUN2_SKIP)"
else
	failed=1
fi
stop

echo "== memory 3: every feature, interactive, the example pushing to the check's listener"
if start memory-interactive "$$python" -I "$$example" --push-url http://$(PUSH_LISTEN)/ \
	--push-secret-file "$$dir/push-secret"; then
	interactive memory-interactive
else
	failed=1
fi
stop

exit $$failed
endef
export CONFORMANCE_SH
