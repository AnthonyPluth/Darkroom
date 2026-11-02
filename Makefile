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
NPM ?= npm

.PHONY: setup check lint python-lint semgrep test test-parallel fix fleet-checks feature-map feature-map-check \
	api-contract api-contract-check frontend-install frontend-check frontend-typecheck frontend-lint frontend-test frontend-build

check: lint feature-map-check api-contract-check fleet-checks test-parallel frontend-check

lint: python-lint semgrep

python-lint:
	$(RUFF) check .
	$(MYPY)
	$(LINT_IMPORTS)
	$(PYTHON) tools/no_comments.py

semgrep:
	$(PYTHON) .semgrep/check_examples.py $(SEMGREP)
	$(SEMGREP) scan --metrics=off --disable-version-check --error --config .semgrep/darkroom.yml darkroom darkroom_browser run.py

test:
	$(PYTHON) -m unittest discover tests

test-parallel:
	$(UNITTEST_PARALLEL) --coverage --coverage-rcfile pyproject.toml
	poetry run coverage json -q -o coverage.json
	$(PYTHON) tools/coverage_floor.py coverage.json

fix:
	$(RUFF) check --fix .

setup:
	poetry install --no-root --no-interaction

fleet-checks:
	$(PYTHON) tools/fleet_checks.py

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
