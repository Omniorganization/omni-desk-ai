from __future__ import annotations

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

# Prepended to git argv at execution time, after the allowlist decision, so a
# repo-local .git/config cannot turn allowlisted git commands into code
# execution through the fsmonitor hook, repository hooks, the ext:: remote
# transport or an editor launch. Command-line config beats repository config.
GIT_EXEC_HARDENING_ARGS: tuple[str, ...] = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.ext.allow=never",
    "-c", "core.editor=:",
)


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


GIT_OPTION_DENY = OptionDenyRules(
    long=frozenset({
        "--output",        # git diff/log --output=<file> writes arbitrary files
        "--no-index",      # git diff --no-index reads paths outside the repo
        "--upload-pack",   # git pull/fetch spawn an arbitrary upload-pack command
        "--receive-pack",
        "--exec",
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
)

RUFF_OPTION_DENY = OptionDenyRules(
    long=frozenset({"--output-file", "--config", "--cache-dir"}),
    short=frozenset({"o"}),
    short_with_value=frozenset("o"),
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
    "compileall": _NO_OPTION_DENY,
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


def _safe_git_push_args(argv: Sequence[str]) -> bool:
    if len(argv) != 4:
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
        return _safe_git_push_args(argv)
    if prefix_list in (["pip", "install", "-e"], ["python3", "-m", "pip", "install", "-e"]):
        return _safe_editable_install_args(argv, prefix_list, workspace_root)
    return True


def _tool_name(argv: Sequence[str]) -> str:
    program = PurePath(str(argv[0])).name if argv else ""
    if program.startswith("python") and len(argv) >= 3 and argv[1] == "-m":
        return str(argv[2])
    return program


def _escapes_workspace(value: str) -> bool:
    """True for absolute paths and for values containing a `..` path segment."""
    if not value:
        return False
    if value.startswith(("/", "\\")) or PurePath(value).is_absolute():
        return True
    return ".." in value.replace("\\", "/").split("/")


def _short_cluster_denied(cluster: str, next_arg: str | None, rules: OptionDenyRules) -> bool:
    for index, letter in enumerate(cluster):
        if letter in rules.short:
            value = cluster[index + 1:] or (next_arg or "")
            allowed_values = rules.short_value_prefixes.get(letter, ())
            if not (allowed_values and value.startswith(allowed_values)):
                return True
        if letter in rules.short_with_value or letter.isdigit():
            # The rest of the cluster is this option's value, not more flags.
            return _escapes_workspace(cluster[index + 1:])
    return False


def _long_option_denied(name: str, rules: OptionDenyRules) -> bool:
    if name in rules.long:
        return True
    return rules.long_abbrev and len(name) > 2 and any(denied.startswith(name) for denied in rules.long)


def _extra_args_safe(argv: Sequence[str], prefix: Sequence[str]) -> bool:
    """Check every argument after the matched prefix.

    Rejects tool-specific dangerous options (output files, config overrides,
    command-spawning options) and any value that is an absolute path or has a
    `..` segment: paths handed to allowlisted tools must stay
    workspace-relative.
    """
    rules = _TOOL_OPTION_DENY.get(_tool_name(argv), GENERIC_OPTION_DENY)
    extra = [str(arg) for arg in argv[len(prefix):]]
    for index, arg in enumerate(extra):
        if "\0" in arg:
            return False
        if arg.startswith("--") and arg != "--":
            name, sep, value = arg.partition("=")
            if _long_option_denied(name, rules) or (sep and _escapes_workspace(value)):
                return False
            continue
        if arg.startswith("-") and len(arg) > 1:
            next_arg = extra[index + 1] if index + 1 < len(extra) else None
            if _short_cluster_denied(arg[1:], next_arg, rules):
                return False
            continue
        if _escapes_workspace(arg):
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
        and _extra_args_safe(argv, prefix)
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
        return [argv_list[0], *GIT_EXEC_HARDENING_ARGS, *argv_list[1:]]
    return argv_list
