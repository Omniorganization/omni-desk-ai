from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnidesk_agent.config import AppConfig
from omnidesk_agent.daemon import OmniDeskRuntime
from omnidesk_agent.models.base import ModelResponse
from omnidesk_agent.security.permissions import PermissionDecision
from omnidesk_agent.tools.base import ToolContext
from omnidesk_agent.tools.vision import VisionGroundingTool

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
IMAGES = {
    "shot.png": PNG,
    "shot.jpg": b"\xff\xd8\xff\xe0" + b"\0" * 16,
    "shot.JPEG": b"\xff\xd8\xff\xdb" + b"\0" * 16,
    "shot.gif": b"GIF89a" + b"\0" * 16,
    "shot.webp": b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8,
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    # Relative image paths are relative to the process directory.
    monkeypatch.chdir(ws)
    return ws


def _tool(workspace: Path, **kwargs):
    router = Mock()
    router.complete = AsyncMock(return_value=ModelResponse(text='{"summary": "ok"}', provider="fake", model="vision", profile="vision"))
    return VisionGroundingTool(router, workspace, **kwargs), router


def _ctx():
    permissions = Mock()
    permissions.verify.return_value = PermissionDecision(True, "allow", "test")
    return ToolContext(permissions=permissions), permissions


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform without symlinks
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(IMAGES))
async def test_workspace_images_are_sent_by_resolved_path(workspace, name):
    (workspace / "screens").mkdir()
    image = workspace / "screens" / name
    image.write_bytes(IMAGES[name])
    tool, router = _tool(workspace)
    ctx, permissions = _ctx()

    result = await tool.call("ground", {"image_path": f"screens/{name}"}, ctx)

    assert result.ok
    resolved = str(image.resolve())
    assert router.complete.await_args.args[0].images == [resolved]
    assert permissions.verify.call_args.args[0].args["image_path"] == resolved


@pytest.mark.asyncio
async def test_relative_paths_must_still_land_in_the_workspace(workspace, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "shot.png").write_bytes(PNG)
    monkeypatch.chdir(elsewhere)
    tool, router = _tool(workspace)

    with pytest.raises(PermissionError, match="escapes workspace"):
        await tool.call("ground", {"image_path": "shot.png"}, _ctx()[0])
    router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_relative_workspace_root_accepts_computer_tool_screenshot_paths(tmp_path, monkeypatch):
    # examples/config.codex-cli.yaml uses a relative workspace.root; the
    # computer tool then reports screenshots relative to the process directory.
    monkeypatch.chdir(tmp_path)
    shots = Path(".omnidesk/workspace/screenshots")
    shots.mkdir(parents=True)
    (shots / "abc.png").write_bytes(PNG)
    tool, router = _tool(Path(".omnidesk/workspace"))

    assert (await tool.call("ground", {"image_path": str(shots / "abc.png")}, _ctx()[0])).ok
    assert router.complete.await_args.args[0].images == [str((tmp_path / shots / "abc.png").resolve())]


@pytest.mark.asyncio
async def test_file_swapped_while_awaiting_approval_is_refused(workspace, tmp_path):
    image = workspace / "shot.png"
    image.write_bytes(PNG)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    tool, router = _tool(workspace)

    def swap_to_text(_proposal):
        image.write_bytes(b"-----BEGIN PRIVATE KEY-----")
        return PermissionDecision(True, "allow", "test")

    ctx, permissions = _ctx()
    permissions.verify.side_effect = swap_to_text
    with pytest.raises(PermissionError, match="does not match"):
        await tool.call("ground", {"image_path": "shot.png"}, ctx)

    other = workspace / "other.png"
    other.write_bytes(PNG)
    for target, match in ((outside, "escapes workspace"), (other, "changed while awaiting approval")):
        if image.is_symlink() or image.exists():
            image.unlink()
        image.write_bytes(PNG)

        def swap_to_symlink(_proposal, target=target):
            image.unlink()
            _symlink(image, target)
            return PermissionDecision(True, "allow", "test")

        ctx, permissions = _ctx()
        permissions.verify.side_effect = swap_to_symlink
        with pytest.raises(PermissionError, match=match):
            await tool.call("ground", {"image_path": "shot.png"}, ctx)
    router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_host_files_outside_the_workspace_are_refused(workspace, tmp_path, monkeypatch):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    monkeypatch.setenv("HOME", str(tmp_path))
    tool, router = _tool(workspace)
    ctx, permissions = _ctx()

    for raw in (str(outside), "../outside.png", "~/outside.png", "screens/../../outside.png"):
        with pytest.raises(PermissionError, match="escapes workspace"):
            await tool.call("ground", {"image_path": raw}, ctx)
    router.complete.assert_not_called()
    permissions.verify.assert_not_called()


@pytest.mark.asyncio
async def test_symlinks_leaving_the_workspace_are_refused(workspace, tmp_path):
    outside_dir = tmp_path / "host"
    outside_dir.mkdir()
    (outside_dir / "secret.png").write_bytes(PNG)
    _symlink(workspace / "link.png", outside_dir / "secret.png")
    _symlink(workspace / "linked-dir", outside_dir)
    tool, router = _tool(workspace)

    for raw in ("link.png", "linked-dir/secret.png", str(workspace / "link.png")):
        with pytest.raises(PermissionError, match="escapes workspace"):
            await tool.call("ground", {"image_path": raw}, _ctx()[0])
    router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_symlink_inside_the_workspace_is_followed_to_its_target(workspace):
    (workspace / "real.png").write_bytes(PNG)
    _symlink(workspace / "alias.png", workspace / "real.png")
    tool, router = _tool(workspace)

    assert (await tool.call("ground", {"image_path": "alias.png"}, _ctx()[0])).ok
    assert router.complete.await_args.args[0].images == [str((workspace / "real.png").resolve())]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "content", "match"),
    [
        ("notes.txt", b"secret", "type is not allowed"),
        ("id_rsa", b"-----BEGIN", "type is not allowed"),
        ("image.svg", b"<svg/>", "type is not allowed"),
        ("fake.png", b"-----BEGIN OPENSSH PRIVATE KEY-----", "does not match"),
        ("fake.jpg", PNG, "does not match"),
        ("fake.webp", b"RIFF\0\0\0\0WAVEfmt ", "does not match"),
        ("empty.png", b"", "size 0"),
    ],
)
async def test_file_type_and_content_are_checked(workspace, name, content, match):
    (workspace / name).write_bytes(content)
    tool, router = _tool(workspace)
    ctx, permissions = _ctx()

    with pytest.raises(PermissionError, match=match):
        await tool.call("ground", {"image_path": name}, ctx)
    router.complete.assert_not_called()
    permissions.verify.assert_not_called()


@pytest.mark.asyncio
async def test_file_size_is_bounded(workspace):
    (workspace / "big.png").write_bytes(PNG + b"\0" * 64)
    tool, router = _tool(workspace, max_image_bytes=32)

    with pytest.raises(PermissionError, match="outside 1..32 bytes"):
        await tool.call("ground", {"image_path": "big.png"}, _ctx()[0])
    router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_non_regular_files_are_refused_without_blocking(workspace):
    (workspace / "dir.png").mkdir()
    tool, router = _tool(workspace)
    with pytest.raises(PermissionError, match="not a regular file"):
        await tool.call("ground", {"image_path": "dir.png"}, _ctx()[0])

    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    os.mkfifo(workspace / "pipe.png")
    with pytest.raises(PermissionError, match="not a regular file"):
        await tool.call("ground", {"image_path": "pipe.png"}, _ctx()[0])
    router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_bad_arguments(workspace):
    tool, router = _tool(workspace)
    with pytest.raises(ValueError, match="requires image_path"):
        await tool.call("ground", {}, _ctx()[0])
    with pytest.raises(PermissionError, match="NUL"):
        await tool.call("ground", {"image_path": "a\0.png"}, _ctx()[0])
    missing = await tool.call("ground", {"image_path": "missing.png"}, _ctx()[0])
    assert not missing.ok and "does not exist" in missing.error
    router.complete.assert_not_called()


def test_runtime_binds_vision_to_the_configured_workspace(tmp_path):
    cfg = AppConfig()
    cfg.workspace.root = tmp_path / "ws"
    cfg.workspace.memory_db = tmp_path / "memory.sqlite3"
    cfg.workspace.skills_dirs = [tmp_path / "skills"]
    cfg.workspace.plugins_dirs = [tmp_path / "plugins"]
    cfg.permissions.audit_log = tmp_path / "audit.log"
    cfg.channels.gmail.credentials_file = tmp_path / "google" / "credentials.json"
    cfg.channels.gmail.token_file = tmp_path / "google" / "token.json"
    cfg.learning.growth_plan_file = tmp_path / "growth_plan.json"
    cfg.ensure_dirs()
    runtime = OmniDeskRuntime(cfg)
    try:
        assert runtime.tools.get("vision").root == (tmp_path / "ws").resolve()
    finally:
        runtime.close()
