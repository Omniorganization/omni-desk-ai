from __future__ import annotations

from pathlib import Path
from typing import Optional

from omnidesk_agent.tools.github_preflight import GitHubPreflight


def validate_github(runtime, head: Optional[str] = None) -> dict:
    cfg = runtime.cfg.github
    repo_root = Path(getattr(runtime, "repo_root", cfg.repo_root or Path.cwd()))
    preflight = GitHubPreflight(
        repo_root,
        remote_name=cfg.remote_name,
        host=cfg.host,
    ).run(head=head, require_write=cfg.require_write_access)
    return {
        "ok": preflight.ok,
        "enabled": cfg.enabled,
        "preflight": preflight.to_dict(),
        "notes": {
            "auth": f"Run `gh auth login -h {cfg.host} -w -s repo` when authentication is missing or invalid.",
            "branch": f"Push AI branches first with `git push -u {cfg.remote_name} ai/<branch>` before creating a PR.",
        },
    }
