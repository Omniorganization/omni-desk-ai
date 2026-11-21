"""Agent evaluation.

`agent_scenarios` / `agent_harness` / `python -m omnidesk_agent.evals`: the task-success suite. Scenarios run
through the real agent runtime with a scripted model (CI) or a live one, and report success rate, steps, planner
calls, cost and safety violations (see docs/AGENT_EVALS.md).

`scenario_loader`, `regression_suite`, `deterministic_assertions`, `llm_judge` and `promotion_gate` are the older
control-list harness: it scores whatever a caller-supplied runner reports, with an exact-match judge.
"""
