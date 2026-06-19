from __future__ import annotations

from scripts.check_monorepo_layout import main


def test_current_tree_exposes_root_monorepo_layout(capsys) -> None:
    assert main(["."]) == 0
    assert "monorepo layout ok" in capsys.readouterr().out
