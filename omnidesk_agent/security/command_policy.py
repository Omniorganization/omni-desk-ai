from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Iterable, Mapping, Sequence

# Shared command allowlist used by ShellTool and the remote sandbox runner.
# Keep this narrow: all entries are argv prefixes, never shell strings.
SAFE_CI_ALLOWED_PREFIXES: list[list[str]] = [
    ["python", "-m", "compileall"],
    ["python3", "-m", "compileall"],
    ["pytest"],
    ["ruff", "check"],
    ["git", "status"],
    ["git", "diff"],
    ["git", "branch"],
    ["git", "log"],
    ["git", "ls-tree"],
]

UPGRADE_ALLOWED_PREFIXES: list[list[str]] = [
    ["git", "add"],
    ["git", "commit"],
    ["git", "pull"],
    ["git", "push"],
    ["pip", "install", "-e"],
    ["python3", "-m", "pip", "install", "-e"],
]

READONLY_PREFIXES: list[list[str]] = [
    ["python", "-m", "compileall"],
    ["python3", "-m", "compileall"],
    ["pytest"],
    ["ruff", "check"],
    ["git", "status"],
    ["git", "diff"],
    ["git", "branch"],
    ["git", "log"],
    ["git", "ls-tree"],
]

_FORBIDDEN_GIT_PATHSPEC_CHARS = frozenset("\0*?[]{}")

# `git branch` is allowlisted for listing only: creating, deleting, renaming,
# copying, force-moving or re-tracking a branch rewrites refs or config.
# Options are matched exactly, so abbreviations such as `--del` are refused.
_GIT_BRANCH_LIST_FLAGS = frozenset({
    "--all", "--remotes", "--verbose", "--list", "--show-current", "--ignore-case",
    "--no-color", "--no-column", "--no-abbrev", "--omit-empty",
})
_GIT_BRANCH_LIST_SHORT = frozenset("arvli")
_GIT_BRANCH_FILTER_OPTIONS = frozenset({"--contains", "--no-contains", "--merged", "--no-merged", "--points-at"})
_GIT_BRANCH_LIST_VALUE_OPTIONS = _GIT_BRANCH_FILTER_OPTIONS | {"--sort", "--format", "--abbrev", "--color", "--column"}

# `git pull` may only fetch from the remote that `git push` may target.
_GIT_PULL_REMOTE = "origin"
_GIT_PULL_FLAGS = frozenset({"--ff-only", "--rebase", "--no-rebase", "--no-edit", "--quiet", "-q", "--verbose", "-v"})
_GIT_REF_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")

# Separators inside option values that can introduce a path, e.g.
# `--cov-report=xml:<path>` or `-Wignore::<path>`.
_VALUE_PATH_SEPARATORS = re.compile(r"[:=,]")
# pytest expands `~` and environment variables in some path options.
_PATH_EXPANSION = re.compile(r"^~|\$(\w|\{)")

# Prepended to git argv at execution time, after the allowlist decision, so a
# repo-local .git/config cannot turn allowlisted git commands into code
# execution through the fsmonitor hook, repository hooks, the ext:: remote
# transport or an editor launch. Command-line config beats repository config.
GIT_EXEC_HARDENING_ARGS: tuple[str, ...] = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.ext.allow=never",
    "-c", "core.editor=:",
    "-c", "sequence.editor=:",
)
# Inserted right after the subcommand. remote.<name>.uploadpack/receivepack
# keep the first configured value, so a `-c` override cannot replace a
# repository one, and an empty `-c diff.external=` breaks every diff.
GIT_SUBCOMMAND_HARDENING_ARGS: dict[str, tuple[str, ...]] = {
    "pull": ("--upload-pack=git-upload-pack",),
    "push": ("--receive-pack=git-receive-pack",),
    "diff": ("--no-ext-diff",),
}


@dataclass(frozen=True)
class OptionDenyRules:
    """Options that must never follow an allowlisted argv prefix.

    ``long`` names are denied as ``--opt`` and ``--opt=value``. If the tool
    accepts unambiguous abbreviations of long options (git parse-options),
    every abbreviation of a denied name is denied as well. ``short`` letters
    are denied anywhere in a short-option cluster up to the first letter that
    consumes the rest of the cluster as its value (``short_with_value``).
    ``short_value_prefixes`` re-allows a denied short option whose value
    starts with one of the listed prefixes (pytest ``-p no:<plugin>``).
    """

    long: frozenset[str] = frozenset()
    short: frozenset[str] = frozenset()
    short_with_value: frozenset[str] = frozenset()
    long_abbrev: bool = False
    short_value_prefixes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # False for tools that never treat `a:b`, `a=b`, `a,b`, `~` or `$VAR`
    # inside a value as a path (git: commit messages, `<rev>:<path>`).
    values_may_hide_paths: bool = True
    # Arguments starting with this prefix are read as more arguments from a
    # file, which would bypass every check here (pytest `@args.txt`).
    arg_file_prefix: str = ""


GIT_OPTION_DENY = OptionDenyRules(
    long=frozenset({
        "--output",        # git diff/log --output=<file> writes arbitrary files
        "--no-index",      # git diff --no-index reads paths outside the repo
        "--upload-pack",   # git pull/fetch spawn an arbitrary upload-pack command
        "--receive-pack",
        "--exec",
        "--ext-diff",      # runs diff.external / diff drivers from repo config
        "--config-env",
        "--git-dir",
        "--work-tree",
        "--exec-path",
    }),
    # git -c <name>=<value> overrides config; commit -c opens an editor.
    short=frozenset({"c"}),
    # Letters that take the rest of the cluster as a value in every
    # allowlisted subcommand. Flags in some subcommands (-s, -n) are left out
    # on purpose so `git commit -nc HEAD` is still scanned past -n.
    short_with_value=frozenset("SGOUMCBlLmFtXu"),
    long_abbrev=True,
    values_may_hide_paths=False,
)

PYTEST_OPTION_DENY = OptionDenyRules(
    long=frozenset({
        "--basetemp",      # pytest rm -rf's the basetemp directory
        "--rootdir",
        "--confcutdir",
        "--config-file",
        "--inifile",
        "--override-ini",
        "--pyargs",
    }),
    short=frozenset({"p", "c", "o"}),
    short_with_value=frozenset("kmrWnpco"),
    short_value_prefixes={"p": ("no:",)},
    arg_file_prefix="@",
)

RUFF_OPTION_DENY = OptionDenyRules(
    long=frozenset({"--output-file", "--config", "--cache-dir"}),
    short=frozenset({"o"}),
    short_with_value=frozenset("o"),
)

# compileall -i reads more target paths from a file.
COMPILEALL_OPTION_DENY = OptionDenyRules(
    short=frozenset({"i"}),
    short_with_value=frozenset("rdspxijoe"),
)

_NO_OPTION_DENY = OptionDenyRules()

# Conservative default for operator-configured prefixes that are not modelled.
GENERIC_OPTION_DENY = OptionDenyRules(
    long=frozenset({"--output", "--output-file", "--config"}),
    short=frozenset({"o"}),
)

_TOOL_OPTION_DENY: dict[str, OptionDenyRules] = {
    "git": GIT_OPTION_DENY,
    "pytest": PYTEST_OPTION_DENY,
    "py.test": PYTEST_OPTION_DENY,
    "ruff": RUFF_OPTION_DENY,
    "compileall": COMPILEALL_OPTION_DENY,
    "pip": _NO_OPTION_DENY,
}


def argv_matches_prefix(argv: Sequence[str], prefix: Sequence[str]) -> bool:
    return len(argv) >= len(prefix) and list(argv[: len(prefix)]) == list(prefix)


def _safe_git_path_arg(value: str) -> bool:
    if value in {"", ".", "--all", "-A"} or value.startswith("-") or value.startswith(":"):
        return False
    if any(char in value for char in _FORBIDDEN_GIT_PATHSPEC_CHARS):
        return False
    path = PurePath(value)
    if path.is_absolute():
        return False
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)


def _safe_git_add_args(argv: Sequence[str], workspace_root: Path | None = None) -> bool:
    if len(argv) <= 2:
        return False
    del workspace_root
    for raw in argv[2:]:
        if not _safe_git_path_arg(str(raw)):
            return False
    return True


def _safe_git_branch_args(argv: Sequence[str]) -> bool:
    list_mode = False
    positionals = 0
    for raw in argv[2:]:
        arg = str(raw)
        if arg.startswith("--"):
            name, sep, _ = arg.partition("=")
            if name in _GIT_BRANCH_LIST_VALUE_OPTIONS and sep:
                continue
            if arg in _GIT_BRANCH_FILTER_OPTIONS:
                # Bare filters switch git to list mode and may take the next
                # argument as their commit.
                list_mode = True
                continue
            if arg in _GIT_BRANCH_LIST_FLAGS:
                list_mode = list_mode or arg == "--list"
                continue
            return False
        if arg.startswith("-") and len(arg) > 1:
            if not set(arg[1:]) <= _GIT_BRANCH_LIST_SHORT:
                return False
            continue
        positionals += 1
    # Without --list a positional argument names a branch to create. `-l`
    # does not count: before git 2.20 it meant --create-reflog.
    return positionals == 0 or list_mode


def _safe_git_ref_name(value: str) -> bool:
    return (
        bool(_GIT_REF_NAME.fullmatch(value))
        and ".." not in value
        and "//" not in value
        and not value.endswith(("/", ".", ".lock"))
    )


def _remote_name_shadowed(workspace_root: Path | None) -> bool:
    # Without a configured `origin` remote, git reads `origin` as a path.
    if workspace_root is None:
        return False
    root = workspace_root.expanduser()
    return any(os.path.lexists(root / name) for name in (_GIT_PULL_REMOTE, f"{_GIT_PULL_REMOTE}.git"))


def _safe_git_pull_args(argv: Sequence[str], workspace_root: Path | None = None) -> bool:
    positionals: list[str] = []
    for raw in argv[2:]:
        arg = str(raw)
        if arg.startswith("-"):
            if arg not in _GIT_PULL_FLAGS:
                return False
            continue
        positionals.append(arg)
    if not positionals:
        # The current branch's configured upstream; no repository on argv.
        return True
    if positionals[0] != _GIT_PULL_REMOTE or len(positionals) > 2 or _remote_name_shadowed(workspace_root):
        return False
    return len(positionals) == 1 or _safe_git_ref_name(positionals[1])


def _safe_git_push_args(argv: Sequence[str], workspace_root: Path | None = None) -> bool:
    if len(argv) != 4 or _remote_name_shadowed(workspace_root):
        return False
    remote = str(argv[2])
    refspec = str(argv[3])
    return remote == "origin" and (
        refspec.startswith("HEAD:codex/")
        or refspec.startswith("HEAD:repair/")
        or refspec.startswith("HEAD:self-upgrade/")
    )


def _safe_editable_install_args(argv: Sequence[str], prefix: Sequence[str], workspace_root: Path | None = None) -> bool:
    # Exactly `<prefix> .`: extra requirements, index URLs or -r files would
    # turn the editable self-install into an arbitrary package install.
    if [str(arg) for arg in argv] != [*prefix, "."]:
        return False
    return workspace_root is not None and workspace_root.expanduser().resolve().exists()


def _prefix_args_safe(argv: Sequence[str], prefix: Sequence[str], workspace_root: Path | None = None) -> bool:
    prefix_list = list(prefix)
    if prefix_list == ["git", "add"]:
        return _safe_git_add_args(argv, workspace_root)
    if prefix_list == ["git", "push"]:
        return _safe_git_push_args(argv, workspace_root)
    if prefix_list == ["git", "branch"]:
        return _safe_git_branch_args(argv)
    if prefix_list == ["git", "pull"]:
        return _safe_git_pull_args(argv, workspace_root)
    if prefix_list in (["pip", "install", "-e"], ["python3", "-m", "pip", "install", "-e"]):
        return _safe_editable_install_args(argv, prefix_list, workspace_root)
    return True


def _tool_name(argv: Sequence[str]) -> str:
    program = PurePath(str(argv[0])).name if argv else ""
    if program.startswith("python") and len(argv) >= 3 and argv[1] == "-m":
        return str(argv[2])
    return program


def _is_git_dir_segment(part: str) -> bool:
    # Same rule as FilesTool: case-insensitive (macOS/Windows), plus the
    # trailing dots/spaces Windows strips from a name and its 8.3 short name.
    return part.rstrip(". ").lower() in {".git", "git~1"}


def _path_escapes(value: str, workspace_root: Path | None, expansion: bool = True) -> bool:
    if value.startswith(("/", "\\")) or PurePath(value).is_absolute():
        return True
    if expansion and _PATH_EXPANSION.search(value):
        return True
    parts = value.replace("\\", "/").split("/")
    if ".." in parts or any(_is_git_dir_segment(part) for part in parts):
        return True
    if workspace_root is None:
        return False
    # A symlink inside the workspace can still point outside it or into .git.
    root = workspace_root.expanduser().resolve()
    try:
        relative = (root / value).resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return True
    return any(_is_git_dir_segment(part) for part in relative.parts)


def _escapes_workspace(value: str, workspace_root: Path | None = None, hidden_paths: bool = True) -> bool:
    """True if any path in ``value`` leaves the workspace or touches git metadata.

    Rejects absolute paths, `..` segments and `.git` segments. With
    ``hidden_paths`` also `~`/`$VAR` expansion and paths behind a `:`, `=` or
    `,` inside a value. With a workspace root, symlinks are resolved too.
    """
    if not value:
        return False
    parts: list[str] = [value]
    if hidden_paths:
        parts.extend(str(part) for part in _VALUE_PATH_SEPARATORS.split(value))
    return any(_path_escapes(part, workspace_root, hidden_paths) for part in parts if part)


def _short_cluster_denied(cluster: str, next_arg: str | None, rules: OptionDenyRules, workspace_root: Path | None = None) -> bool:
    for index, letter in enumerate(cluster):
        if letter in rules.short:
            value = cluster[index + 1:] or (next_arg or "")
            allowed_values = rules.short_value_prefixes.get(letter, ())
            if not (allowed_values and value.startswith(allowed_values)):
                return True
        if letter in rules.short_with_value or letter.isdigit():
            # The rest of the cluster is this option's value, not more flags.
            return _escapes_workspace(cluster[index + 1:], workspace_root, rules.values_may_hide_paths)
    return False


def _long_option_denied(name: str, rules: OptionDenyRules) -> bool:
    if name in rules.long:
        return True
    return rules.long_abbrev and len(name) > 2 and any(denied.startswith(name) for denied in rules.long)


def _extra_args_safe(argv: Sequence[str], prefix: Sequence[str], workspace_root: Path | None = None) -> bool:
    """Check every argument after the matched prefix.

    Rejects tool-specific dangerous options (output files, config overrides,
    command-spawning options) and any value that is an absolute path or has a
    `..` or `.git` segment: paths handed to allowlisted tools must stay
    workspace-relative and out of git metadata.
    """
    rules = _TOOL_OPTION_DENY.get(_tool_name(argv), GENERIC_OPTION_DENY)
    extra = [str(arg) for arg in argv[len(prefix):]]
    hidden = rules.values_may_hide_paths
    for index, arg in enumerate(extra):
        if "\0" in arg:
            return False
        if rules.arg_file_prefix and arg.startswith(rules.arg_file_prefix):
            return False
        if arg.startswith("--") and arg != "--":
            name, sep, value = arg.partition("=")
            if _long_option_denied(name, rules) or (sep and _escapes_workspace(value, workspace_root, hidden)):
                return False
            continue
        if arg.startswith("-") and len(arg) > 1:
            next_arg = extra[index + 1] if index + 1 < len(extra) else None
            if _short_cluster_denied(arg[1:], next_arg, rules, workspace_root):
                return False
            continue
        if _escapes_workspace(arg, workspace_root, hidden):
            return False
    return True


def argv_allowed(
    argv: Sequence[str],
    prefixes: Iterable[Sequence[str]] = SAFE_CI_ALLOWED_PREFIXES,
    *,
    workspace_root: Path | None = None,
) -> bool:
    return any(
        argv_matches_prefix(argv, prefix)
        and _prefix_args_safe(argv, prefix, workspace_root)
        and _extra_args_safe(argv, prefix, workspace_root)
        for prefix in prefixes
    )


def readonly_command(argv: Sequence[str]) -> bool:
    return argv_allowed(argv, READONLY_PREFIXES)


def harden_exec_argv(argv: Sequence[str]) -> list[str]:
    """Return the argv to spawn for a command that already passed the allowlist.

    git invocations get repo-config code-execution hooks disabled. Run the
    allowlist check on the original argv first: the injected ``-c`` flags are
    intentionally rejected by the policy itself.
    """
    argv_list = [str(arg) for arg in argv]
    if argv_list and PurePath(argv_list[0]).name == "git":
        rest = argv_list[1:]
        if rest:
            rest = [rest[0], *GIT_SUBCOMMAND_HARDENING_ARGS.get(rest[0], ()), *rest[1:]]
        return [argv_list[0], *GIT_EXEC_HARDENING_ARGS, *rest]
    return argv_list
