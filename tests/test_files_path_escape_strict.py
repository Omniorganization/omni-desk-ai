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
