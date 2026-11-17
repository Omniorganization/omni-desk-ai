from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omnidesk_agent.config import PermissionConfig, SandboxConfig
from omnidesk_agent.security.command_policy import (
    GIT_EXEC_HARDENING_ARGS,
    SAFE_CI_ALLOWED_PREFIXES,
    UPGRADE_ALLOWED_PREFIXES,
    argv_allowed,
    harden_exec_argv,
    readonly_command,
)
from omnidesk_agent.tools.shell import ShellTool

ALL_PREFIXES = SAFE_CI_ALLOWED_PREFIXES + UPGRADE_ALLOWED_PREFIXES


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / "reports").mkdir()
    return ws


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "branch", "-D", "main"],
        ["git", "branch", "-d", "main"],
        ["git", "branch", "-m", "main", "renamed"],
        ["git", "branch", "-M", "renamed"],
        ["git", "branch", "-C", "main", "copy"],
        ["git", "branch", "-vD", "main"],
        ["git", "branch", "-f", "main", "HEAD~3"],
        ["git", "branch", "--delete", "--force", "main"],
        ["git", "branch", "--del", "main"],
        ["git", "branch", "--move", "main", "renamed"],
        ["git", "branch", "--copy", "main", "copy"],
        ["git", "branch", "--set-upstream-to=origin/evil"],
        ["git", "branch", "-u", "origin/evil"],
        ["git", "branch", "--unset-upstream"],
        ["git", "branch", "--edit-description"],
        ["git", "branch", "--track", "new", "origin/main"],
        ["git", "branch", "new-branch"],
        ["git", "branch", "-v", "new-branch"],
        ["git", "branch", "-l", "new-branch"],
        ["git", "branch", "--format", "%(refname)", "new-branch"],
        ["git", "branch", "--", "new-branch"],
    ],
)
def test_git_branch_writes_are_neither_allowed_nor_readonly(argv, workspace):
    assert not argv_allowed(argv, SAFE_CI_ALLOWED_PREFIXES, workspace_root=workspace)
    # The remote runner checks without a workspace root.
    assert not argv_allowed(argv, SAFE_CI_ALLOWED_PREFIXES)
    assert not readonly_command(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "branch"],
        ["git", "branch", "-a"],
        ["git", "branch", "-avv"],
        ["git", "branch", "--remotes", "--verbose"],
        ["git", "branch", "--show-current"],
        ["git", "branch", "--list", "ai/*"],
        ["git", "branch", "-l"],
        ["git", "branch", "--merged"],
        ["git", "branch", "--merged", "main"],
        ["git", "branch", "--contains=HEAD"],
        ["git", "branch", "--format=%(refname:short)", "--sort=-committerdate"],
    ],
)
def test_git_branch_listing_stays_allowed_and_readonly(argv, workspace):
    assert argv_allowed(argv, SAFE_CI_ALLOWED_PREFIXES, workspace_root=workspace)
    assert readonly_command(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "pull", "https://evil.example/repo.git"],
        ["git", "pull", "https://evil.example/repo.git", "main"],
        ["git", "pull", "git@evil.example:repo.git"],
        ["git", "pull", "ext::sh -c id"],
        ["git", "pull", "file:///tmp/repo"],
        ["git", "pull", "../other-repo"],
        ["git", "pull", "upstream", "main"],
        ["git", "pull", "evil"],
        ["git", "pull", "origin", "main", "other"],
        ["git", "pull", "origin", "+main"],
        ["git", "pull", "origin", "main:main"],
        ["git", "pull", "origin", "HEAD@{1}"],
        ["git", "pull", "origin", "a..b"],
        ["git", "pull", "--rebase=interactive", "origin", "main"],
        ["git", "pull", "--set-upstream", "origin", "main"],
        ["git", "pull", "-s", "ours", "origin", "main"],
        ["git", "pull", "--strategy-option=theirs", "origin", "main"],
        ["git", "pull", "--recurse-submodules", "origin", "main"],
        ["git", "pull", "--depth=1", "origin"],
        ["git", "pull", "--", "origin", "main"],
    ],
)
def test_git_pull_only_fetches_the_push_remote(argv, workspace):
    assert not argv_allowed(argv, ALL_PREFIXES, workspace_root=workspace)


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "pull"],
        ["git", "pull", "origin"],
        ["git", "pull", "origin", "main"],
        ["git", "pull", "--ff-only", "origin", "ai/tools-fix"],
        ["git", "pull", "--rebase", "origin", "main"],
        ["git", "pull", "-q", "--no-rebase", "origin", "refs/heads/main"],
    ],
)
def test_git_pull_from_origin_stays_allowed(argv, workspace):
    assert argv_allowed(argv, ALL_PREFIXES, workspace_root=workspace)


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "--junitxml=.git/hooks/pre-commit"],
        ["pytest", "--junitxml", ".git/hooks/pre-commit"],
        ["pytest", "--junit-xml=.GIT/config"],
        ["pytest", "--junitxml=sub/.Git/config"],
        ["pytest", "--junitxml=.git./config"],
        ["pytest", "--junitxml=.git /config"],
        ["pytest", "--junitxml=GIT~1/config"],
        ["pytest", "--junitxml=.git\\hooks\\pre-commit"],
        ["pytest", "--log-file=.git/info/exclude"],
        ["pytest", "--cov-report=xml:.git/hooks/pre-commit"],
        ["pytest", "--cov-report=html:../outside"],
        ["pytest", "--cov-report=xml:/tmp/outside.xml"],
        ["pytest", ".git/hooks"],
        ["ruff", "check", ".git"],
        ["python", "-m", "compileall", ".git/hooks"],
        ["git", "log", "-O.git/x"],
        ["git", "diff", "--", ".git/config"],
        ["git", "add", ".git/config"],
        ["git", "commit", "-F", ".git/COMMIT_EDITMSG"],
        # pytest expands ~ and environment variables in path options
        ["pytest", "--junitxml=$TMPDIR/out.xml"],
        ["pytest", "--junitxml=${HOME}/out.xml"],
        ["pytest", "--junitxml=~/out.xml"],
        ["pytest", "--junitxml", "~/out.xml"],
        ["pytest", "--cov-report=xml:$HOME/out.xml"],
        # argument files and file lists bypass per-argument checks
        ["pytest", "@args.txt"],
        ["pytest", "-q", "@reports/args.txt"],
        ["python", "-m", "compileall", "-i", "list.txt"],
        ["python3", "-m", "compileall", "-qi", "list.txt"],
    ],
)
def test_path_values_may_not_reach_git_metadata_or_leave_workspace(argv, workspace):
    assert not argv_allowed(argv, ALL_PREFIXES, workspace_root=workspace)
    assert not argv_allowed(argv, ALL_PREFIXES)


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "--junitxml=reports/junit.xml"],
        ["pytest", "--junitxml", "reports/junit.xml"],
        ["pytest", "--cov-report=xml:reports/coverage.xml"],
        ["pytest", "tests/test_x.py::test_name"],
        ["pytest", "-W", "ignore::DeprecationWarning"],
        ["pytest", "-k", "not slow"],
        ["ruff", "check", ".github/workflows", ".gitignore", "docs/.gitkeep"],
        ["git", "commit", "-m", "fix: keep .git out of reach"],
        ["git", "log", "--format=%H:%s"],
        ["git", "push", "origin", "HEAD:codex/source-fix"],
        # git never reads a path out of these values
        ["git", "commit", "-m", "See https://example.com/issue/1"],
        ["git", "commit", "-m", "fix:/tmp handling, costs $5"],
        ["git", "log", "--grep=https://example.com/x"],
        ["git", "log", ":/fix bug"],
        ["git", "diff", "HEAD~1:omnidesk_agent/config.py", "HEAD:omnidesk_agent/config.py"],
    ],
)
def test_ordinary_path_values_stay_allowed(argv, workspace):
    assert argv_allowed(argv, ALL_PREFIXES, workspace_root=workspace)


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform without symlinks
        pytest.skip(f"symlinks unavailable: {exc}")


def test_symlinked_path_values_are_resolved(workspace, tmp_path):
    _symlink(workspace / "hooks-link", workspace / ".git" / "hooks")
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink(workspace / "out-link", outside)

    assert not argv_allowed(["pytest", "--junitxml=hooks-link/pre-commit"], ALL_PREFIXES, workspace_root=workspace)
    assert not argv_allowed(["pytest", "--junitxml", "hooks-link/pre-commit"], ALL_PREFIXES, workspace_root=workspace)
    assert not argv_allowed(["pytest", "--cov-report=xml:out-link/cov.xml"], ALL_PREFIXES, workspace_root=workspace)
    assert argv_allowed(["pytest", "--junitxml=reports/pre-commit"], ALL_PREFIXES, workspace_root=workspace)


@pytest.mark.parametrize("name", ["origin", "origin.git"])
@pytest.mark.parametrize(
    "argv",
    [["git", "pull", "origin", "main"], ["git", "pull", "origin"], ["git", "push", "origin", "HEAD:codex/source-fix"]],
)
def test_workspace_path_cannot_stand_in_for_the_origin_remote(argv, name, workspace):
    # Without a configured `origin` remote git would use this path instead.
    (workspace / name).mkdir()
    assert not argv_allowed(argv, ALL_PREFIXES, workspace_root=workspace)


def test_exec_hardening_pins_transport_programs():
    assert harden_exec_argv(["git", "pull", "origin", "main"]) == [
        "git", *GIT_EXEC_HARDENING_ARGS, "pull", "--upload-pack=git-upload-pack", "origin", "main",
    ]
    assert harden_exec_argv(["git", "push", "origin", "HEAD:codex/x"]) == [
        "git", *GIT_EXEC_HARDENING_ARGS, "push", "--receive-pack=git-receive-pack", "origin", "HEAD:codex/x",
    ]
    assert harden_exec_argv(["git", "diff", "--stat"]) == ["git", *GIT_EXEC_HARDENING_ARGS, "diff", "--no-ext-diff", "--stat"]
    assert harden_exec_argv(["git", "status"]) == ["git", *GIT_EXEC_HARDENING_ARGS, "status"]
    assert "sequence.editor=:" in GIT_EXEC_HARDENING_ARGS
    assert not argv_allowed(["git", "diff", "--ext-diff"], SAFE_CI_ALLOWED_PREFIXES)
    assert not argv_allowed(["git", "log", "-p", "--ext-diff"], SAFE_CI_ALLOWED_PREFIXES)


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
def test_exec_hardening_ignores_repo_config_commands(tmp_path: Path):
    git = shutil.which("git")
    assert git is not None
    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]

    def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run([git, *ident, *args], cwd=cwd, capture_output=True, text=True, check=False)

    def hook(name: str) -> Path:
        script = tmp_path / f"{name}.sh"
        script.write_text(f"#!/bin/sh\ntouch '{tmp_path / name}.ran'\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def fired(name: str) -> bool:
        marker = tmp_path / f"{name}.ran"
        ran = marker.exists()
        marker.unlink(missing_ok=True)
        return ran

    upstream = tmp_path / "upstream"
    run("init", "-q", "-b", "main", str(upstream), cwd=tmp_path)
    run("commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
    run("clone", "-q", str(upstream), "work", cwd=tmp_path)
    work = tmp_path / "work"
    (work / "f.txt").write_text("a\n", encoding="utf-8")
    run("add", "f.txt", cwd=work)
    run("commit", "-q", "-m", "f", cwd=work)

    def diverge() -> None:
        run("commit", "-q", "--allow-empty", "-m", "upstream", cwd=upstream)
        run("commit", "-q", "--allow-empty", "-m", "local", cwd=work)

    # Repository config that would make allowlisted git run a command.
    cases = [
        ("diff.external", "diff.external", str(hook("ext")), ["git", "diff", "f.txt"], "ext"),
        ("uploadpack", "remote.origin.uploadpack", f"{hook('upload')}; git-upload-pack", ["git", "pull", "--no-rebase", "--no-edit", "origin", "main"], "upload"),
        ("receivepack", "remote.origin.receivepack", f"{hook('receive')}; git-receive-pack", ["git", "push", "origin", "HEAD:codex/hardening"], "receive"),
        ("sequence.editor", "sequence.editor", str(hook("seq")), ["git", "pull"], "seq"),
    ]
    for label, key, value, argv, name in cases:
        run("config", key, value, cwd=work)
        if label == "sequence.editor":
            run("config", "pull.rebase", "interactive", cwd=work)
        assert argv_allowed(argv, ALL_PREFIXES, workspace_root=work), argv
        for hardened in (False, True):
            if label == "diff.external":
                (work / "f.txt").write_text(f"{hardened}\n", encoding="utf-8")
            if label in {"uploadpack", "sequence.editor"}:
                diverge()
            exec_argv = harden_exec_argv(argv) if hardened else argv
            result = run(*exec_argv[1:], cwd=work)
            run("rebase", "--abort", cwd=work)
            run("checkout", "-q", "--", "f.txt", cwd=work)
            if not hardened and not fired(name):
                pytest.skip(f"this git build does not run {label} from repo config: {result.stderr}")
            if hardened:
                assert not fired(name), (label, result.stderr)
                assert result.returncode == 0, (label, result.stderr)
                if label == "diff.external":
                    assert "+True" in result.stdout
        run("config", "--unset", key, cwd=work)
    assert run("ls-remote", str(upstream), "refs/heads/codex/hardening", cwd=work).stdout.strip()


def test_shell_tool_applies_git_scope(workspace):
    tool = ShellTool(workspace, PermissionConfig(default_mode="allow", audit_log=workspace / "audit.log"), SandboxConfig(backend="docker"))
    assert not tool._allowed(["git", "branch", "-D", "main"])
    assert not tool._allowed(["pytest", "--junitxml=.git/hooks/pre-commit"])
    assert tool._allowed(["git", "branch", "-a"])
    docker = tool._docker_argv(["git", "branch", "-a"])
    assert any("dst=/workspace" in part and part.endswith("readonly") for part in docker)
    assert not tool._is_readonly_command(["git", "branch", "-m", "main", "x"])
