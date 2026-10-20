from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from typing import Any, Coroutine, Optional, Protocol, TypeVar

from omnidesk_agent.self_learning.schemas import LearningProposal, SandboxValidationResult

T = TypeVar("T")


class CommandRunResult(Protocol):
    ok: bool
    output: str
    exit_code: int


class CommandRunner(Protocol):
    """Sandboxed, allowlisted command runner (e.g. self_upgrade.SandboxRunner).

    Injected by the caller: self_learning may not import self_upgrade.
    """

    async def run(self, command: Any, timeout: int = 120) -> CommandRunResult: ...


class SandboxValidator:
    """Run bounded validation before a proposal can be approved or promoted."""

    def __init__(self, repo_root: Path, *, runner: CommandRunner | None = None):
        self.repo_root = repo_root.resolve()
        self.runner = runner

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
        # Proposal test plans are generated content: run them through the same
        # allowlisted SandboxRunner (docker/remote backend in production) as other
        # upgrade checks instead of spawning them directly on the host.
        if self.runner is None:
            # Fail closed: never fall back to running generated commands on the host.
            return {"command": command, "exit_code": 126, "stdout": "", "stderr": "no sandbox runner configured for validation"}
        result = _run_sync(self.runner.run(command, timeout=timeout))
        return {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": result.output[-4000:],
            "stderr": "" if result.ok else result.output[-4000:],
        }


def _run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, also when called inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
