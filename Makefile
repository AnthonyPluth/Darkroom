# `make check` runs, once each: ruff, mypy, import-linter, Darkroom's own Semgrep rules, the fleet checks, the feature
# map's and API contract's checks, the Python tests in parallel with the coverage floor, and the web app's checks
# (svelte-check, ESLint, Vitest with the coverage floor in frontend/vite.config.ts, and the build into
# darkroom/static/app). The security scans (Semgrep's registry packs, npm audit, Trivy, zizmor, pip-audit) run only in CI.
# Python goes through Poetry; the web app through npm (Node 22, frontend/.nvmrc).

PYTHON ?= poetry run python
RUFF ?= poetry run ruff
MYPY ?= poetry run mypy
LINT_IMPORTS ?= poetry run lint-imports
UNITTEST_PARALLEL_BASE ?= poetry run unittest-parallel -t . -s tests
UNITTEST_PARALLEL ?= $(UNITTEST_PARALLEL_BASE) -j 4
SEMGREP ?= semgrep
FLEET ?= tools/fleet
NPM ?= npm

.PHONY: setup check lint python-lint semgrep test test-parallel fix fleet-checks fleet-sync feature-map feature-map-check \
	api-contract api-contract-check frontend-install frontend-check frontend-typecheck frontend-lint frontend-test frontend-build \
	verify verify-skill verify-skill-print user-feature-map control-smoke

check: lint feature-map-check api-contract-check fleet-checks test-parallel frontend-check

lint: python-lint semgrep

python-lint:
	$(RUFF) check .
	$(MYPY)
	$(LINT_IMPORTS)
	$(FLEET) no-comments

semgrep:
	$(PYTHON) .semgrep/check_examples.py $(SEMGREP)
	$(SEMGREP) scan --metrics=off --disable-version-check --error --config .semgrep/darkroom.yml darkroom darkroom_browser run.py

test:
	$(PYTHON) -m unittest discover tests

test-parallel:
	$(UNITTEST_PARALLEL) --coverage --coverage-rcfile pyproject.toml
	poetry run coverage json -q -o coverage.json
	$(FLEET) coverage-floor coverage.json

fix:
	$(RUFF) check --fix .

setup:
	poetry install --no-root --no-interaction

# The checks that replaced instructions (`fleet checks`, configured by [tool.fleet] in pyproject.toml, plus Darkroom's own in
# tools/app_checks.py): one Alembic head, a test for each new migration, the workflows' conventions, the skills, and the
# Co-Authored-By trailer on your commits since main, as CI checks a pull request's. Then `fleet drift`: the AGENTS.md
# blocks, the vendored .claude/ files and tools/fleet are fleet's at .fleet/version.
fleet-checks:
	$(FLEET) checks --commits origin/main..HEAD
	$(FLEET) drift

# Moves fleet's content here (the AGENTS.md blocks, .claude/skills/fleet-*, .claude/agents/fleet-reviewer.md,
# .claude/hooks/fleet-session-start.sh, tools/fleet and .fleet/version) to a fleet release: `make fleet-sync version=v1.2.3`.
# The workflow pins in .github/workflows/ move with Dependabot, and fleet-sync.yml normally runs this after they do.
fleet-sync:
	@test -n "$(version)" || { echo "Say which release: make fleet-sync version=v1.2.3" >&2; exit 1; }
	$(FLEET) sync --to $(version)

feature-map:
	$(PYTHON) tools/feature_map.py

feature-map-check:
	$(PYTHON) tools/feature_map.py --check

api-contract:
	$(PYTHON) tools/api_contract.py

api-contract-check:
	$(PYTHON) tools/api_contract.py --check

frontend-install:
	cd frontend && $(NPM) ci --no-audit --no-fund

frontend/node_modules: frontend/package-lock.json
	cd frontend && $(NPM) ci --no-audit --no-fund
	touch frontend/node_modules

frontend-check: frontend-typecheck frontend-lint frontend-test frontend-build

frontend-typecheck: frontend/node_modules
	cd frontend && $(NPM) run typecheck

frontend-lint: frontend/node_modules
	cd frontend && $(NPM) run lint

frontend-test: frontend/node_modules
	cd frontend && $(NPM) run coverage

frontend-build: frontend/node_modules
	cd frontend && $(NPM) run build

# Every page of the web app at phone (440x956) and desktop (1280x800) width, on made-up demo data served by tools/verify.py
# under libfaketime (so the clock is fixed), with the scripted flows in frontend/verify/flows and an accessibility audit (axe
# WCAG 2.2 AA, 44 px tap targets at phone width, a visible focus ring). Full-page images go to docs/screenshots/ (committed: they
# are the visual baselines, and the only screenshots a pull request may carry); the report and the flows' images go to
# artifacts/verify/ (ignored). It fails on a console error, a 5xx, a request to any address but its own server, or an
# accessibility problem. It runs VERIFY_JOBS servers at once (default: the CPU count, at most 4).
# `make verify PAGES="status people"` visits only those pages.
verify: frontend/node_modules frontend-build
	$(PYTHON) tools/verify.py run $(PAGES)

# tools/control's command reference in the verify-darkroom skill (.claude/skills/verify-darkroom/references/cli.md), generated
# from the CLI's own help. make fleet-checks fails when it is out of date.
verify-skill:
	node frontend/verify/control.mjs help --markdown > .claude/skills/verify-darkroom/references/cli.md

verify-skill-print:
	@node frontend/verify/control.mjs help --markdown

# What a person can do, by page and flow (.claude/skills/verify-darkroom/references/feature-map.md), generated from the verify
# pages and flows plus references/feature-gotchas.md (hand-edited). make fleet-checks fails when it is out of date.
user-feature-map:
	$(PYTHON) tools/user_feature_map.py

# tools/control end to end on the real demo server under the frozen clock: doctor, start, snapshot, screenshot, flows, reset and
# stop (frontend/verify/control-smoke.mjs; the Control smoke workflow runs it too). Needs what make verify needs; writes only
# under artifacts/control-smoke/.
control-smoke: frontend/node_modules frontend-build
	DARKROOM_PYTHON="$$($(PYTHON) -c 'import sys; print(sys.executable)')" node frontend/verify/control-smoke.mjs
