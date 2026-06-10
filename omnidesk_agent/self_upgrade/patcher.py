from __future__ import annotations

from pathlib import Path

from omnidesk_agent.self_upgrade.models import PatchResult, UpgradePlan


class UpgradePatcher:
    """Write proposal artifacts.

    This class does not autonomously rewrite source code. It creates reviewable
    artifacts first, so Level 2 remains human-auditable.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _safe_repo_path(self, relative: str) -> Path:
        path = (self.repo_root / relative).resolve()
        if not str(path).startswith(str(self.repo_root)):
            raise ValueError(f"Path outside repository: {path}")
        return path

    async def write_plan_artifacts(self, plan: UpgradePlan, output_dir: str = ".omnidesk/upgrades") -> PatchResult:
        out_dir = self._safe_repo_path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        plan_path = out_dir / "UPGRADE_PLAN.md"
        patch_path = out_dir / "PATCH_NOTES.md"
        plan_text = plan.to_markdown()
        patch_text = "\n".join([
            f"# Patch Notes: {plan.title}",
            "",
            "This Level 2 artifact is a proposal only. It does not apply source-code changes automatically.",
            "",
            "## Intended Files",
            *[f"- `{name}`" for name in plan.files_to_change],
            "",
            "## Review Requirement",
            "A human must approve the exact diff before it is committed, pushed, or opened as a PR.",
            "",
        ])
        plan_path.write_text(plan_text, encoding="utf-8")
        patch_path.write_text(patch_text, encoding="utf-8")
        return PatchResult(
            changed_files=[str(plan_path.relative_to(self.repo_root)), str(patch_path.relative_to(self.repo_root))],
            summary=f"Wrote upgrade proposal artifacts to {out_dir}",
            diff="",
        )
