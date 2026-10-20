from __future__ import annotations

import pytest

from omnidesk_agent.tools.files import FilesTool


def test_files_tool_blocks_prefix_sibling_escape(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "root2" / "secret.txt"
    sibling.parent.mkdir()
    sibling.write_text("secret", encoding="utf-8")

    tool = FilesTool(root)
    with pytest.raises(PermissionError):
        tool._safe_path(str(sibling))


def test_files_tool_allows_inside_workspace(tmp_path):
    root = tmp_path / "root"
    tool = FilesTool(root)
    assert tool._safe_path("nested/file.txt").relative_to(root.resolve())


@pytest.mark.parametrize("rel", [".git/config", ".git/hooks/pre-commit", "sub/.git", "sub/.GIT/config", "./.git/info/attributes"])
def test_files_tool_refuses_writes_to_git_metadata(tmp_path, rel):
    tool = FilesTool(tmp_path / "root", allow_write=True)
    with pytest.raises(PermissionError, match="git metadata"):
        tool._writable_path(rel)


def test_files_tool_refuses_git_metadata_through_symlinked_dir(tmp_path):
    root = tmp_path / "root"
    tool = FilesTool(root, allow_write=True)
    (root / ".git").mkdir()
    (root / "innocent").symlink_to(root / ".git", target_is_directory=True)
    with pytest.raises(PermissionError, match="git metadata"):
        tool._writable_path("innocent/config")


def test_files_tool_still_writes_regular_files(tmp_path):
    tool = FilesTool(tmp_path / "root", allow_write=True)
    assert tool._writable_path("src/.gitignore").name == ".gitignore"
    assert tool._writable_path("docs/git-notes.md").name == "git-notes.md"
