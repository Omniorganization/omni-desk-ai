.PHONY: test test-fast test-security test-strict test-ci readiness init-production-config compose-smoke strict-sandbox-smoke web-admin-container-hardening tri-app-contract tri-app-test-web tri-app-build-web tri-app-test-desktop tri-app-build-desktop tri-app-test-flutter tri-app-rust-check tri-app-quality tri-app-release-web tri-app-release-desktop tri-app-release-mobile tri-app-release-builds tri-app-release-preflight external-ga-evidence-audit external-ga-evidence-gate distribution-ga-preflight

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

web-admin-container-hardening:
	$(PYTHON) scripts/check_web_admin_container_hardening.py .

production-closure:
	python scripts/check_kubernetes_contract.py .
	python scripts/production_closure_drill.py --root . --contract-only

tri-app-contract:
	PYTHONPATH=. $(PYTEST) -q tests/test_tri_app_foundation.py

tri-app-test-web:
	cd apps/web-admin-next && npm test && npm run typecheck

tri-app-build-web:
	cd apps/web-admin-next && npm run build

tri-app-test-desktop:
	cd apps/desktop-tauri && npm test && npm run typecheck

tri-app-build-desktop:
	cd apps/desktop-tauri && npm run build

tri-app-test-flutter:
	cd apps/mobile-flutter && flutter pub get
	cd apps/mobile-flutter && dart analyze
	cd apps/mobile-flutter && flutter test

tri-app-rust-check:
	cd apps/desktop-tauri/src-tauri && cargo generate-lockfile
	cd apps/desktop-tauri/src-tauri && cargo check --locked

tri-app-quality: tri-app-contract tri-app-test-web tri-app-build-web tri-app-test-desktop tri-app-build-desktop
	@if command -v flutter >/dev/null; then $(MAKE) tri-app-test-flutter; else echo "flutter not found; skipping Flutter tests"; fi
	@if command -v cargo >/dev/null; then $(MAKE) tri-app-rust-check; else echo "cargo not found; skipping Tauri Rust check"; fi

tri-app-release-web:
	$(PYTHON) scripts/check_web_admin_container_hardening.py .
	cd apps/web-admin-next && npm ci
	cd apps/web-admin-next && npm run typecheck
	cd apps/web-admin-next && npm test
	cd apps/web-admin-next && npm run build

tri-app-release-desktop:
	cd apps/desktop-tauri && npm ci
	cd apps/desktop-tauri && npm run typecheck
	cd apps/desktop-tauri && npm test
	cd apps/desktop-tauri && npm run build
	cd apps/desktop-tauri/src-tauri && cargo generate-lockfile
	cd apps/desktop-tauri/src-tauri && cargo check --locked

tri-app-release-mobile:
	cd apps/mobile-flutter && flutter pub get
	cd apps/mobile-flutter && dart analyze
	cd apps/mobile-flutter && flutter test
	cd apps/mobile-flutter && flutter build appbundle --release
	cd apps/mobile-flutter && flutter build ipa --release

tri-app-release-builds: tri-app-release-web tri-app-release-desktop tri-app-release-mobile

tri-app-release-preflight:
	$(PYTHON) scripts/check_tri_app_release_readiness.py . --mode release

external-ga-evidence-audit:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.11.json

external-ga-evidence-gate:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/check_external_ga_evidence.py .

distribution-ga-preflight: tri-app-release-preflight external-ga-evidence-gate
