"""Self-upgrade pipeline: propose -> verify -> publish, one step per command.

    python -m omnidesk_agent.self_upgrade.pipeline propose --issue issue.json --out work/ --provider anthropic --model MODEL
    python -m omnidesk_agent.self_upgrade.pipeline verify  --issue issue.json --diff work/proposal.diff --out work/
    python -m omnidesk_agent.self_upgrade.pipeline publish --issue issue.json --diff work/proposal.diff --evidence work/evidence.json --repo OWNER/NAME

The three steps run as separate CI jobs: ``propose`` holds the model key but
never runs the patch, ``verify`` runs the patch but holds no secrets, and
``publish`` holds the GitHub App token but only re-checks, applies (without
running) and publishes a patch whose evidence passed. Nothing merges.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Optional

from omnidesk_agent.self_upgrade.pipeline.diff_policy import DiffPolicy
from omnidesk_agent.self_upgrade.pipeline.issue import load_issue
from omnidesk_agent.self_upgrade.pipeline.proposer import ProviderModelClient, propose
from omnidesk_agent.self_upgrade.pipeline.publisher import GitHubPublisher
from omnidesk_agent.self_upgrade.pipeline.verifier import patch_sha256, verify
from omnidesk_agent.self_upgrade.pipeline.workspace import IsolatedWorktree


def branch_name(issue: Any, *, today: Optional[dt.date] = None, attempt: Optional[str] = None) -> str:
    """ai/self-upgrade-<ref>-<slug>-<yyyymmdd>-<attempt>; unique per publication attempt."""
    day = (today or dt.datetime.now(dt.timezone.utc).date()).strftime("%Y%m%d")
    ref = re.sub(r"[^a-z0-9]+", "", str(issue.reference).lower())[:12] or "manual"
    token = re.sub(r"[^a-z0-9]+", "", (attempt or os.getenv("GITHUB_RUN_ID") or secrets.token_hex(3)).lower())[:12]
    return f"ai/self-upgrade-{ref}-{issue.slug()[:30].strip('-')}-{day}-{token}"


def pr_body(issue: Any, proposal: dict[str, Any], evidence: dict[str, Any]) -> str:
    steps = "\n".join(f"- `{s['name']}`: exit {s['exit_code']} ({s['seconds']} s)" for s in evidence.get("steps", []))
    tests = ", ".join(f"`{t}`" for t in evidence.get("targeted_tests", [])) or "none"
    return f"""Opened by the OmniDesk self-upgrade pipeline for {issue.source} {issue.reference}: {issue.title}

### Change
{proposal.get('summary') or '(no summary)'}

Model: `{proposal.get('model') or 'unknown'}`. Files: {', '.join(f'`{f}`' for f in evidence.get('changed_files', []))}.

### Evidence (verify job, isolated worktree, no secrets)
Base `{evidence.get('base_sha')}`, patch sha256 `{evidence.get('patch_sha256')}`. Targeted tests: {tests}.
{steps}

### Risk
Model-written code. It passed the diff policy (no CI, scripts, deploy, release, security, sandbox, plugin, self-upgrade or dependency files; size limits), the targeted and full test suites and ruff/bandit/compileall. Review the diff as you would any external contribution.

### Rollback
Close this PR, or revert its squash commit after merge. The pipeline never merges and never pushes to `main`.

self-upgrade-targeted-tests: {json.dumps(evidence.get('targeted_tests', []))}
"""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omnidesk_agent.self_upgrade.pipeline")
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--issue", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--api-key-env", default="OMNIDESK_UPGRADE_MODEL_API_KEY")
    v = sub.add_parser("verify")
    v.add_argument("--issue", required=True)
    v.add_argument("--diff", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--base-ref", default="HEAD")
    v.add_argument("--no-full-suite", action="store_true")
    pub = sub.add_parser("publish")
    pub.add_argument("--issue", required=True)
    pub.add_argument("--diff", required=True)
    pub.add_argument("--evidence", required=True)
    pub.add_argument("--proposal", default=None)
    pub.add_argument("--repo", required=True)
    pub.add_argument("--token-env", default="OMNIDESK_UPGRADE_GITHUB_TOKEN")
    pub.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    issue = load_issue(Path(args.issue))

    if args.cmd == "propose":
        client = ProviderModelClient(args.provider, args.model, api_key_env=args.api_key_env)
        proposal = asyncio.run(propose(root, issue, client, model_name=f"{args.provider}/{args.model}"))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "proposal.diff").write_text(proposal.diff, encoding="utf-8")
        (out / "proposal.json").write_text(json.dumps(proposal.to_dict() | {"diff": None}, indent=2), encoding="utf-8")
        report = DiffPolicy().check(proposal.diff)
        print(json.dumps({"diff_policy_ok": report.ok, "violations": report.violations, "files": report.files}))
        return 0 if report.ok else 3

    diff = Path(args.diff).read_text(encoding="utf-8")
    if args.cmd == "verify":
        bundle = verify(root, diff, issue, base_ref=args.base_ref, run_full_suite=not args.no_full_suite)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "evidence.json").write_text(json.dumps(bundle.to_dict(), indent=2), encoding="utf-8")
        print(json.dumps({"ok": bundle.ok, "reasons": bundle.reasons}))
        return 0 if bundle.ok else 4

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not evidence.get("ok") or evidence.get("patch_sha256") != patch_sha256(diff):
        print(json.dumps({"published": False, "reason": "evidence is not passing or does not belong to this patch"}))
        return 5
    if not any(step.get("name") == "full_suite" and step.get("exit_code") == 0 for step in evidence.get("steps", [])):
        print(json.dumps({"published": False, "reason": "evidence has no passing full-suite run (verify ran with --no-full-suite?)"}))
        return 5
    report = DiffPolicy().check(diff)
    if not report.ok:
        print(json.dumps({"published": False, "reason": "diff policy", "violations": report.violations}))
        return 5
    with IsolatedWorktree(root, evidence["base_sha"]) as tree:  # apply only; nothing runs here
        if tree.apply(diff).exit_code != 0:
            print(json.dumps({"published": False, "reason": "patch no longer applies to its base"}))
            return 5
        files = {path: tree.read(path) for path in tree.changed_files()}
    proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8")) if args.proposal else {}
    branch = branch_name(issue)
    title = f"fix(self-upgrade): {issue.title}"[:120]
    body = pr_body(issue, proposal, evidence)
    if args.dry_run:
        print(json.dumps({"published": False, "dry_run": True, "branch": branch, "files": sorted(files), "title": title}))
        return 0
    result = GitHubPublisher(args.repo, token_env=args.token_env).publish(
        branch, files=files, headline=title, commit_body=f"{issue.source} {issue.reference}\n\npatch sha256 {evidence['patch_sha256']}",
        title=title, pr_body=body, base_sha=evidence["base_sha"],
    )
    print(json.dumps({"published": True, "pr": result.number, "url": result.url, "branch": result.branch}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
