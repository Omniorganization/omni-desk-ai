from __future__ import annotations

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
