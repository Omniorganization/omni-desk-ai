from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_web_admin_uses_request_scoped_nonce_csp_without_unsafe_inline() -> None:
    middleware = text("apps/web-admin-next/middleware.ts")
    next_config = text("apps/web-admin-next/next.config.mjs")
    assert "crypto.randomUUID()" in middleware
    assert "x-nonce" in middleware
    assert "Content-Security-Policy" in middleware
    assert "script-src 'self' 'nonce-${nonce}' 'strict-dynamic'" in middleware
    assert "style-src 'self' 'nonce-${nonce}'" in middleware
    assert "'unsafe-inline'" not in middleware
    assert "Content-Security-Policy" not in next_config
    assert not (ROOT / "apps/web-admin-next/proxy.ts").exists()


def test_playwright_production_browser_gate_is_present_and_pinned() -> None:
    workflow = text(".github/workflows/web-admin-browser-e2e.yml")
    config = text("apps/web-admin-e2e/playwright.config.ts")
    browser_test = text("apps/web-admin-e2e/tests/csp-production.spec.ts")
    package = json.loads(text("apps/web-admin-e2e/package.json"))
    assert package["devDependencies"]["@playwright/test"] == "1.61.1"
    assert "@playwright/test@1.61.1" in workflow
    assert "playwright install --with-deps chromium" in workflow
    assert "NODE_ENV: 'production'" in config
    assert "securitypolicyviolation" in browser_test
    assert "consoleErrors" in browser_test
    assert "pageErrors" in browser_test
    assert "toBeDisabled" in browser_test


def test_expanded_type_gate_covers_appsync_models_and_server() -> None:
    config = json.loads(text("pyrightconfig.industrial.json"))
    workflow = text(".github/workflows/industrial-type-gate.yml")
    assert "omnidesk_agent/appsync" in config["include"]
    assert "omnidesk_agent/models" in config["include"]
    assert "omnidesk_agent/server.py" in config["include"]
    assert config["reportMissingImports"] == "error"
    assert config["reportGeneralTypeIssues"] == "error"
    assert "pyright --project pyrightconfig.industrial.json" in workflow
    assert "Private Request body/json mutation is forbidden" in workflow


def test_critical_coverage_gates_are_raised_to_security_thresholds() -> None:
    source = text("scripts/check_coverage_gates.py")
    assert '"omnidesk_agent/security/": 90.0' in source
    assert '"omnidesk_agent/security/resource_guard.py": 95.0' in source
    assert '"omnidesk_agent/security/admin_auth.py": 90.0' in source
