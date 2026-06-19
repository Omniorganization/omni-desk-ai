from __future__ import annotations

from pathlib import Path


def test_web_admin_docker_public_directory_is_committed() -> None:
    assert Path("apps/web-admin-next/public/.gitkeep").exists()
    dockerfile = Path("apps/web-admin-next/Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=build /app/public ./public" in dockerfile


def test_desktop_rust_has_no_undeclared_dirs_dependency() -> None:
    main_rs = Path("apps/desktop-tauri/src-tauri/src/main.rs").read_text(encoding="utf-8")
    cargo_toml = Path("apps/desktop-tauri/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    assert "dirs::home_dir" not in main_rs
    assert "std::env::var_os" in main_rs
    assert "dirs =" not in cargo_toml


def test_mobile_push_service_uses_named_platform_argument() -> None:
    push_service = Path("apps/mobile-flutter/lib/push_service.dart").read_text(encoding="utf-8")
    assert "platform: 'mobile'" in push_service
    assert "registerPushToken(deviceId, token, 'mobile')" not in push_service
    assert "registerPushToken(deviceId, newToken, 'mobile')" not in push_service


def test_ios_registrant_matches_app_delegate_shape() -> None:
    registrant = Path("apps/mobile-flutter/ios/Runner/GeneratedPluginRegistrant.swift").read_text(encoding="utf-8")
    assert "final class GeneratedPluginRegistrant" in registrant
    assert "static func register(with registry: FlutterPluginRegistry)" in registrant


def test_tri_app_release_gates_force_native_builds() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/tri-app-quality.yml").read_text(encoding="utf-8")
    combined = makefile + "\n" + workflow
    assert "tri-app-release-builds" in makefile
    assert "cargo check --locked" in combined
    assert "flutter build appbundle --release" in combined
    assert "flutter build ipa --release" in combined
    assert "dart analyze" in combined or "flutter analyze" in combined


def test_shared_contract_declares_device_signature_headers() -> None:
    contract = Path("apps/shared/omni-app-api.contract.json").read_text(encoding="utf-8")
    assert "X-OmniDesk-Device-Id" in contract
    assert "X-OmniDesk-Timestamp" in contract
    assert "X-OmniDesk-Nonce" in contract
    assert "X-OmniDesk-Device-Signature" in contract
