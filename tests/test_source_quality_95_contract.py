from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_desktop_capabilities_are_truthful_and_fail_closed() -> None:
    executor = text("apps/desktop-tauri/src/executor.ts")
    app = text("apps/desktop-tauri/src/App.tsx")
    api = text("apps/desktop-tauri/src/api.ts")
    package = text("apps/desktop-tauri/package.json")

    assert "new BrowserAutomationExecutor()" not in executor
    assert "new FileOperationExecutor()" not in executor
    assert "new UiBridgeExecutor()" not in executor
    assert "unsupported runtime capability" in executor
    assert "contents and path omitted from status" in executor
    assert "names and path omitted from status" in executor
    assert "workspace read completed: ${relativePath}" not in executor
    assert "workspace list completed: ${relativePath}" not in executor
    assert "EXECUTABLE_CAPABILITIES" in api
    assert "if (status >= 500) return new Error(`${status} gateway_unavailable`)" in api
    assert "tests/*.test.ts" in package
    assert "const CAPABILITIES = advertisedRuntimeCapabilities()" in app
    assert "const workerBusy = useRef(false)" in app
    assert "if (workerBusy.current) return" in app


def test_unimplemented_controls_are_truthfully_unavailable() -> None:
    web = text("apps/web-admin-next/app/page.tsx")
    mobile = text("apps/mobile-flutter/lib/main.dart")
    assert 'disabled title="未启用"' in web
    assert "style={{" not in web
    assert "onTap: () {}" not in mobile
    assert "enabled: false" in mobile


def test_web_base_image_is_resolved_to_an_immutable_digest() -> None:
    dockerfile = text("apps/web-admin-next/Dockerfile")
    workflow = text(".github/workflows/release.yml")
    assert "ARG NODE_BASE_IMAGE=node:" not in dockerfile
    assert 'NODE_BASE_IMAGE="$(docker image inspect' in workflow
    assert '--build-arg "NODE_BASE_IMAGE=$NODE_BASE_IMAGE"' in workflow


def test_native_workspace_boundary_rejects_escape_and_symlinks() -> None:
    native = text("apps/desktop-tauri/src-tauri/src/main.rs")

    assert "validate_relative_path" in native
    assert "Component::ParentDir" in native
    assert "Component::Prefix(_)" in native
    assert "workspace path cannot traverse symlinks" in native
    assert "approved workspace root cannot be a symlink" in native
    assert "workspace root cannot be a symlink" in native
    assert "symlink_metadata(&declared)" in native
    assert "approved workspace root ~/OmniDesktopWorkspace is unavailable" in native


def test_liveness_is_independent_from_readiness() -> None:
    deployment = text("deploy/kubernetes/helm/omnidesk/templates/deployment.yaml")
    startup = deployment.split("startupProbe:", 1)[1].split("livenessProbe:", 1)[0]
    liveness = deployment.split("livenessProbe:", 1)[1].split("readinessProbe:", 1)[0]
    readiness = deployment.split("readinessProbe:", 1)[1]

    assert "path: /health" in startup
    assert "path: /health" in liveness
    assert "path: /ready" in readiness


def test_web_admin_runtime_is_standalone_and_non_root() -> None:
    dockerfile = text("apps/web-admin-next/Dockerfile")
    next_config = text("apps/web-admin-next/next.config.mjs")

    assert "output: 'standalone'" in next_config
    assert "USER nextjs" in dockerfile
    assert "/app/.next/standalone" in dockerfile
    assert "COPY --from=build --chown=nextjs:nodejs /app/public ./public" in dockerfile
    assert 'CMD ["node", "server.js"]' in dockerfile


def test_backend_oci_source_matches_organization_repository() -> None:
    dockerfile = text("Dockerfile")
    assert "https://github.com/Omniorganization/omni-desk-ai" in dockerfile
    assert "https://github.com/yinyufan0813-cmyk/omni-desk-ai" not in dockerfile
    assert "http://127.0.0.1:18789/health" in dockerfile
