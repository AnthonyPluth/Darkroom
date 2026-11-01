# `make check` runs, once each: ruff, mypy, import-linter, Darkroom's own Semgrep rules, the fleet checks, the feature
# map's and API contract's checks, the Python tests in parallel with the coverage floor, and the web app's checks once
# frontend/package.json exists. The security scans (Semgrep's registry packs, Trivy, zizmor, pip-audit, CodeQL) run
# only in CI. Python goes through Poetry.

PYTHON ?= poetry run python
RUFF ?= poetry run ruff
MYPY ?= poetry run mypy
LINT_IMPORTS ?= poetry run lint-imports
UNITTEST_PARALLEL_BASE ?= poetry run unittest-parallel -t . -s tests
UNITTEST_PARALLEL ?= $(UNITTEST_PARALLEL_BASE) -j 4
SEMGREP ?= semgrep

.PHONY: setup check lint python-lint semgrep test test-parallel fix fleet-checks feature-map feature-map-check \
	api-contract api-contract-check frontend-check

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

frontend-check:
	@if [ -f frontend/package.json ]; then cd frontend && npm run check; else echo "No frontend yet (frontend/package.json arrives with the frontend scaffold)."; fi
