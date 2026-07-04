from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from omnidesk_agent.self_learning.schemas import LearningProposal, SandboxValidationResult


class SandboxValidator:
    """Run bounded validation before a proposal can be approved or promoted."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def validate(self, proposal: LearningProposal, *, commands: Optional[list[list[str]]] = None, timeout: int = 180) -> SandboxValidationResult:
        commands = commands if commands is not None else self._default_commands(proposal)
        if not commands:
            if proposal.proposal_type in {"code_fix", "test_improvement"}:
                return SandboxValidationResult(
                    proposal_id=proposal.proposal_id,
                    ok=False,
                    validation_type="sandbox",
                    reason="code repair proposals require regression commands before PR promotion",
                )
            return SandboxValidationResult(
                proposal_id=proposal.proposal_id,
                ok=True,
                validation_type="static_review",
                reason="non-code draft validated for policy and approval gating",
            )

        results = [self._run(command, timeout=timeout) for command in commands]
        ok = all(item["exit_code"] == 0 for item in results)
        return SandboxValidationResult(
            proposal_id=proposal.proposal_id,
            ok=ok,
            validation_type="sandbox",
            command_results=results,
            reason="validation commands passed" if ok else "one or more validation commands failed",
        )

    @staticmethod
    def _default_commands(proposal: LearningProposal) -> list[list[str]]:
        commands: list[list[str]] = []
        for command in proposal.test_plan:
            parts = str(command).strip().split()
            if parts:
                commands.append(parts)
        return commands

    def _run(self, command: list[str], *, timeout: int) -> dict:
        try:
            result = subprocess.run(command, cwd=self.repo_root, text=True, capture_output=True, timeout=timeout, check=False)
            return {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "command": command,
                "exit_code": 124,
                "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "validation timed out",
            }
        except OSError as exc:
            return {
                "command": command,
                "exit_code": 127,
                "stdout": "",
                "stderr": str(exc),
            }
