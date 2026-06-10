from __future__ import annotations

from typing import Any

from omnidesk_agent.learning.failure_analyzer import FailureAnalyzer


class ExperienceExtractor:
    """Turn raw run output into structured experience memory."""

    def __init__(self, failure_analyzer: FailureAnalyzer | None = None):
        self.failure_analyzer = failure_analyzer or FailureAnalyzer()

    def extract(self, *, task: str, plan: dict[str, Any], run_result: dict[str, Any], tags: list[str] | None = None) -> dict[str, Any]:
        status = run_result.get("status", "unknown")
        success = status == "completed"
        failure_reason = None if success else self.failure_analyzer.classify(run_result, self._first_error(run_result))
        task_type = self._task_type(task, plan, run_result)
        solution_attempted = [
            f"{s.get('tool')}.{s.get('action')}"
            for s in run_result.get("steps", []) or []
            if s.get("tool") and s.get("action")
        ]
        recommended_next_action = "reuse successful plan" if success else self.failure_analyzer.recommended_upgrade(failure_reason or "unknown")
        return {
            "task_type": task_type,
            "goal": run_result.get("goal") or task,
            "success": success,
            "failure_reason": failure_reason,
            "solution_attempted": solution_attempted,
            "recommended_next_action": recommended_next_action,
            "risk_level": self._risk_level(run_result),
            "reusable_skill": bool(success and len(solution_attempted) >= 2),
            "success_score": 1.0 if success else 0.0,
            "tool_cost": self._tool_cost(run_result),
            "tags": tags or [run_result.get("plan_id", "run")],
            "raw_trace": {
                "status": status,
                "plan_id": run_result.get("plan_id"),
                "run_id": run_result.get("run_id"),
                "result_count": len(run_result.get("results", []) or []),
            },
        }

    @staticmethod
    def _first_error(run_result: dict[str, Any]) -> str:
        for result in run_result.get("results", []) or []:
            if result.get("error"):
                return str(result["error"])
        return str(run_result.get("error", ""))

    @staticmethod
    def _task_type(task: str, plan: dict[str, Any], run_result: dict[str, Any]) -> str:
        text = f"{task} {run_result.get('goal', '')}".lower()
        steps = run_result.get("steps", []) or []
        tools = {s.get("tool") for s in steps}
        if "browser" in tools or "chrome" in text:
            return "browser_automation"
        if "computer" in tools or "ui_bridge" in tools:
            return "computer_use"
        if "gmail" in tools or "email" in text or "邮件" in text:
            return "gmail_workflow"
        if "shell" in tools or "git" in tools:
            return "code_or_upgrade"
        if any(k in text for k in ["tiktok", "ads", "gmv"]):
            return "tiktok_ads_analysis"
        if any(k in text for k in ["小红书", "xiaohongshu"]):
            return "xiaohongshu_workflow"
        return "general"

    @staticmethod
    def _risk_level(run_result: dict[str, Any]) -> str:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        risk = "low"
        for step in run_result.get("steps", []) or []:
            r = step.get("risk", "medium")
            if order.get(r, 1) > order.get(risk, 0):
                risk = r
        return risk

    @staticmethod
    def _tool_cost(run_result: dict[str, Any]) -> float:
        # Placeholder for future token + latency + tool-call cost.
        return float(len(run_result.get("results", []) or []))
