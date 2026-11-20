"""Replay must score what the candidate policy makes the planner do, credit only
outcomes it can verify, and never turn failures into "improvement"."""
from __future__ import annotations

import asyncio

from omnidesk_agent.learning.daily_job import DailySelfLearningJob
from omnidesk_agent.memory.experience import ExperienceStore
from omnidesk_agent.self_learning.policy_hook import EMPTY_POLICY, PlannerPolicy
from omnidesk_agent.self_learning.replay import ReplayEngine, cases_from_runs

GUIDED = PlannerPolicy(policy_id="planner_candidate", version="pv_guided", skills_block="## extract\nUse browser.extract for prices.")
NOOP = PlannerPolicy(policy_id="planner_candidate", version="pv_noop", rules=("Be polite.",))


def _run(i: int, *, steps, oks, status):
    return {
        "id": f"run-{i}",
        "status": status,
        "original_message": {"channel": "telegram", "sender_id": f"u{i}", "text": f"price check {i}"},
        "plan_json": {"steps": steps},
        "results": [{"ok": ok} for ok in oks],
    }


def _history(n_failed: int = 12, n_ok: int = 12):
    """Recorded runs: clicking the price selector fails, extracting it works."""
    click = [{"tool": "browser", "action": "open", "args": {"url": "shop"}}, {"tool": "browser", "action": "click", "args": {"selector": "#price"}}]
    extract = [{"tool": "browser", "action": "open", "args": {"url": "shop"}}, {"tool": "browser", "action": "extract", "args": {"selector": "#price"}}]
    runs = [_run(i, steps=click, oks=[True, False], status="failed") for i in range(n_failed)]
    runs += [_run(100 + i, steps=extract, oks=[True, True], status="completed") for i in range(n_ok)]
    return runs


def scripted_planner(calls, history):
    """Stands in for the LLM planner (scripted model): without guidance it reproduces
    the recorded plan for the request; with the extract skill it follows the skill."""
    recorded = {run["original_message"]["text"]: run["plan_json"]["steps"] for run in history}

    async def plan(message, policy):
        calls.append((message["text"], policy.version))
        if "browser.extract" in policy.skills_block:
            return {"steps": [{"tool": "browser", "action": "open", "args": {"url": "shop"}},
                              {"tool": "browser", "action": "extract", "args": {"selector": "#price"}}]}
        return {"steps": recorded[message["text"]]}
    return plan


def test_candidate_that_steers_to_known_good_steps_is_improved_and_both_arms_are_planned():
    calls: list = []
    history = _history()
    cases, skipped = cases_from_runs(history)
    report = asyncio.run(ReplayEngine(scripted_planner(calls, history)).evaluate(cases, baseline=EMPTY_POLICY, candidate=GUIDED))

    assert report.verdict == "improved"
    assert report.wins == 12 and report.losses == 0 and report.ties == 12
    assert report.p_improved < 0.05
    assert {version for _, version in calls} == {EMPTY_POLICY.version, GUIDED.version}
    assert len(calls) == 2 * len(cases)


def test_a_candidate_that_changes_nothing_is_not_scored_as_improvement_on_failed_history():
    # The audited template replay gave an all-failed history a score of 1.0.
    history = _history(n_failed=20, n_ok=0)
    cases, _ = cases_from_runs(history)
    report = asyncio.run(ReplayEngine(scripted_planner([], history)).evaluate(cases, baseline=EMPTY_POLICY, candidate=NOOP))

    assert report.wins == 0 and report.policy_improvement_score == 0.0
    assert report.verdict == "non_inferior"


def test_unverifiable_candidate_plans_are_inconclusive_not_wins():
    async def novel(message, policy):
        tool_action = ("browser", "scrape") if policy is GUIDED else ("browser", "click")
        steps = [{"tool": "browser", "action": "open", "args": {"url": "shop"}}]
        steps.append({"tool": tool_action[0], "action": tool_action[1], "args": {"selector": "#price"}})
        return {"steps": steps}

    cases, _ = cases_from_runs(_history(n_failed=15, n_ok=0))
    report = asyncio.run(ReplayEngine(novel).evaluate(cases, baseline=EMPTY_POLICY, candidate=GUIDED))

    assert report.verdict == "inconclusive"
    assert report.candidate_unknown == 15 and report.wins == 0


def test_a_harmful_candidate_is_regressed():
    async def harmful(message, policy):
        action = "click" if policy is GUIDED else "extract"
        return {"steps": [{"tool": "browser", "action": "open", "args": {"url": "shop"}},
                          {"tool": "browser", "action": action, "args": {"selector": "#price"}}]}

    cases, _ = cases_from_runs(_history())
    report = asyncio.run(ReplayEngine(harmful).evaluate(cases, baseline=EMPTY_POLICY, candidate=GUIDED))

    assert report.verdict == "regressed" and report.losses == len(cases)


def test_planner_errors_count_against_the_arm_that_errors():
    async def flaky(message, policy):
        if policy is GUIDED:
            raise ValueError("candidate produced an invalid plan")
        return {"steps": [{"tool": "browser", "action": "open", "args": {"url": "shop"}},
                          {"tool": "browser", "action": "extract", "args": {"selector": "#price"}}]}

    cases, _ = cases_from_runs(_history(n_failed=0, n_ok=12))
    report = asyncio.run(ReplayEngine(flaky).evaluate(cases, baseline=EMPTY_POLICY, candidate=GUIDED))
    assert report.candidate_plan_errors == 12 and report.verdict == "regressed"


def test_runs_without_a_mappable_plan_are_skipped_and_reported():
    runs = _history(n_failed=1, n_ok=1)
    runs.append({"id": "x", "status": "completed", "original_message": {}, "plan_json": None, "results": [{"ok": True}]})
    runs.append({"id": "y", "status": "completed", "original_message": {}, "plan_json": {"steps": [{"tool": "a", "action": "b"}]},
                 "results": [{"ok": True}, {"ok": True}]})
    runs.append({"id": "z", "status": "waiting_approval", "original_message": {}, "plan_json": {"steps": []}, "results": []})
    cases, skipped = cases_from_runs(runs)
    assert len(cases) == 2
    assert skipped == {"no_plan": 1, "unmapped_results": 1, "not_final": 1}


def test_daily_job_records_replay_evaluations_on_registry_candidates(tmp_path):
    history = _history()
    with ExperienceStore(tmp_path / "memory.sqlite3") as memory:
        job = DailySelfLearningJob(memory, tmp_path, replay_plan_fn=scripted_planner([], history), replay_runs=lambda: history)
        artifact = job.policy_registry.register_candidate("skill", "extract", "Use browser.extract for prices.")
        report = job.run(days=7)

    replay = report["replay_report"]
    assert replay["status"] == "ran" and replay["evaluations"][0]["verdict"] == "improved"
    stored = job.policy_registry.get(artifact.artifact_id)
    assert stored.status == "evaluated" and stored.evaluation["method"] == "paired_replay_v1"
    assert job.policy_registry.active_policy().is_empty  # evaluation applies nothing
