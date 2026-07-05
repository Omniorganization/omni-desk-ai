from __future__ import annotations

from pathlib import Path

from scripts.check_github_workflows import main


def test_github_workflow_check_rejects_dispatch_input_overflow(tmp_path, capsys):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    inputs = "\n".join(
        f"      input_{index}:\n        required: false" for index in range(11)
    )
    (workflows / "too-many-inputs.yml").write_text(
        f"""
name: too many inputs
on:
  workflow_dispatch:
    inputs:
{inputs}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
""",
        encoding="utf-8",
    )

    assert main([str(workflows)]) == 1
    assert "at most 10" in capsys.readouterr().err


def test_github_workflow_check_rejects_workflow_call_required_default(
    tmp_path, capsys
):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "callable.yml").write_text(
        """
name: callable
on:
  workflow_call:
    inputs:
      channel:
        type: string
        required: true
        default: candidate
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
""",
        encoding="utf-8",
    )

    assert main([str(workflows)]) == 1
    assert "cannot be both required and have a default" in capsys.readouterr().err


def test_latest_main_ci_evidence_dispatch_is_main_only() -> None:
    workflow = Path(".github/workflows/latest-main-ci-evidence.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "if: ${{ github.ref_name == 'main' }}" in workflow


def test_github_workflow_check_rejects_unguarded_latest_main_dispatch(
    tmp_path, capsys
):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "latest-main-ci-evidence.yml").write_text(
        """
name: Latest Main CI Evidence
on:
  workflow_dispatch:
jobs:
  latest-main-ci-evidence:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
""",
        encoding="utf-8",
    )

    assert main([str(workflows)]) == 1
    assert "guarded to run only on main" in capsys.readouterr().err
