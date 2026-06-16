from __future__ import annotations

from pathlib import Path


def test_web_admin_docker_public_directory_is_committed() -> None:
    assert Path("apps/web-admin-next/public/.gitkeep").exists()
    dockerfile = Path("apps/web-admin-next/Dockerfile").read_text(encoding="utf-8")
    next_config = Path("apps/web-admin-next/next.config.mjs").read_text(encoding="utf-8")
    assert "COPY --from=build /app/public ./public" in dockerfile
    assert "COPY --from=build /app/.next/standalone ./" in dockerfile
    assert "CMD [\"node\", \"server.js\"]" in dockerfile
    assert "output: 'standalone'" in next_config


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


def test_ios_podfile_prefers_ci_flutter_root() -> None:
    podfile = Path("apps/mobile-flutter/ios/Podfile").read_text(encoding="utf-8")
    assert "env_flutter_root = ENV['FLUTTER_ROOT']" in podfile
    assert "return env_flutter_root" in podfile
    assert "Generated.xcconfig" in podfile
    assert "post_install do |installer|" in podfile
    assert "flutter_additional_ios_build_settings(target)" in podfile


def test_ios_release_jobs_generate_standard_flutter_project() -> None:
    tri_app = Path(".github/workflows/tri-app-quality.yml").read_text(encoding="utf-8")
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    for workflow in [tri_app, release]:
        assert "flutter create . --platforms=ios --project-name omnidesk_mobile --org com.omnidesk --overwrite --no-pub" in workflow
        assert "git checkout -- pubspec.yaml pubspec.lock README.md lib test ios/Podfile ios/Runner/AppDelegate.swift ios/Runner/Info.plist" in workflow
        assert "Link CocoaPods build settings" in workflow
        assert "Pods-Runner.$config_lc.xcconfig" in workflow
        assert "Pods-Runner.profile.xcconfig" in workflow
    assert "flutter build ios --release --no-codesign" in tri_app
    assert "OMNI_IOS_CERTIFICATE_P12_BASE64" in release
    assert "OMNI_IOS_PROVISIONING_PROFILE_BASE64" in release
    assert "flutter build ipa --release --export-options-plist=ios/ExportOptions.plist" in release


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
