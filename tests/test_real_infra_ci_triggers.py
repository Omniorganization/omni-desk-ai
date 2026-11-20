"""The only real-infrastructure CI tests must run on every change.

Why: isolated-cloud-acceptance (real Postgres, rootless Podman runner, TLS,
restore) and web-first-candidate (real browser against the packaged gateway)
were path-filtered, so most pull requests never ran them, and a
path-filtered workflow cannot be made a required check: a PR it skips waits
forever for the status. They must run on every pull request and in the merge
queue.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


@pytest.mark.parametrize("name", ["isolated-cloud-acceptance.yml", "web-first-candidate.yml"])
def test_real_infra_workflow_runs_on_every_pull_request_and_merge_group(name):
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    on = workflow.get("on", workflow.get(True))
    assert "pull_request" in on and not (on["pull_request"] or {}).get("paths")
    assert not (on["pull_request"] or {}).get("paths-ignore")
    assert "merge_group" in on
    assert "push" in on and not (on["push"] or {}).get("paths")
