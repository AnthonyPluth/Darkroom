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
	api-contract api-contract-check frontend-install frontend-check frontend-typecheck frontend-lint frontend-test frontend-build

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
