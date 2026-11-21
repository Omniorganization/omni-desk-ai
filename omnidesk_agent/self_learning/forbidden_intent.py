"""Detect forbidden intent in learning and upgrade proposal text.

This is a coarse, fail-closed lint, not the safety boundary. Learned artefacts
and upgrade proposals can only change planner guidance or open reviewable PRs;
approvals, permissions, the sandbox and audit logging are enforced by code that
no proposal text can reach. Text matching is defence in depth: it rejects
proposals that ask for a forbidden change in so many words, including common
rephrasings ("skip the approval step", "remove the audit logs", "force push to
main"), and it errs towards blocking. Blocked text needs a human to reword it.
"""
from __future__ import annotations

import re
from typing import Iterable

_WORD = re.compile(r"[a-z0-9]+|[一-鿿]+")

# (verbs, objects, window): the rule fires when an object token appears within
# ``window`` tokens after a verb token. ``both_ways`` also checks before the verb.
_Rule = tuple[frozenset[str], frozenset[str], int, bool]


def _rule(verbs: Iterable[str], objects: Iterable[str], window: int, *, both_ways: bool = False) -> _Rule:
    return frozenset(verbs), frozenset(objects), window, both_ways


_BYPASS_VERBS = (
    "bypass", "bypassing", "bypasses", "bypassed", "skip", "skips", "skipping", "skipped",
    "circumvent", "circumventing", "avoid", "avoiding", "ignore", "ignoring", "ignores",
    "disable", "disabling", "disables", "remove", "removing", "removes", "drop", "dropping",
    "omit", "omitting", "suppress", "suppressing", "override", "overriding",
)
_APPROVAL_OBJECTS = (
    "approval", "approvals", "approver", "approvers", "review", "reviews", "reviewer", "reviewers",
    "signoff", "consent",
)
_DELETE_VERBS = (
    "delete", "deleting", "deletes", "deleted", "remove", "removing", "removes", "erase", "erasing",
    "clear", "clearing", "purge", "purging", "truncate", "truncating", "wipe", "wiping", "drop",
    "dropping", "disable", "disabling", "shred",
)
_WEAKEN_VERBS = (
    "disable", "disabling", "disables", "remove", "removing", "bypass", "bypassing", "skip",
    "skipping", "weaken", "weakening", "lower", "lowering", "relax", "relaxing", "loosen",
    "loosening", "downgrade", "downgrading",
)
_MAIN_WRITE_VERBS = (
    "commit", "commits", "committing", "committed", "push", "pushes", "pushing", "pushed",
    "patch", "patching", "write", "writes", "writing", "apply", "applying", "merge", "merges",
    "merging", "merged", "force",
)

RULES: dict[str, tuple[_Rule, ...]] = {
    "auto_merge": (
        _rule(("merge", "merges", "merging", "merged"),
              ("automatically", "automatic", "auto", "autonomously", "unreviewed", "unattended", "immediately"),
              3, both_ways=True),
        _rule(("automerge", "automerging"), ("automerge", "automerging", "enable", "enabled", "on"), 3, both_ways=True),
    ),
    "direct_main_patch": (
        _rule(_MAIN_WRITE_VERBS, ("main", "master"), 3),
    ),
    "force_push": (
        _rule(("force", "forced", "forcing"), ("push", "pushes", "pushing", "pushed"), 2),
    ),
    "direct_production_write": (
        _rule(("write", "writes", "apply", "applies", "push", "pushes", "deploy", "deploys", "ship", "ships"),
              ("directly", "straight"), 3),
    ),
    "bypass_approval": (
        _rule(_BYPASS_VERBS, _APPROVAL_OBJECTS, 4),
        _rule(("without", "no"), _APPROVAL_OBJECTS, 2),
    ),
    "delete_audit_log": (
        _rule(_DELETE_VERBS, ("audit", "audits", "auditing"), 4),
    ),
    "disable_rollback": (
        _rule(("disable", "disabling", "skip", "skipping", "remove", "removing", "drop", "bypass", "avoid",
               "without", "no"), ("rollback", "rollbacks"), 3),
    ),
    "weaken_security_control": (
        _rule(_WEAKEN_VERBS, ("permission", "permissions", "rbac", "sandbox", "csp", "signature",
                              "signatures", "allowlist", "security"), 4),
    ),
}

# Literal fragments for languages and spellings the token rules do not cover.
PHRASES: dict[str, tuple[str, ...]] = {
    "auto_merge": ("auto merge", "auto-merge", "automerge", "merge without review", "merge without approval", "自动合并"),
    "direct_main_patch": ("direct main patch", "直接推送到main", "直接推送到 main", "直接提交到main", "直接提交到 main"),
    "force_push": ("force-push", "push --force", "push -f", "强制推送"),
    "direct_production_write": ("production without staging", "direct production write", "直接写入生产", "直接发布到生产"),
    "bypass_approval": ("no human approval", "跳过审批", "绕过审批", "无需审批", "不需要审批", "跳过审核", "绕过审核"),
    "delete_audit_log": ("clear audit history", "删除审计", "清除审计", "清空审计"),
    "disable_rollback": ("without rollback", "关闭回滚", "禁用回滚", "跳过回滚"),
    "weaken_security_control": ("关闭沙箱", "禁用沙箱", "降低安全", "关闭权限"),
}


def tokens(text: str) -> list[str]:
    """Lower-case word tokens; ``_``, ``-`` and punctuation are separators."""
    return _WORD.findall(str(text or "").lower().replace("_", " "))


def forbidden_intents(text: str, *, only: Iterable[str] | None = None) -> list[str]:
    """Return the forbidden-intent codes found in ``text`` (stable order)."""
    wanted = set(only) if only is not None else None
    words = tokens(text)
    collapsed = " ".join(str(text or "").lower().split())
    hits: list[str] = []
    for code in RULES.keys() | PHRASES.keys():
        if wanted is not None and code not in wanted:
            continue
        if any(phrase in collapsed for phrase in PHRASES.get(code, ())) or any(
            _hit(words, rule) for rule in RULES.get(code, ())
        ):
            hits.append(code)
    return sorted(hits)


def _hit(words: list[str], rule: _Rule) -> bool:
    verbs, objects, window, both_ways = rule
    for index, word in enumerate(words):
        if word not in verbs:
            continue
        after = words[index + 1: index + 1 + window]
        if any(candidate in objects for candidate in after):
            return True
        if both_ways and any(candidate in objects for candidate in words[max(0, index - window): index]):
            return True
    return False
