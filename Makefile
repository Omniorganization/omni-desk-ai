.PHONY: test test-fast test-security test-strict test-ci readiness init-production-config compose-smoke strict-sandbox-smoke monorepo-layout release-channel-policy tri-app-contract tri-app-test-web tri-app-build-web tri-app-test-desktop tri-app-build-desktop tri-app-test-flutter tri-app-rust-check tri-app-quality tri-app-release-web tri-app-release-desktop tri-app-release-mobile tri-app-release-builds tri-app-release-preflight ios-real-device-evidence-import tri-app-live-smoke-preflight workflow-governance-preflight distribution-package-manifest package-final-gate external-ga-evidence-audit external-ga-evidence-gate release-external-ga-evidence distribution-ga-preflight

PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
PUBLIC_BASE_URL ?= https://omnidesk.company.example.invalid
SANDBOX_IMAGE ?= python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
RUNNER_URL ?= http://sandbox-runner:18890
IOS_EVIDENCE_RAW_DIR ?= /tmp/omnidesk-ios-real-device-evidence
IOS_EVIDENCE_EXPECTED_VERSION ?= 1.12.5+root-monorepo-production-ga-candidate
PACKAGE_DIR ?= dist/package
DISTRIBUTION_PACKAGE_VERSION ?= 1.12.5+root-monorepo-production-ga-candidate
DISTRIBUTION_PACKAGE_SLUG ?= Omni-desk-AI-1.12.5-root-monorepo-production-ga-candidate
DISTRIBUTION_SOURCE_COMMIT ?= unknown
RELEASE_CHANNEL ?= candidate

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

monorepo-layout:
	$(PYTHON) scripts/check_monorepo_layout.py .

release-channel-policy:
	$(PYTHON) scripts/check_release_channel_policy.py .

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
	$(PYTHON) scripts/check_release_configuration.py --scope web-admin --format json --report-path dist/web-admin-preflight.json
	$(PYTHON) scripts/check_release_configuration.py --scope desktop --format json --report-path dist/desktop-preflight.json
	$(PYTHON) scripts/check_release_configuration.py --scope mobile --format json --report-path dist/mobile-preflight.json
	$(PYTHON) scripts/check_release_configuration.py --scope tri-app --format json --report-path dist/tri-app-preflight.json

ios-real-device-evidence-import:
	IOS_EVIDENCE_RAW_DIR="$(IOS_EVIDENCE_RAW_DIR)" IOS_EVIDENCE_EXPECTED_VERSION="$(IOS_EVIDENCE_EXPECTED_VERSION)" $(PYTHON) scripts/check_release_configuration.py --scope ios-evidence --format json --report-path dist/ios-evidence-preflight.json
	$(PYTHON) scripts/import_ios_real_device_evidence.py --raw-dir "$(IOS_EVIDENCE_RAW_DIR)" --expected-version "$(IOS_EVIDENCE_EXPECTED_VERSION)" --copy --write-report release/ios-real-device-evidence-import-report.json
	$(PYTHON) scripts/check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.5.json

tri-app-live-smoke-preflight:
	$(PYTHON) scripts/check_release_configuration.py --scope tri-app-live-smoke --format json --report-path dist/tri-app-live-smoke-preflight.json
	$(PYTHON) scripts/import_tri_app_live_smoke_evidence.py --report "$${TRI_APP_LIVE_SMOKE_REPORT_PATH}" --expected-org-id "$${TRI_APP_ORG_ID}" --expected-scenario-id "$${TRI_APP_LIVE_SMOKE_SCENARIO_ID}" --copy --write-report release/tri-app-live-smoke-evidence-import-report.json

workflow-governance-preflight:
	$(PYTHON) scripts/check_workflow_governance.py . --require-real-workflows

distribution-package-manifest:
	$(PYTHON) scripts/write_distribution_manifest.py --package-dir "$(PACKAGE_DIR)" --version "$(DISTRIBUTION_PACKAGE_VERSION)" --package-slug "$(DISTRIBUTION_PACKAGE_SLUG)" --source-commit "$(DISTRIBUTION_SOURCE_COMMIT)" --external-audit release/real-ga-evidence-audit-1.12.5.json --output release-manifest.json
	$(PYTHON) scripts/write_distribution_manifest.py --package-dir "$(PACKAGE_DIR)" --verify --manifest release-manifest.json

package-final-gate: distribution-package-manifest
	$(PYTHON) scripts/check_release_hygiene.py "$(PACKAGE_DIR)"
	$(PYTHON) scripts/write_portable_sha256s.py --base-dir "$(PACKAGE_DIR)" --output SHA256SUMS.txt --verify
	$(PYTHON) scripts/write_distribution_manifest.py --package-dir "$(PACKAGE_DIR)" --verify --manifest release-manifest.json

external-ga-evidence-audit:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.5.json

external-ga-evidence-gate:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/check_external_ga_evidence.py .

release-external-ga-evidence:
	@if [ "$(RELEASE_CHANNEL)" = "real-ga" ]; then \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/check_external_ga_evidence.py . --write-report release/real-ga-evidence-audit-1.12.5.json; \
	elif [ "$(RELEASE_CHANNEL)" = "candidate" ]; then \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.5.json; \
	else \
		echo "RELEASE_CHANNEL must be candidate or real-ga" >&2; exit 64; \
	fi

distribution-ga-preflight: tri-app-release-preflight ios-real-device-evidence-import tri-app-live-smoke-preflight workflow-governance-preflight external-ga-evidence-gate
