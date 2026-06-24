#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HEX_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"missing required file: {path}")
    return path.read_text(encoding="utf-8")


def _project_version(pyproject: str) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    if not match:
        raise RuntimeError("pyproject.toml does not declare a project version")
    return match.group(1)


def _run(cmd: list[str], cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{completed.stdout}\n{completed.stderr}")


def _check(condition: bool, message: str, failures: list[str], ok: list[str]) -> None:
    (ok if condition else failures).append(message)


def _native_version(full_version: str) -> str:
    app_version = full_version.split("+", 1)[0]
    parts = app_version.split(".")
    if len(parts) == 2:
        return f"{app_version}.0"
    return app_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GA release gate for OmniDesk production GA closure source/release trees.")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--release-metadata", help="Optional dist/release_metadata.json. When present, image.digest must be the final OCI digest.")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    failures: list[str] = []
    ok: list[str] = []

    try:
        _run([sys.executable, "scripts/check_version_consistency.py", "."], root)
        ok.append("version consistency gate passes")
    except Exception as exc:
        failures.append(str(exc))

    try:
        _run([sys.executable, "scripts/check_release_hygiene.py", ".", "--allow-vcs"], root)
        ok.append("release hygiene gate passes")
    except Exception as exc:
        failures.append(str(exc))

    try:
        _run([sys.executable, "scripts/check_release_channel_policy.py", "."], root)
        ok.append("release channel policy gate passes")
    except Exception as exc:
        failures.append(str(exc))

    try:
        _run([sys.executable, "scripts/check_ci_evidence_contract.py", "."], root)
        ok.append("CI evidence contract gate passes")
    except Exception as exc:
        failures.append(str(exc))

    try:
        _run([sys.executable, "scripts/check_security_workflow_policy.py", "."], root)
        ok.append("security workflow policy gate passes")
    except Exception as exc:
        failures.append(str(exc))

    try:
        _run([sys.executable, "scripts/check_production_install_policy.py", "."], root)
        ok.append("production install policy gate passes")
    except Exception as exc:
        failures.append(str(exc))

    pyproject = _read(root / "pyproject.toml")
    version = _project_version(pyproject)
    chart_version = _native_version(version)
    init = _read(root / "omnidesk_agent" / "__init__.py")
    chart = _read(root / "deploy/kubernetes/helm/omnidesk/Chart.yaml")
    values = _read(root / "deploy/kubernetes/helm/omnidesk/values.yaml")
    deployment = _read(root / "deploy/kubernetes/helm/omnidesk/templates/deployment.yaml")
    web_csp = _read(root / "apps/web-admin-next/next.config.mjs")
    routes = _read(root / "omnidesk_agent/appsync/routes.py")
    config = _read(root / "omnidesk_agent/config.py")
    production_validator = _read(root / "omnidesk_agent/validation/production.py")
    resource_guard = _read(root / "omnidesk_agent/security/resource_guard.py")
    desktop_app = _read(root / "apps/desktop-tauri/src/App.tsx")
    desktop_identity = _read(root / "apps/desktop-tauri/src/deviceIdentity.ts")
    mobile_main = _read(root / "apps/mobile-flutter/lib/main.dart")
    mobile_identity = _read(root / "apps/mobile-flutter/lib/device_identity.dart")
    mobile_pubspec = _read(root / "apps/mobile-flutter/pubspec.yaml")
    docker_prod_config = _read(root / "deploy/docker/config.production.example.yaml")
    postgres_store = _read(root / "omnidesk_agent/appsync/postgres_store.py")
    release_workflow = _read(root / ".github/workflows/release.yml")
    evidence_manifest = _read(root / "release/production-evidence.manifest.json")
    makefile = _read(root / "Makefile")
    tri_app_workflow = _read(root / ".github/workflows/tri-app-quality.yml")
    web_docker = _read(root / "apps/web-admin-next/Dockerfile")
    desktop_main = _read(root / "apps/desktop-tauri/src-tauri/src/main.rs")
    mobile_push = _read(root / "apps/mobile-flutter/lib/push_service.dart")
    ios_registrant = _read(root / "apps/mobile-flutter/ios/Runner/GeneratedPluginRegistrant.swift")
    contract = _read(root / "apps/shared/omni-app-api.contract.json")
    store = _read(root / "omnidesk_agent/appsync/store.py")
    desktop_api = _read(root / "apps/desktop-tauri/src/api.ts")
    mobile_api = _read(root / "apps/mobile-flutter/lib/omni_api.dart")
    self_healing = _read(root / "omnidesk_agent/self_healing.py")
    external_gate = _read(root / "scripts/check_external_ga_evidence.py")
    external_summary = _read(root / "scripts/write_real_ga_evidence_summary.py")
    external_required = _read(root / "release/external-ga-evidence.required.json")
    external_audit = _read(root / "release/real-ga-evidence-audit-1.12.7.json")
    package_script = _read(root / "scripts/package_distribution_bundle.sh")
    agents_root = _read(root / "AGENTS.md")
    onboarding = _read(root / "omnidesk_agent/onboarding.py")
    channel_capabilities = _read(root / "omnidesk_agent/channels/capability_matrix.py")
    identity_firewall = _read(root / "omnidesk_agent/channels/identity_firewall.py")
    execution_profiles = _read(root / "omnidesk_agent/security/execution_profiles.py")
    cik_guard = _read(root / "omnidesk_agent/security/cik_guard.py")
    signed_skill_registry = _read(root / "omnidesk_agent/skills/signed_registry.py")
    repair_loop = _read(root / "omnidesk_agent/self_upgrade/repair_loop.py")
    pr_generator = _read(root / "omnidesk_agent/self_upgrade/pr_generator.py")
    repair_workflow = _read(root / ".github/workflows/agent-repair-pr.yml")
    desktop_control_hub = _read(root / "apps/desktop-tauri/src/controlHub.ts")
    eval_promotion = _read(root / "omnidesk_agent/evals/promotion_gate.py")

    _check(version in pyproject and version in init, "Python package version is GA-aligned", failures, ok)
    _check(f"version: {chart_version}" in chart and f"appVersion: {version}" in chart, "Helm chart/appVersion are GA-aligned", failures, ok)
    _check('digest: "" # required' in values and 'required "image.digest is required' in deployment, "Helm requires pipeline-injected final image digest", failures, ok)
    _check("unsafe-eval" not in web_csp and "unsafe-inline" not in web_csp and "object-src 'none'" in web_csp, "Web Admin CSP forbids unsafe eval/inline and object embedding", failures, ok)
    _check("__Host-omni_session_token" in _read(root / "apps/web-admin-next/lib/session.ts"), "Web Admin uses __Host- prefixed session cookies", failures, ok)
    _check("allow_websocket_query_auth" in config and "must be false in production" in production_validator, "Production validator blocks WebSocket query-token auth", failures, ok)
    _check("class ApiResourceGuard" in resource_guard and "request body too large" in resource_guard and "actor-chat" in resource_guard and "actor-agent" in resource_guard, "API resource guard enforces body, rate, and chat/agent limits", failures, ok)
    _check("api_resource_guard.enabled must be true in production" in production_validator and "models.budget.{field_name} must be a positive hard limit in production" in production_validator, "Production validator requires API resource guards and model spend budgets", failures, ok)
    _check("OMNIDESK_REQUIRE_PRODUCTION_GUARDS" in production_validator and "OMNIDESK_REQUIRE_PRODUCTION_GUARDS" in _read(root / "deploy/systemd/omnidesk-agent.production.service") and "OMNIDESK_REQUIRE_PRODUCTION_GUARDS" in _read(root / "deploy/docker/docker-compose.full.yml"), "Production profiles include explicit production guard enforcement switch", failures, ok)
    _check("per_task_max_llm_calls: Optional[int] = 16" in config and "per-task model call limit exceeded" in _read(root / "omnidesk_agent/core/token_budget.py"), "Token budget enforces per-task model call hard limits", failures, ok)
    _check("token and \"authorization\" not in headers and allow_query_auth" in routes, "WebSocket query-token compatibility is gated outside production", failures, ok)
    _check("public_key is required for desktop/mobile device enrollment in production" in routes, "Production device registration requires a public key", failures, ok)
    _check("predictable device_id values are forbidden" in routes, "Production device registration rejects predictable IDs", failures, ok)
    _check("loadOrCreateDesktopIdentity" in desktop_app and "crypto.subtle.generateKey" in desktop_identity and "omni.devicePrivateKeyJwk.v2" in desktop_identity, "Desktop generates and stores per-install keypair", failures, ok)
    _check("DeviceIdentityStore" in mobile_main and "Ed25519" in mobile_identity and "omni.device_private_key.v2" in mobile_identity, "Mobile generates and stores per-install keypair", failures, ok)
    _check("cryptography:" in mobile_pubspec, "Mobile crypto dependency is declared", failures, ok)
    _check("app_sync:" in _read(root / "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml") and "backend: postgres" in _read(root / "deploy/kubernetes/helm/omnidesk/templates/configmap.yaml"), "Helm production config uses Postgres AppSync", failures, ok)
    _check("app_sync:" in docker_prod_config and "backend: postgres" in docker_prod_config and "postgres_dsn_env: OMNIDESK_POSTGRES_DSN" in docker_prod_config, "Docker production config uses Postgres AppSync", failures, ok)
    _check("omnidesk_appsync_snapshots" not in postgres_store and "source of truth" in postgres_store and "task not found in postgres appsync source of truth" in postgres_store, "Postgres AppSync no longer persists compact production state payloads", failures, ok)
    _check((root / "apps/desktop-tauri/src-tauri/Cargo.lock").exists(), "Desktop Rust lockfile is present", failures, ok)
    _check((root / "apps/mobile-flutter/pubspec.lock").exists(), "Mobile Flutter lockfile is present", failures, ok)
    _check("python -m pytest" in release_workflow and "render_locked_helm_values.py" in release_workflow and "--locked-values" in release_workflow, "Release workflow uses stable pytest entrypoint and locked Helm digest rendering", failures, ok)
    _check(f'"version": "{version}"' in evidence_manifest and '"external_evidence_required"' in evidence_manifest, "Release evidence manifest is present and explicit about external evidence", failures, ok)
    _check((root / "apps/web-admin-next/public/.gitkeep").exists() and "COPY --from=build /app/public ./public" in web_docker, "Web Admin Docker runtime has a committed public asset directory", failures, ok)
    _check("dirs::home_dir" not in desktop_main and "std::env::var_os" in desktop_main, "Desktop Tauri Rust source has no undeclared dirs dependency", failures, ok)
    _check("platform: 'mobile'" in mobile_push and "registerPushToken(deviceId, token, 'mobile')" not in mobile_push, "Mobile push service uses named platform argument", failures, ok)
    _check("final class GeneratedPluginRegistrant" in ios_registrant and "static func register(with registry: FlutterPluginRegistry)" in ios_registrant, "iOS plugin registrant has the AppDelegate-compatible entrypoint", failures, ok)
    _check("X-OmniDesk-Device-Id" in contract and "X-OmniDesk-Device-Signature" in contract, "Shared API contract declares device request signature headers", failures, ok)
    _check("require_device_signed_requests_in_production" in config and "require_device_signed_requests_in_production must be true in production" in production_validator, "Production config enforces signed device requests", failures, ok)
    _check("verify_device_request_signature" in store and "nonce_replay" in store and "device.request_signature_verified" in store, "AppSync store verifies request signatures and rejects nonce replay", failures, ok)
    _check("device signature rejected" in routes and "omnidesk_device_signature_failures_total" in routes, "AppSync routes enforce signed sensitive device requests", failures, ok)
    _check("signDesktopDeviceRequest" in desktop_identity and "deviceSigner" in desktop_api, "Desktop client signs sensitive device requests", failures, ok)
    _check("signRequest" in mobile_identity and "deviceIdentityStore" in mobile_api and "x-omnidesk-device-signature" in mobile_identity, "Mobile client signs sensitive device requests", failures, ok)
    _check("RuntimeSelfHealingController" in self_healing and "rollback_release" in self_healing and "create_upgrade_proposal" in self_healing, "Runtime self-healing policy is codified", failures, ok)
    _check("REQUIRED_EVIDENCE" in external_gate and "blocked_missing_external_evidence" in external_gate, "External real GA evidence checker exists and fails closed", failures, ok)
    _check("omnidesk-real-ga-evidence-summary/v1" in external_summary and "real_ga_ready" in external_summary and "blocking_categories" in external_summary, "Machine-readable Real GA evidence summary writer exists", failures, ok)
    _check("native-build/flutter-android-release.json" in external_required and "drills/self-healing-failure-injection.json" in external_required, "External real GA evidence required-file contract is declared", failures, ok)
    _check('"status": "blocked_missing_external_evidence"' in external_audit and '"self_healing_failure_injection"' in external_audit, "Current real GA evidence audit records missing external evidence", failures, ok)
    _check(
        "write_real_ga_evidence_summary.py" in release_workflow
        and "dist/external-ga-evidence-summary.json" in release_workflow
        and "real-ga-evidence-summary.json" in package_script,
        "Release and distribution flows emit machine-readable Real GA evidence summary",
        failures,
        ok,
    )
    _check("external-ga-evidence-gate" in makefile and "distribution-ga-preflight" in makefile, "Distribution GA preflight includes external real evidence gate", failures, ok)
    _check(
        "RELEASE_CHANNEL" in release_workflow
        and "real-ga" in release_workflow
        and "candidate" in release_workflow
        and "check_external_ga_evidence.py . --write-report release/real-ga-evidence-audit-1.12.7.json" in release_workflow
        and "check_external_ga_evidence.py . --audit-only --write-report release/real-ga-evidence-audit-1.12.7.json" in release_workflow,
        "Release workflow separates candidate audit from Real GA fail-closed evidence gate",
        failures,
        ok,
    )
    _check("web-admin-release" in release_workflow and "desktop-release" in release_workflow and "mobile-android-release" in release_workflow and "mobile-ios-release" in release_workflow and "needs:" in release_workflow, "Main release workflow depends on tri-app release builds", failures, ok)
    _check("tri-app-release-builds" in makefile and "cargo check --locked" in (makefile + tri_app_workflow + release_workflow) and "flutter build ipa --release" in (makefile + tri_app_workflow + release_workflow), "Tri-app release gates force native locked builds", failures, ok)
    _check("Do not lower security policy" in agents_root and "Do not fabricate release evidence" in agents_root, "Repository AGENTS rules forbid safety downgrade and fake evidence", failures, ok)
    _check((root / "apps/web-admin-next/AGENTS.md").exists() and (root / "apps/desktop-tauri/AGENTS.md").exists() and (root / "apps/mobile-flutter/AGENTS.md").exists() and (root / "omnidesk_agent/security/AGENTS.md").exists() and (root / "release/AGENTS.md").exists(), "Scoped AGENTS rules cover web, desktop, mobile, security, and release areas", failures, ok)
    _check("run_doctor" in onboarding and "build_onboarding_plan" in onboarding and "build_evidence_doctor" in onboarding, "Onboarding and doctor commands are backed by structured readiness checks", failures, ok)
    _check("channel_capability_matrix" in channel_capabilities and "bypass_approval" in channel_capabilities, "Channel capability matrix denies unsafe channel actions", failures, ok)
    _check("SenderIdentityStore" in identity_firewall and "pairing_required" in identity_firewall and "OAuth subject changed" in identity_firewall, "Channel identity firewall requires pairing and detects identity drift", failures, ok)
    _check("profile_workspace_write_no_network" in execution_profiles and "profile_break_glass" in execution_profiles and "no_secret_read" in execution_profiles, "Codex-style sandbox and approval profiles are codified", failures, ok)
    _check("evaluate_cik" in cik_guard and "capability_guard_denied" in cik_guard and "knowledge_guard_untrusted_source" in cik_guard, "CIK defense layer evaluates capability, identity, and knowledge boundaries", failures, ok)
    _check("SignedSkillRegistry" in signed_skill_registry and "skill signature is required" in signed_skill_registry and "vulnerability scan must pass" in signed_skill_registry, "Signed skill registry fails closed on unsigned or unscanned skills", failures, ok)
    _check("IncidentReviewer" in repair_loop and "RepairPlanner" in repair_loop and "GateRunner" in repair_loop, "Self-healing repair loop separates observe, diagnosis, repair plan, and gates", failures, ok)
    _check("PRGenerator" in pr_generator and "External evidence status" in pr_generator and "ready_for_review" in pr_generator, "Codex-style repair PR generator includes tests, rollback, and evidence", failures, ok)
    _check("startsWith(github.head_ref, 'ai/')" in repair_workflow and "check_external_ga_evidence.py . --audit-only" in repair_workflow, "Agent repair PR workflow is branch-scoped and keeps external evidence fail-closed", failures, ok)
    _check("buildControlHubPanels" in desktop_control_hub and "External Evidence" in desktop_control_hub, "Desktop Control Hub surfaces runtime and evidence status", failures, ok)
    _check("evaluate_promotion" in eval_promotion and "min_pass_rate" in eval_promotion, "Agent eval harness has a promotion gate", failures, ok)

    if args.release_metadata:
        metadata = json.loads(Path(args.release_metadata).read_text(encoding="utf-8"))
        digest = metadata.get("image", {}).get("digest", "")
        _check(bool(HEX_DIGEST_RE.match(digest)), "release metadata includes final OCI image digest", failures, ok)
        _check(metadata.get("version") == version, "release metadata version is GA-aligned", failures, ok)

    for message in ok:
        print(f"OK      {message}")
    for message in failures:
        print(f"BLOCKER {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
