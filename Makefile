.PHONY: test test-fast test-security test-strict test-ci readiness init-production-config compose-smoke strict-sandbox-smoke

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
PUBLIC_BASE_URL ?= https://omnidesk.company.example.invalid
SANDBOX_IMAGE ?= python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
RUNNER_URL ?= http://sandbox-runner:18890

test:
	PYTHONPATH=. $(PYTEST) -q

test-fast:
	PYTHONPATH=. $(PYTEST) -q -m "not slow and not e2e and not soak"

test-security:
	PYTHONPATH=. $(PYTEST) -q -m "security" || test $$? -eq 5

test-strict:
	PYTHONPATH=. $(PYTEST) -q -W error

readiness:
	PYTHONPATH=. $(PYTHON) scripts/check_version_consistency.py .
	PYTHONPATH=. $(PYTHON) scripts/check_deployment_readiness.py --compose-file deploy/docker/docker-compose.full.yml
	PYTHONPATH=. $(PYTHON) scripts/check_supply_chain_standard.py .
	PYTHONPATH=. $(PYTHON) scripts/check_observability_contract.py .

test-ci:
	$(PYTHON) -m pip install --require-hashes -r requirements.dev.lock
	PYTHONPATH=. $(PYTEST) -q -W error --timeout=60 --timeout-method=thread
	PYTHONPATH=. $(PYTEST) --cov=omnidesk_agent --cov-report=term-missing --cov-report=xml --cov-report=json --cov-fail-under=80 -q --timeout=60 --timeout-method=thread
	PYTHONPATH=. $(PYTHON) scripts/check_coverage_gates.py

init-production-config:
	$(PYTHON) scripts/init_production_config.py --public-base-url "$(PUBLIC_BASE_URL)" --sandbox-image "$(SANDBOX_IMAGE)" --runner-url "$(RUNNER_URL)"

compose-smoke:
	docker compose -f deploy/docker/docker-compose.yml config >/dev/null
	docker compose -f deploy/sandbox-runner/docker-compose.yml config >/dev/null
	$(PYTHON) scripts/production_smoke_test.py

strict-sandbox-smoke:
	$(PYTHON) scripts/production_smoke_test.py --sandbox-only --strict-sandbox

production-closure:
	python scripts/check_kubernetes_contract.py .
	python scripts/production_closure_drill.py --root . --contract-only
