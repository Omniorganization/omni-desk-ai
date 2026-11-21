"""The self-upgrade pipeline must turn a scoped issue into a tested ai/* PR, and
must refuse anything that is not a small, tested, policy-compliant change."""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnidesk_agent.self_upgrade.pipeline import DiffPolicy, GitHubPublisher, ScriptedModelClient, UpgradeIssue, check_branch, propose, verify
from omnidesk_agent.self_upgrade.pipeline import cli
from omnidesk_agent.self_upgrade.pipeline.proposer import extract_diff
from omnidesk_agent.self_upgrade.pipeline.publisher import is_pipeline_branch

BUGGY = "def clamp(value, low, high):\n    return max(low, value)\n"
TEST = (
    "from omnidesk_agent.demo.calc import clamp\n\n\n"
    "def test_clamp_upper_bound():\n    assert clamp(15, 0, 10) == 10\n"
)
FIX_DIFF = """diff --git a/omnidesk_agent/demo/calc.py b/omnidesk_agent/demo/calc.py
--- a/omnidesk_agent/demo/calc.py
+++ b/omnidesk_agent/demo/calc.py
@@ -1,2 +1,2 @@
 def clamp(value, low, high):
-    return max(low, value)
+    return min(high, max(low, value))
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.email=ci@example.invalid", "-c", "user.name=ci", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "omnidesk_agent" / "demo").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "omnidesk_agent" / "__init__.py").write_text("")
    (root / "omnidesk_agent" / "demo" / "__init__.py").write_text("")
    (root / "omnidesk_agent" / "demo" / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_calc.py").write_text(TEST)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


ISSUE = UpgradeIssue(
    title="clamp ignores the upper bound", body="clamp(15, 0, 10) returns 15", source="github_issue", reference="#1",
    scope_paths=("omnidesk_agent/demo/",), failing_tests=("tests/test_calc.py::test_clamp_upper_bound",),
)


def test_diff_policy_refuses_restricted_destructive_and_oversized_changes():
    policy = DiffPolicy()

    def header(path, extra=""):
        return f"diff --git a/{path} b/{path}\n{extra}--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"

    for path in (".github/workflows/ci.yml", "omnidesk_agent/security/permissions.py", "AGENTS.md", "requirements.dev.lock",
                 "omnidesk_agent/self_upgrade/pipeline/diff_policy.py", "scripts/check_ga_release_gate.py"):
        assert not policy.check(header(path)).ok, path
    assert not policy.check(header("omnidesk_agent/x.py", "deleted file mode 100644\n")).ok
    assert not policy.check(header("omnidesk_agent/x.py", "old mode 100644\nnew mode 100755\n")).ok
    assert not policy.check("diff --git a/omnidesk_agent/a.py b/omnidesk_agent/b.py\nrename from omnidesk_agent/a.py\nrename to omnidesk_agent/b.py\n").ok
    assert not policy.check(header("omnidesk_agent/x.py") + "GIT binary patch\n").ok
    assert not policy.check(header("omnidesk_agent/../AGENTS.md")).ok
    assert not policy.check("+just text\n").ok
    big = "diff --git a/omnidesk_agent/x.py b/omnidesk_agent/x.py\n--- a/omnidesk_agent/x.py\n+++ b/omnidesk_agent/x.py\n" + "+x\n" * 401
    assert "401 changed lines (max 400)" in policy.check(big).violations
    assert not DiffPolicy(allowed_prefixes=("omnidesk_agent/demo/",)).check(header("omnidesk_agent/memory/store.py")).ok
    assert policy.check(FIX_DIFF).ok


@pytest.mark.parametrize("branch", ["main", "ai/x:main", "ai/../main", "refs/heads/ai/x", "+ai/x", "ai/X", "ai/x.lock", "feature/x"])
def test_only_plain_ai_branches_can_be_written(branch):
    with pytest.raises(PermissionError):
        check_branch(branch)


def test_model_answers_without_a_diff_block_are_rejected():
    with pytest.raises(ValueError, match="BEGIN_DIFF"):
        extract_diff("Sure! Here is how I would fix it ...")
    diff, summary = extract_diff(f"BEGIN_DIFF\n{FIX_DIFF}END_DIFF\nSUMMARY: clamp the upper bound\n")
    assert diff == FIX_DIFF and summary == "clamp the upper bound"


def test_end_to_end_issue_to_verified_patch_to_ai_pr(repo, tmp_path, monkeypatch):
    async def model(system, user):
        assert "clamp ignores the upper bound" in user and "FILE omnidesk_agent/demo/calc.py" in user
        return f"BEGIN_DIFF\n{FIX_DIFF}END_DIFF\nSUMMARY: clamp the value to the upper bound\n"

    proposal = asyncio.run(propose(repo, ISSUE, ScriptedModelClient(model), model_name="scripted/test"))
    bundle = verify(repo, proposal.diff, ISSUE, python=sys.executable)

    assert bundle.ok, bundle.reasons
    names = [s["name"] for s in bundle.steps]
    assert names[:2] == ["failing_tests_before_change", "apply"] and "full_suite" in names
    assert bundle.steps[0]["exit_code"] != 0  # the bug was reproduced before the fix
    assert bundle.targeted_tests == ["tests/test_calc.py", "tests/test_calc.py::test_clamp_upper_bound"]

    calls = []

    def transport(method, url, body, headers):
        calls.append((method, url, body))
        assert headers["Authorization"] == "Bearer app-installation-token"
        if method == "GET":
            return {"object": {"sha": bundle.base_sha}}
        if url.endswith("/graphql"):
            return {"data": {"createCommitOnBranch": {"commit": {"oid": "c" * 40, "url": "u"}}}}
        if url.endswith("/pulls"):
            return {"number": 4242, "html_url": "https://github.com/o/r/pull/4242"}
        return {}

    monkeypatch.setenv("OMNIDESK_UPGRADE_GITHUB_TOKEN", "app-installation-token")
    publisher = GitHubPublisher("Omniorganization/omni-desk-ai", transport=transport)
    files = {"omnidesk_agent/demo/calc.py": b"def clamp(value, low, high):\n    return min(high, max(low, value))\n"}
    result = publisher.publish("ai/self-upgrade-clamp-20261121", files=files, headline="fix", commit_body="b",
                               title="fix(self-upgrade): clamp", pr_body=cli.pr_body(ISSUE, proposal.to_dict(), bundle.to_dict()),
                               base_sha=bundle.base_sha)

    assert result.number == 4242
    methods_urls = [(m, u.rsplit("/repos/Omniorganization/omni-desk-ai", 1)[-1]) for m, u, _ in calls]
    assert methods_urls == [("POST", "/git/refs"), ("POST", "https://api.github.com/graphql"), ("POST", "/pulls"), ("POST", "/issues/4242/labels")]
    assert calls[0][2] == {"ref": "refs/heads/ai/self-upgrade-clamp-20261121", "sha": bundle.base_sha}
    commit_input = calls[1][2]["variables"]["input"]
    assert commit_input["expectedHeadOid"] == bundle.base_sha
    assert base64.b64decode(commit_input["fileChanges"]["additions"][0]["contents"]) == files["omnidesk_agent/demo/calc.py"]
    assert calls[2][2]["base"] == "main" and calls[2][2]["draft"] is True
    assert "self-upgrade-targeted-tests" in calls[2][2]["body"]
    assert not any("merge" in u for _, u, _ in calls)


def test_verify_refuses_untested_unreproduced_or_failing_changes(repo):
    no_test_issue = UpgradeIssue(title="refactor", body="b", source="manual", scope_paths=("omnidesk_agent/demo/",))
    (repo / "tests" / "test_calc.py").write_text("def test_unrelated():\n    assert True\n")
    _git(repo, "commit", "-q", "-am", "unrelated test")
    assert "no test covers the change; the diff must add or touch a test" in verify(repo, FIX_DIFF, no_test_issue, run_full_suite=False).reasons

    passing_already = UpgradeIssue(title="x", body="b", source="manual", failing_tests=("tests/test_calc.py::test_unrelated",))
    reasons = verify(repo, FIX_DIFF, passing_already, run_full_suite=False).reasons
    assert "the named failing tests pass before the change; the issue is not reproduced" in reasons

    breaking = FIX_DIFF.replace("+    return min(high, max(low, value))", "+    return None")
    breaking_test = breaking + (
        "diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_new.py\n@@ -0,0 +1,4 @@\n"
        "+from omnidesk_agent.demo.calc import clamp\n+\n+def test_clamp():\n+    assert clamp(1, 0, 2) == 1\n"
    )
    bundle = verify(repo, breaking_test, UpgradeIssue(title="x", body="b", source="manual"), run_full_suite=False)
    assert "targeted tests fail" in bundle.reasons and not bundle.ok

    restricted = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n"
    bundle = verify(repo, restricted, UpgradeIssue(title="x", body="b", source="manual"), run_full_suite=False)
    assert bundle.steps == [] and not bundle.ok  # refused before anything runs


def test_code_under_test_cannot_see_the_pipeline_credentials(repo, monkeypatch):
    monkeypatch.setenv("OMNIDESK_UPGRADE_GITHUB_TOKEN", "should-not-leak")
    monkeypatch.setenv("OMNIDESK_UPGRADE_MODEL_API_KEY", "should-not-leak")
    probe = (
        "diff --git a/tests/test_env.py b/tests/test_env.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_env.py\n@@ -0,0 +1,6 @@\n"
        "+import os\n+from omnidesk_agent.demo.calc import clamp\n+\n+def test_no_credentials():\n"
        "+    assert not any(k.startswith('OMNIDESK_UPGRADE') for k in os.environ)\n+    assert clamp(1, 0, 2) == 1\n"
    )
    bundle = verify(repo, probe, UpgradeIssue(title="env probe", body="b", source="manual"), run_full_suite=False)
    assert bundle.ok, bundle.reasons


def test_publish_step_refuses_evidence_for_another_patch(repo, tmp_path, monkeypatch):
    issue_file = tmp_path / "issue.json"
    issue_file.write_text(json.dumps(ISSUE.to_dict()))
    diff_file = tmp_path / "proposal.diff"
    diff_file.write_text(FIX_DIFF)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"ok": True, "patch_sha256": "0" * 64, "base_sha": "f" * 40}))
    code = cli.main(["--repo-root", str(repo), "publish", "--issue", str(issue_file), "--diff", str(diff_file),
                     "--evidence", str(evidence), "--repo", "o/r", "--dry-run"])
    assert code == 5


# ------------------------------------------------ review findings on #240

SMUGGLED = """diff --git a/omnidesk_agent/demo/calc.py b/omnidesk_agent/demo/calc.py
--- a/omnidesk_agent/security/permissions.py
+++ b/omnidesk_agent/security/permissions.py
@@ -1 +1 @@
-x
+y
"""


def test_path_headers_must_agree_with_the_git_header(repo):
    report = DiffPolicy().check(SMUGGLED)
    assert not report.ok and any("does not match diff --git header" in v for v in report.violations)
    bundle = verify(repo, SMUGGLED, UpgradeIssue(title="x", body="b", source="manual"), run_full_suite=False)
    assert bundle.steps == []  # refused before anything is applied or run


def test_publish_requires_a_passing_full_suite_in_the_evidence(repo, tmp_path):
    issue_file = tmp_path / "issue.json"
    issue_file.write_text(json.dumps(ISSUE.to_dict()))
    diff_file = tmp_path / "proposal.diff"
    diff_file.write_text(FIX_DIFF)
    partial = verify(repo, FIX_DIFF, ISSUE, run_full_suite=False)
    assert partial.ok and not partial.full_suite_passed
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(partial.to_dict()))
    args = ["--repo-root", str(repo), "publish", "--issue", str(issue_file), "--diff", str(diff_file),
            "--evidence", str(evidence), "--repo", "o/r", "--dry-run"]
    assert cli.main(args) == 5
    full = verify(repo, FIX_DIFF, ISSUE)
    evidence.write_text(json.dumps(full.to_dict()))
    assert full.full_suite_passed and cli.main(args) == 0


def test_branch_names_are_unique_per_attempt_and_name_the_issue():
    first = cli.branch_name(ISSUE, attempt="111")
    second = cli.branch_name(ISSUE, attempt="222")
    assert first != second and first.startswith("ai/self-upgrade-1-clamp-ignores-the-upper-bound-")
    check_branch(first)
    other = UpgradeIssue(title=ISSUE.title, body="b", source="github_issue", reference="#2")
    assert cli.branch_name(other, attempt="111") != first


def test_every_pipeline_branch_is_rechecked_and_no_human_branch_is():
    # agent-repair-pr.yml re-checks paths and tests only on pipeline branches. A
    # prefix match also caught the human PR that builds the pipeline
    # (ai/self-upgrade-pipeline-20261121); a miss would let a pipeline PR skip
    # the re-check, so branch_name() refuses to produce a name the check misses.
    issues = [
        ISSUE,
        UpgradeIssue(title="Fix 20261121 parser!!", body="b", source="github_issue", reference="#12345678901234"),
        UpgradeIssue(title="日本語のみ", body="b", source="repair_request", reference="repair-9f8e7d6c5b4a3210"),
        UpgradeIssue(title="x" * 200, body="b", source="manual"),
    ]
    for issue in issues:
        for attempt in ("111", "a" * 40, None):
            assert is_pipeline_branch(cli.branch_name(issue, attempt=attempt))
    for human in ("ai/self-upgrade-pipeline-20261121", "ai/self-upgrade-gate-fix-20261121",
                  "ai/learning-artifact-lifecycle-20261121", "ai/self-upgrade-1-fix-20261121-"):
        assert not is_pipeline_branch(human)
    with pytest.raises(ValueError):
        cli.branch_name(ISSUE, attempt="!!!")


def test_the_repair_workflow_gates_its_rechecks_on_the_exact_branch_format():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "agent-repair-pr.yml"
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["repair-policy"]["steps"]
    origin = next(step for step in steps if step.get("id") == "origin")
    assert "is_pipeline_branch" in origin["run"]
    rechecks = [step for step in steps if "RESTRICTED_PATTERNS" in step.get("run", "") or "self-upgrade-targeted-tests" in step.get("run", "")]
    assert len(rechecks) == 2
    assert all(step.get("if") == "steps.origin.outputs.pipeline == 'true'" for step in rechecks)
