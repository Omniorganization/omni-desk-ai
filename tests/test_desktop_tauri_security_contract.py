from __future__ import annotations

from pathlib import Path


def test_tauri_desktop_does_not_enable_arbitrary_fs_shell_or_http_plugins() -> None:
    cargo = Path("apps/desktop-tauri/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    config = Path("apps/desktop-tauri/src-tauri/tauri.conf.json").read_text(encoding="utf-8")

    assert "tauri-plugin-shell" not in cargo
    assert "tauri-plugin-fs" not in cargo
    assert "tauri-plugin-http" not in cargo
    assert '"shell"' not in config
    assert '"fs"' not in config
    assert '"http"' not in config


def test_tauri_native_commands_are_allowlisted_and_workspace_scoped() -> None:
    main = Path("apps/desktop-tauri/src-tauri/src/main.rs").read_text(encoding="utf-8")

    assert "read_workspace_file, list_workspace_directory" in main
    assert 'home.join("OmniDesktopWorkspace")' in main
    assert "workspace must be inside ~/OmniDesktopWorkspace" in main
    assert "relative.is_absolute()" in main
    assert "Component::ParentDir" in main
    assert "resolved.starts_with(&root)" in main
    assert "Command::new" not in main
