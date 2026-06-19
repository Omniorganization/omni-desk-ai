from __future__ import annotations
from dataclasses import dataclass, asdict

@dataclass
class PermissionDiff:
    added: list[str]
    removed: list[str]
    risk: str
    requires_human_approval: bool
    notes: list[str]
    def to_dict(self) -> dict:
        return asdict(self)

class PermissionDiffChecker:
    HIGH_RISK_PREFIXES = ("shell.", "computer.input", "computer.click", "browser.write", "browser.evaluate", "gmail.send", "gmail.modify", "files.write", "security.", "permissions.", "plugins.", "self_upgrade.")
    def compare(self, old_permissions: list[str], new_permissions: list[str]) -> PermissionDiff:
        old, new = set(old_permissions or []), set(new_permissions or [])
        added, removed = sorted(new - old), sorted(old - new)
        risk = self.calculate_permission_risk(added)
        notes = []
        if added: notes.append(f"permissions expanded: {', '.join(added)}")
        if removed: notes.append(f"permissions reduced: {', '.join(removed)}")
        if risk in {"high", "critical"}: notes.append("permission expansion requires human approval")
        return PermissionDiff(added, removed, risk, bool(added and risk in {"medium", "high", "critical"}), notes)
    def calculate_permission_risk(self, added: list[str]) -> str:
        if not added: return "low"
        if any(p.startswith("permissions.") or p.startswith("security.") or p.startswith("self_upgrade.") for p in added): return "critical"
        if any(p.startswith(prefix) for p in added for prefix in self.HIGH_RISK_PREFIXES): return "high"
        if any(p.endswith(".write") or p.endswith(".send") or p.endswith(".modify") for p in added): return "medium"
        return "low"
