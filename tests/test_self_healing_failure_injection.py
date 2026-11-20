"""Failure injection: each test breaks a real component and checks that the
self-healing loop detects it, takes a bounded action and verifies recovery."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.worker import WebhookWorker
from omnidesk_agent.self_healing.actuators import (
    CircuitActuator,
    ConfigRollbackActuator,
    DeadLetterRequeueActuator,
    EscalationActuator,
    RepairRequestActuator,
    StaleJobActuator,
    WorkerRestartActuator,
)
from omnidesk_agent.self_healing.policy import RuntimeSignal
from omnidesk_agent.self_healing.probes import (
    ConfigFileProbe,
    ConfigSnapshots,
    JobQueueProbe,
    ModelRouterProbe,
    WorkerProbe,
    WorkerRegistry,
)
from omnidesk_agent.self_healing.supervisor import HealingBudget, HealingSupervisor
from omnidesk_agent.self_healing.watchdog import GatewayWatchdog


class WorkerCrash(BaseException):
    """Escapes WebhookWorker's ``except Exception`` and kills its task, like a real crash."""


class Orchestrator:
    def __init__(self, failures):
        self.failures = list(failures)
        self.handled = []

    async def handle_message(self, message):
        if self.failures:
            raise self.failures.pop(0)
        self.handled.append(message.text)
        return {"status": "completed"}


def _register(registry, worker, name="webhook"):
    registry.register(name, start=worker.start, stop=worker.stop, is_alive=lambda: worker._task is not None and not worker._task.done())


def _audit(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def _until(predicate, *, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _no_sleep(_seconds):
    await asyncio.sleep(0.02)


def test_crashed_worker_is_restarted_and_its_stuck_job_completes(tmp_path):
    async def scenario():
        queue = JobQueue(tmp_path / "jobs.sqlite3", base_retry_seconds=0)
        orchestrator = Orchestrator([WorkerCrash("segfault in tool")])
        # Production lease: a restart alone does not recover a job claimed seconds ago.
        worker = WebhookWorker(queue, orchestrator, poll_interval_seconds=0.01, lease_seconds=300)
        registry = WorkerRegistry()
        _register(registry, worker)
        job = queue.enqueue(ChannelMessage(channel="telegram", sender_id="u1", text="order status", message_id="m1"))
        worker.start()
        await _until(lambda: worker._task.done())  # injected crash killed the worker
        assert queue.get(job["job_id"])["status"] == "running"

        # lease 0 stands in for "the lease has expired" without waiting 300 s.
        healer = HealingSupervisor(
            [WorkerProbe(registry), JobQueueProbe(queue, lease_seconds=0)],
            [WorkerRestartActuator(registry), StaleJobActuator(queue, lease_seconds=0, registry=registry, worker_name="webhook")],
            audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
        )
        records = await healer.tick()
        await _until(lambda: queue.get(job["job_id"])["status"] == "completed")
        await worker.stop()
        return records, orchestrator

    records, orchestrator = asyncio.run(scenario())
    actions = {(r["component"], r["decision"]["action"], r.get("verified")) for r in records if r["event"] == "acted"}
    assert ("worker:webhook", "restart_worker", True) in actions
    assert ("queue:stale_jobs", "recover_stale_jobs", True) in actions
    assert orchestrator.handled == ["order status"]


def test_transient_dead_letters_wait_for_the_dependency_then_requeue(tmp_path):
    async def scenario():
        queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
        transient = queue.enqueue(ChannelMessage(channel="telegram", sender_id="u1", text="a", message_id="m1"))
        permanent = queue.enqueue(ChannelMessage(channel="telegram", sender_id="u1", text="b", message_id="m2"))
        for job, error in ((transient, "TimeoutError: model provider timed out"), (permanent, "ValueError: payload schema invalid")):
            queue.claim_next()
            queue.fail(job["job_id"], error)
        provider_up = {"value": False}
        healer = HealingSupervisor(
            [JobQueueProbe(queue, dependency_healthy=lambda: provider_up["value"])],
            [DeadLetterRequeueActuator(queue)],
            audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
        )
        while_down = await healer.tick()
        provider_up["value"] = True
        after_recovery = await healer.tick()
        return queue, transient, permanent, while_down, after_recovery

    queue, transient, permanent, while_down, after_recovery = asyncio.run(scenario())
    assert while_down == []  # nothing is requeued into a provider that is still down
    acted = [r for r in after_recovery if r["event"] == "acted"]
    assert acted[0]["result"]["detail"]["requeued"] == [transient["job_id"]]
    assert acted[0]["verified"] is True
    assert queue.get(transient["job_id"])["status"] == "pending"
    assert queue.get(permanent["job_id"])["status"] == "dead_letter"  # left for a human


def test_corrupted_config_is_rolled_back_to_the_known_good_snapshot(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"gateway": {"port": 8080}}))
    snapshots = ConfigSnapshots(tmp_path / "snapshots")
    good = snapshots.save_known_good(config)
    config.write_text('{"gateway": {"port": ')  # injected: truncated deploy

    def validate(path: Path) -> None:
        json.loads(path.read_text())

    healer = HealingSupervisor(
        [ConfigFileProbe(config, validate, snapshots)],
        [ConfigRollbackActuator(snapshots, config)],
        audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
    )
    records = asyncio.run(healer.tick())

    assert records[0]["decision"]["action"] == "rollback_config" and records[0]["verified"] is True
    assert json.loads(config.read_text()) == {"gateway": {"port": 8080}}
    rejected = Path(records[0]["result"]["detail"]["rejected_copy"])
    assert rejected.read_text() == '{"gateway": {"port": '  # evidence of what was rejected
    assert records[0]["result"]["detail"]["snapshot"] == good


def test_a_worker_that_keeps_dying_is_quarantined_and_escalated_not_restarted_forever(tmp_path):
    registry = WorkerRegistry()
    starts = []
    registry.register("reconnect", start=lambda: starts.append(1), stop=lambda: None, is_alive=lambda: False)
    clock = {"now": 1_000.0}
    healer = HealingSupervisor(
        [WorkerProbe(registry)],
        [WorkerRestartActuator(registry, settle_seconds=0), EscalationActuator(tmp_path)],
        audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
        budget=HealingBudget(max_actions_per_component_per_hour=10, failed_verifications_before_escalation=3),
        clock=lambda: clock["now"],
    )

    async def scenario():
        for _ in range(8):
            await healer.tick()
            clock["now"] += 61  # past the 60 s restart cooldown

    asyncio.run(scenario())
    events = [r["event"] for r in _audit(tmp_path / "healing.jsonl")]
    assert len(starts) == 3
    assert events.count("quarantined") == 5
    escalations = (tmp_path / "escalations.jsonl").read_text().splitlines()
    assert len(escalations) == 1 and "did not recover worker:reconnect" in escalations[0]


def test_cooldown_and_hourly_limits_bound_actions(tmp_path):
    registry = WorkerRegistry()
    registry.register("w", start=lambda: None, stop=lambda: None, is_alive=lambda: False)
    clock = {"now": 0.0}
    healer = HealingSupervisor(
        [WorkerProbe(registry)], [WorkerRestartActuator(registry, settle_seconds=0)],
        audit_path=tmp_path / "h.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
        budget=HealingBudget(max_actions_per_component_per_hour=2, failed_verifications_before_escalation=99),
        clock=lambda: clock["now"],
    )

    async def scenario():
        for step in (0, 10, 70, 140):
            clock["now"] = step
            await healer.tick()

    asyncio.run(scenario())
    events = [(r["event"], r.get("reason")) for r in _audit(tmp_path / "h.jsonl")]
    assert events == [("acted", None), ("suppressed", "cooldown 60s"), ("acted", None), ("suppressed", "per-component hourly action limit")]


def test_observe_mode_decides_but_never_acts(tmp_path):
    registry = WorkerRegistry()
    starts = []
    registry.register("w", start=lambda: starts.append(1), stop=lambda: None, is_alive=lambda: False)
    healer = HealingSupervisor([WorkerProbe(registry)], [WorkerRestartActuator(registry)], audit_path=tmp_path / "h.jsonl", mode="observe")
    records = asyncio.run(healer.tick())
    assert records[0]["event"] == "observed" and records[0]["decision"]["action"] == "restart_worker"
    assert starts == []


def test_repeated_unknown_failures_file_one_repair_request_per_day(tmp_path):
    filed = []

    async def file_repair(request):
        filed.append(request["component"])
        return {"status": "pr_opened", "pr": 1234}

    class FailingComponent:
        name = "integration"

        async def collect(self):
            return [RuntimeSignal(component="integration:bigseller", consecutive_failures=5, health_ok=False)]

    healer = HealingSupervisor(
        [FailingComponent()], [RepairRequestActuator(tmp_path, file_repair=file_repair)],
        audit_path=tmp_path / "h.jsonl", mode="act", budget=HealingBudget(max_actions_per_component_per_hour=5),
    )
    first = asyncio.run(healer.tick())
    second = asyncio.run(healer.tick())
    assert filed == ["integration:bigseller"]
    assert first[0]["result"]["detail"]["status"] == "pr_opened"
    assert second[0]["result"]["detail"]["deduplicated"] is True
    assert first[0]["verified"] is None  # closed by a human reviewing the PR


def test_model_profile_circuit_is_opened_through_the_public_api_only(tmp_path):
    class Router:
        def __init__(self):
            self.opened = []

        def status(self):
            return {"circuit": {"primary": {"failures": 4.0, "opened_at": 1.0}}}

        def open_circuit(self, profile, *, reason):
            self.opened.append(profile)

    router = Router()
    healer = HealingSupervisor([ModelRouterProbe(router)], [CircuitActuator(router)], audit_path=tmp_path / "h.jsonl",
                               mode="act", verify_delay_seconds=0, sleep=_no_sleep)
    records = asyncio.run(healer.tick())
    assert router.opened == ["primary"] and records[0]["decision"]["action"] == "switch_model_profile"

    class LegacyRouter(Router):
        open_circuit = None  # today's router: no public API

    legacy = HealingSupervisor([ModelRouterProbe(LegacyRouter())], [CircuitActuator(LegacyRouter())],
                               audit_path=tmp_path / "h2.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep)
    result = asyncio.run(legacy.tick())[0]["result"]
    assert result["ok"] is False and "no public open_circuit API" in result["detail"]["error"]


class FakeHealth:
    def __init__(self, states):
        self.states = list(states)

    async def collect(self):
        ok = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return [RuntimeSignal(component="gateway", health_ok=ok)]


def test_watchdog_rolls_back_a_bad_config_restarts_and_verifies(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("gateway: {port: 8080}\n")
    clock = {"now": 0.0}
    restarts = []

    async def restart():
        restarts.append(config.read_text())
        return {"exit_code": 0}

    async def tick_sleep(seconds):
        clock["now"] += seconds

    # healthy 400 s (known-good saved), bad deploy -> 3 failures -> recovery -> healthy
    health = FakeHealth([True, True, False, False, False, False, True])
    dog = GatewayWatchdog(health, config, tmp_path / "state", restart=restart, failures_before_action=3,
                          good_after_seconds=300, clock=lambda: clock["now"], sleep=tick_sleep, poll_seconds=5)

    async def scenario():
        results = [await dog.check()]
        clock["now"] = 400
        results.append(await dog.check())
        config.write_text("gateway: {port: 80}\nsecrets: {missing: true}\n")  # injected bad deploy
        for _ in range(3):
            results.append(await dog.check())
        return results

    results = asyncio.run(scenario())
    assert results[1]["event"] == "known_good_saved"
    final = results[-1]
    assert final["event"] == "recovered" and final["containment_action"] == "rollback_config_and_restart"
    assert config.read_text() == "gateway: {port: 8080}\n"
    assert restarts == ["gateway: {port: 8080}\n"]  # restarted on the restored config
    assert Path(final["config_rollback"]["rejected_config"]).read_text().startswith("gateway: {port: 80}")


def test_watchdog_stops_restarting_after_the_hourly_limit(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("a: 1\n")
    clock = {"now": 0.0}

    async def restart():
        return {"exit_code": 0}

    async def tick_sleep(seconds):
        clock["now"] += seconds

    dog = GatewayWatchdog(FakeHealth([False]), config, tmp_path / "state", restart=restart, failures_before_action=1,
                          max_restarts_per_hour=2, recovery_timeout_seconds=10, poll_seconds=5,
                          clock=lambda: clock["now"], sleep=tick_sleep)

    async def scenario():
        return [await dog.check() for _ in range(4)]

    events = [r["event"] for r in asyncio.run(scenario())]
    assert events == ["recovery_failed", "recovery_failed", "escalated", "escalated"]


# ------------------------------------------------ review findings on #232

def test_watchdog_qualifies_each_config_separately(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("v: 1\n")
    clock = {"now": 0.0}

    async def restart():
        return {"exit_code": 0}

    dog = GatewayWatchdog(FakeHealth([True]), config, tmp_path / "state", restart=restart, good_after_seconds=300, clock=lambda: clock["now"])

    async def scenario():
        await dog.check()
        clock["now"] = 400
        assert (await dog.check())["event"] == "known_good_saved"
        good = dog.snapshots.known_good()
        config.write_text("v: 2-broken\n")  # deployed while the old process still answers
        clock["now"] = 410
        await dog.check()
        assert dog.snapshots.known_good() == good  # not promoted on the old process's uptime
        clock["now"] = 720
        await dog.check()
        assert dog.snapshots.known_good() != good  # promoted after its own 300 s

    asyncio.run(scenario())


def test_watchdog_survives_a_failing_restart_command(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("v: 1\n")

    async def restart():
        raise TimeoutError("systemctl timed out")

    dog = GatewayWatchdog(FakeHealth([False]), config, tmp_path / "state", restart=restart, failures_before_action=1)
    record = asyncio.run(dog.check())
    assert record["event"] == "recovery_failed" and record["restart"]["error_class"] == "TimeoutError"
    assert asyncio.run(dog.check())["event"] in {"recovery_failed", "escalated"}  # still checking


def test_a_full_page_of_dead_letters_is_escalated_not_reported_healthy(tmp_path):
    class Queue:
        def list(self, *, status, limit):
            return []

        def list_dead_letters(self, *, limit):
            return [{"id": str(i), "last_error": "ValueError: bad payload"} for i in range(200)]

    healer = HealingSupervisor([JobQueueProbe(Queue())], [EscalationActuator(tmp_path)], audit_path=tmp_path / "h.jsonl", mode="act")
    records = asyncio.run(healer.tick())
    escalations = [r for r in records if r["component"] == "queue:dead_letters"]
    assert escalations and escalations[0]["decision"]["action"] == "escalate_human_approval"


def test_requeue_batches_count_as_progress_not_failed_recovery(tmp_path):
    async def scenario():
        queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
        for i in range(61):
            queue.enqueue(ChannelMessage(channel="c", sender_id="u", text=f"m{i}", message_id=f"m{i}"))
            job = queue.claim_next()
            queue.fail(job["id"], "TimeoutError: provider timed out")
        clock = {"now": 0.0}
        healer = HealingSupervisor(
            [JobQueueProbe(queue)], [DeadLetterRequeueActuator(queue)],
            audit_path=tmp_path / "h.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
            budget=HealingBudget(max_actions_per_component_per_hour=10, failed_verifications_before_escalation=3),
            clock=lambda: clock["now"],
        )
        verified = []
        for _ in range(4):
            for record in await healer.tick():
                verified.append(record.get("verified"))
            clock["now"] += 301
        return queue, verified

    queue, verified = asyncio.run(scenario())
    assert verified == ["progress", "progress", "progress", True]
    assert queue.list_dead_letters(limit=200) == []


def test_stale_job_recovery_failure_still_restarts_the_worker(tmp_path):
    class BrokenQueue:
        def recover_stale_running(self, *, lease_seconds):
            raise RuntimeError("database is locked")

    registry = WorkerRegistry()
    state = {"alive": True, "starts": 0}
    registry.register("w", start=lambda: state.update(alive=True, starts=state["starts"] + 1), stop=lambda: state.update(alive=False),
                      is_alive=lambda: state["alive"])
    actuator = StaleJobActuator(BrokenQueue(), registry=registry, worker_name="w")
    decision = HealingSupervisor([], [], audit_path=tmp_path / "x.jsonl").controller.decide(
        RuntimeSignal(component="queue:stale_jobs", health_ok=False, metadata={"stale_count": 1}))
    try:
        asyncio.run(actuator.execute(decision, RuntimeSignal(component="queue:stale_jobs")))
    except RuntimeError:
        pass
    assert state == {"alive": True, "starts": 1}
