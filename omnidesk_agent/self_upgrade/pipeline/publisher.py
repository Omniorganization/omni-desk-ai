"""Open a reviewable PR for a verified change. It cannot merge or touch main.

The client can do four things: read a branch head, create an ``ai/*`` branch,
add one commit to that branch, and open a draft PR into ``main``. There is no
merge, update-ref-to-main, delete or workflow call. The commit is created
through GitHub's ``createCommitOnBranch`` mutation, so GitHub signs it and
attributes it to the token's identity (the GitHub App). The token comes from
an environment variable and is only sent to api.github.com.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

API = "https://api.github.com"
BRANCH_RE = re.compile(r"^ai/[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
Transport = Callable[[str, str, Optional[dict[str, Any]], dict[str, str]], dict[str, Any]]


def _urllib_transport(method: str, url: str, body: Optional[dict[str, Any]], headers: dict[str, str]) -> dict[str, Any]:
    if not url.startswith(API + "/"):
        raise PermissionError("the publisher only talks to api.github.com")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
        payload = response.read()
    return json.loads(payload) if payload else {}


@dataclass(frozen=True)
class PullRequestResult:
    number: int
    url: str
    branch: str
    commit_oid: str


class GitHubPublisher:
    def __init__(self, repo: str, *, token_env: str = "OMNIDESK_UPGRADE_GITHUB_TOKEN", transport: Optional[Transport] = None):
        if not REPO_RE.match(repo):
            raise ValueError(f"invalid repository slug: {repo}")
        token = os.getenv(token_env, "")
        if not token:
            raise RuntimeError(f"{token_env} is not set")
        self.repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "omnidesk-self-upgrade",
        }
        self._transport = transport or _urllib_transport

    def _call(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._transport(method, f"{API}{path}", body, dict(self._headers))

    def head_sha(self, branch: str = "main") -> str:
        return str(self._call("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")["object"]["sha"])

    def create_branch(self, branch: str, sha: str) -> None:
        check_branch(branch)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("base sha must be a full commit id")
        self._call("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def commit(self, branch: str, *, expected_head: str, headline: str, body: str, files: dict[str, bytes]) -> str:
        check_branch(branch)
        mutation = """
        mutation($input: CreateCommitOnBranchInput!) {
          createCommitOnBranch(input: $input) { commit { oid url } }
        }"""
        variables = {"input": {
            "branch": {"repositoryNameWithOwner": self.repo, "branchName": branch},
            "expectedHeadOid": expected_head,
            "message": {"headline": headline[:200], "body": body},
            "fileChanges": {"additions": [
                {"path": path, "contents": base64.b64encode(content).decode()} for path, content in sorted(files.items())
            ]},
        }}
        result = self._call("POST", "/graphql", {"query": mutation, "variables": variables})
        if result.get("errors"):
            raise RuntimeError(f"createCommitOnBranch failed: {json.dumps(result['errors'])[:500]}")
        return str(result["data"]["createCommitOnBranch"]["commit"]["oid"])

    def open_pull_request(self, branch: str, *, title: str, body: str, draft: bool = True, labels: tuple[str, ...] = ()) -> dict[str, Any]:
        check_branch(branch)
        pr = self._call("POST", f"/repos/{self.repo}/pulls", {"title": title[:250], "head": branch, "base": "main", "body": body, "draft": draft})
        if labels:
            try:
                self._call("POST", f"/repos/{self.repo}/issues/{pr['number']}/labels", {"labels": list(labels)})
            except Exception:
                pass  # a missing label must not lose the PR
        return pr

    def publish(self, branch: str, *, files: dict[str, bytes], headline: str, commit_body: str, title: str, pr_body: str,
                base_sha: Optional[str] = None, labels: tuple[str, ...] = ("self-upgrade",)) -> PullRequestResult:
        base = base_sha or self.head_sha("main")
        self.create_branch(branch, base)
        oid = self.commit(branch, expected_head=base, headline=headline, body=commit_body, files=files)
        pr = self.open_pull_request(branch, title=title, body=pr_body, labels=labels)
        return PullRequestResult(number=int(pr["number"]), url=str(pr["html_url"]), branch=branch, commit_oid=oid)


def check_branch(branch: str) -> None:
    if not BRANCH_RE.match(branch) or len(branch) > 100 or ".." in branch or branch.endswith((".lock", "/", ".")):
        raise PermissionError(f"the pipeline may only write ai/* branches: {branch!r}")
