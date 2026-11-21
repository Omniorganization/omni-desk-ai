"""Failure injection: each test breaks a real component and checks that the
self-healing loop detects it, takes a bounded action and verifies recovery."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from omnidesk_agent.core.job_queue import JobQueue
from omnidesk_agent.core.models import ChannelMessage
from omnidesk_agent.core.worker import WebhookWorker
from omnidesk_agent.self_healing.actuators import (
    CircuitActuator,
    DeadLetterRequeueActuator,
    EscalationActuator,
    RepairRequestActuator,
    StaleJobActuator,
    WorkerRestartActuator,
)
from omnidesk_agent.self_healing.policy import RuntimeSelfHealingController, RuntimeSignal
from omnidesk_agent.self_healing.probes import (
    ConfigFileProbe,
    ConfigSnapshots,
    JobQueueProbe,
    ModelRouterProbe,
    RequeueLedger,
    WorkerProbe,
    WorkerRegistry,
    is_transient,
)
from omnidesk_agent.self_healing.supervisor import HealingBudget, HealingSupervisor
from omnidesk_agent.self_healing.watchdog import GatewayWatchdog, main as watchdog_main


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


def _age_lock(queue, job_id, seconds):
    """Stands in for "the lease has run out" without waiting: move the claim back in time."""
    with sqlite3.connect(queue.db_path) as con:
        con.execute("UPDATE jobs SET locked_at = locked_at - ? WHERE id = ?", (seconds, job_id))


def test_crashed_worker_is_restarted_and_its_orphaned_job_completes(tmp_path):
    async def scenario():
        queue = JobQueue(tmp_path / "jobs.sqlite3", base_retry_seconds=0)
        orchestrator = Orchestrator([WorkerCrash("segfault in tool")])
        worker = WebhookWorker(queue, orchestrator, poll_interval_seconds=0.01, lease_seconds=300)
        registry = WorkerRegistry()
        _register(registry, worker)
        job = queue.enqueue(ChannelMessage(channel="telegram", sender_id="u1", text="order status", message_id="m1"))
        worker.start()
        await _until(lambda: worker._task.done())  # injected crash killed the worker mid-job
        assert queue.get(job["job_id"])["status"] == "running"

        restarted = lambda: registry.restarted_at("webhook")  # noqa: E731 - the SQLite queue has this one worker
        healer = HealingSupervisor(
            [WorkerProbe(registry), JobQueueProbe(queue, dependency_healthy=lambda: True, ledger=RequeueLedger(tmp_path / "r.json"), orphaned_before=restarted)],
            [WorkerRestartActuator(registry), StaleJobActuator(queue, orphaned_before=restarted)],
            audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
        )
        records = await healer.tick()  # restarts the worker; the orphaned job is still inside its lease
        assert queue.get(job["job_id"])["status"] == "running"
        _age_lock(queue, job["job_id"], 400)
        records += await healer.tick()  # past its lease, and claimed before the restart: nothing runs it
        await _until(lambda: queue.get(job["job_id"])["status"] == "completed")
        await worker.stop()
        return records, orchestrator

    records, orchestrator = asyncio.run(scenario())
    actions = {(r["component"], r["decision"]["action"], r.get("verified")) for r in records if r["event"] == "acted"}
    assert ("worker:webhook", "restart_worker", True) in actions
    assert ("queue:stale_jobs", "recover_stale_jobs", True) in actions
    assert orchestrator.handled == ["order status"]


def test_a_healthy_job_that_outlives_its_lease_is_neither_cancelled_nor_run_twice(tmp_path):
    # Coordinator review on #232: the queue never renews a lease, so "past its lease"
    # also describes a healthy long run. Re-running it would repeat its side effects.
    async def scenario():
        queue = JobQueue(tmp_path / "jobs.sqlite3", base_retry_seconds=0)
        release, calls = asyncio.Event(), []

        class SlowOrchestrator:
            async def handle_message(self, message):
                calls.append("start")
                await release.wait()
                calls.append("done")
                return {"status": "completed"}

        worker = WebhookWorker(queue, SlowOrchestrator(), poll_interval_seconds=0.01, lease_seconds=300)
        registry = WorkerRegistry()
        _register(registry, worker)
        job = queue.enqueue(ChannelMessage(channel="telegram", sender_id="u1", text="long report", message_id="m1"))
        worker.start()
        await _until(lambda: calls == ["start"])
        _age_lock(queue, job["job_id"], 400)
        restarted = lambda: registry.restarted_at("webhook")  # noqa: E731
        healer = HealingSupervisor(
            [WorkerProbe(registry), JobQueueProbe(queue, dependency_healthy=lambda: True, ledger=RequeueLedger(tmp_path / "r.json"), orphaned_before=restarted)],
            [WorkerRestartActuator(registry), StaleJobActuator(queue, orphaned_before=restarted), EscalationActuator(tmp_path)],
            audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep,
        )
        records = await healer.tick()
        during = queue.get(job["job_id"])["status"]
        release.set()
        await _until(lambda: queue.get(job["job_id"])["status"] == "completed")
        await worker.stop()
        return records, calls, during

    records, calls, during = asyncio.run(scenario())
    stale = [r for r in records if r["component"] == "queue:stale_jobs"]
    assert stale[0]["decision"]["action"] == "escalate_human_approval"
    assert during == "running" and calls == ["start", "done"]
    assert (tmp_path / "escalations.jsonl").exists()


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
            [JobQueueProbe(queue, dependency_healthy=lambda: provider_up["value"], ledger=RequeueLedger(tmp_path / "r.json"))],
            [DeadLetterRequeueActuator(queue, ledger=RequeueLedger(tmp_path / "r.json"))],
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


def test_a_corrupted_config_is_escalated_with_its_snapshot_never_rolled_back_autonomously(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"gateway": {"port": 8080}}))
    snapshots = ConfigSnapshots(tmp_path / "snapshots")
    good = snapshots.save_known_good(config.read_bytes())
    config.write_text('{"gateway": {"port": ')  # injected: truncated deploy

    def validate(path: Path) -> None:
        json.loads(path.read_text())

    healer = HealingSupervisor([ConfigFileProbe(config, validate, snapshots)], [EscalationActuator(tmp_path)],
                               audit_path=tmp_path / "healing.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep)
    record = asyncio.run(healer.tick())[0]
    # The config holds security, sandbox and approval settings: going back to an
    # older one is an owner's decision (AGENTS.md), as a release rollback is.
    assert record["decision"]["action"] == "rollback_config" and record["decision"]["requires_human_approval"] is True
    assert record["event"] == "acted" and record["verified"] is None
    assert config.read_text() == '{"gateway": {"port": '  # untouched
    escalation = json.loads((tmp_path / "escalations.jsonl").read_text().splitlines()[0])
    assert escalation["decision"]["rollback_ref"] == good

    no_snapshot = HealingSupervisor([ConfigFileProbe(config, validate, ConfigSnapshots(tmp_path / "empty"))], [EscalationActuator(tmp_path / "e2")],
                                    audit_path=tmp_path / "h2.jsonl", mode="act")
    assert asyncio.run(no_snapshot.tick())[0]["decision"]["action"] == "escalate_human_approval"  # never "healthy"


def test_decisions_that_need_a_human_never_reach_an_actuator_that_makes_the_change(tmp_path):
    class ConfigRestorer:  # would restore the config itself
        actions = frozenset({"rollback_config"})

        async def execute(self, decision, signal):
            raise AssertionError("must never run")

    with pytest.raises(ValueError, match="only request actuators"):
        HealingSupervisor([], [ConfigRestorer()], audit_path=tmp_path / "h.jsonl", mode="act")


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


def test_watchdog_restarts_but_never_reverts_a_config_on_its_own(tmp_path):
    loose = "security: {allow_local_admin_without_token: true}\n"
    tight = "security: {allow_local_admin_without_token: false}\n"
    config = tmp_path / "config.yaml"
    config.write_text(loose)
    clock = {"now": 0.0}
    restarts = []

    async def restart():
        restarts.append(config.read_text())
        return {"exit_code": 0}

    async def tick_sleep(seconds):
        clock["now"] += seconds

    # crash -> restart -> healthy 300 s (known-good) -> operator tightens security -> slow start fails a check
    health = FakeHealth([False, True, True, False, False, False, True])
    dog = GatewayWatchdog(health, config, tmp_path / "state", restart=restart, failures_before_action=1,
                          good_after_seconds=300, clock=lambda: clock["now"], sleep=tick_sleep, poll_seconds=5)

    async def scenario():
        results = [await dog.check()]
        clock["now"] = 400
        results.append(await dog.check())
        config.write_text(tight)
        results.append(await dog.check())
        return results

    first, saved, after_change = asyncio.run(scenario())
    assert first["event"] == "recovered" and saved["event"] == "known_good_saved"
    # Coordinator review on #232: a valid security-tightening change must not be undone
    # without a human. The watchdog restarts, reports the difference and leaves the file.
    assert after_change["event"] == "recovered" and after_change["containment_action"] == "restart"
    assert after_change["config_differs_from_known_good"]["known_good"] == saved["snapshot"]
    assert config.read_text() == tight and restarts == [loose, tight]

    # Going back is an explicit operator command; the replaced file is kept.
    assert watchdog_main(["--config", str(config), "--state-dir", str(tmp_path / "state"), "--restore-known-good"]) == 0
    assert config.read_text() == loose
    assert watchdog_main(["--config", str(config), "--state-dir", str(tmp_path / "empty"), "--restore-known-good"]) == 1


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

def test_a_config_staged_while_the_old_process_serves_never_becomes_known_good(tmp_path):
    # Coordinator review on #232: the gateway reads its config only at start, so a
    # file that sat on disk while the old process kept serving proves nothing.
    config = tmp_path / "config.yaml"
    config.write_text("v: 1\n")
    clock = {"now": 0.0}

    async def restart():
        return {"exit_code": 0}

    async def tick_sleep(seconds):
        clock["now"] += seconds

    dog = GatewayWatchdog(FakeHealth([True, True, True, False, True]), config, tmp_path / "state", restart=restart,
                          failures_before_action=1, good_after_seconds=300, clock=lambda: clock["now"], sleep=tick_sleep, poll_seconds=5)

    async def scenario():
        await dog.check()  # a process this watchdog did not start: what it loaded is unknown
        clock["now"] = 400
        assert (await dog.check())["event"] == "healthy" and dog.snapshots.known_good() is None
        config.write_text("v: 2\n")  # staged while the old process keeps serving
        clock["now"] = 800
        assert (await dog.check())["event"] == "healthy" and dog.snapshots.known_good() is None
        clock["now"] = 900
        assert (await dog.check())["event"] == "recovered"  # restarted by the watchdog: v2 is what runs now
        clock["now"] = 1300
        assert (await dog.check())["event"] == "known_good_saved"
        assert (dog.snapshots.root / f"{dog.snapshots.known_good()}.snapshot").read_text() == "v: 2\n"

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

    healer = HealingSupervisor([JobQueueProbe(Queue(), dependency_healthy=lambda: True, ledger=RequeueLedger(tmp_path / "r.json"))],
                               [EscalationActuator(tmp_path)], audit_path=tmp_path / "h.jsonl", mode="act")
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
            [JobQueueProbe(queue, dependency_healthy=lambda: True, ledger=RequeueLedger(tmp_path / "r.json"))],
            [DeadLetterRequeueActuator(queue, ledger=RequeueLedger(tmp_path / "r.json"))],
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


def test_stale_recovery_only_touches_jobs_claimed_before_the_restart():
    class Queue:
        def __init__(self):
            self.listed = {"old": 100.0, "new": 600.0, "reclaimed": 100.0}
            self.now = {"old": 100.0, "new": 600.0, "reclaimed": 700.0}  # "reclaimed" moved on after the listing
            self.failed = []

        def list(self, *, status, limit):
            return [{"id": job_id, "status": "running", "locked_at": at} for job_id, at in self.listed.items()]

        def get(self, job_id):
            return {"id": job_id, "status": "running", "locked_at": self.now[job_id]}

        def fail(self, job_id, error):
            self.failed.append(job_id)

    decision = RuntimeSelfHealingController().decide(
        RuntimeSignal(component="queue:stale_jobs", health_ok=False, metadata={"stale_count": 3, "orphaned_count": 2}))
    signal = RuntimeSignal(component="queue:stale_jobs")
    queue = Queue()
    # Restarted at 500; at 1000 every job is past a 300 s lease, but only "old" lost its worker.
    result = asyncio.run(StaleJobActuator(queue, orphaned_before=lambda: 500.0, clock=lambda: 1000.0).execute(decision, signal))
    assert result.ok and queue.failed == ["old"]
    unknown = asyncio.run(StaleJobActuator(queue, orphaned_before=lambda: None, clock=lambda: 1000.0).execute(decision, signal))
    assert unknown.ok is False and queue.failed == ["old"]  # no restart known: nothing is touched


def test_only_real_transient_errors_count_not_ids_that_contain_status_digits():
    transient = [
        "TimeoutError: model provider timed out",
        "Server error '503 Service Unavailable' for url 'https://api.example/v1'",
        "Client error '429 Too Many Requests' for url 'https://api.example/v1'",
        "HTTP Error 502: Bad Gateway",
        "status_code=504",
        "[Errno 111] Connection refused",
        "Connection reset by peer",
        "Temporary failure in name resolution",
        "rate limit exceeded",
    ]
    permanent = [
        "ValueError: order 15029 not found",
        "HTTPError 404 for /api/orders/50321",
        "KeyError: 'sku-4291'",
        "ValueError: invalid timeout parameter in payload",
        "timeout_seconds Field required",
        # a job that hung or crashed its worker is a poison message, not a transient failure
        "stale running job recovered after 300s lease and moved to dead_letter",
        "",
    ]
    assert [error for error in transient if not is_transient(error)] == []
    assert [error for error in permanent if is_transient(error)] == []


def test_a_job_that_fails_again_after_its_automatic_requeue_stays_for_a_human(tmp_path):
    # Coordinator review on #232: requeue resets the job's retries and error, so
    # without a durable per-job record one job was requeued 18 times in 6 hours.
    async def scenario():
        queue = JobQueue(tmp_path / "jobs.sqlite3", max_retries=0)
        job = queue.enqueue(ChannelMessage(channel="telegram", sender_id="u1", text="a", message_id="m1"))
        queue.claim_next()
        queue.fail(job["job_id"], "TimeoutError: tool call timed out")
        ledger = RequeueLedger(tmp_path / "requeues.json")
        clock = {"now": 0.0}
        healer = HealingSupervisor(
            [JobQueueProbe(queue, dependency_healthy=lambda: True, ledger=ledger)],
            [DeadLetterRequeueActuator(queue, ledger=ledger), EscalationActuator(tmp_path)],
            audit_path=tmp_path / "h.jsonl", mode="act", verify_delay_seconds=0, sleep=_no_sleep, clock=lambda: clock["now"],
        )
        first = await healer.tick()
        queue.claim_next()
        queue.fail(job["job_id"], "TimeoutError: tool call timed out")  # fails the same way again
        later = []
        for _ in range(6):
            clock["now"] += 3601
            later += await healer.tick()
        return queue, job["job_id"], first, later

    queue, job_id, first, later = asyncio.run(scenario())
    assert [(r["decision"]["action"], r["verified"]) for r in first] == [("requeue_dead_letters", True)]
    assert {r["decision"]["action"] for r in later} == {"escalate_human_approval"}
    assert queue.get(job_id)["status"] == "dead_letter" and queue.get(job_id)["last_error"] == "TimeoutError: tool call timed out"
    assert json.loads((tmp_path / "requeues.json").read_text())[job_id][0]["last_error"] == "TimeoutError: tool call timed out"


def test_waiting_for_a_dependency_is_not_taken_for_recovery(tmp_path):
    class Probe:
        name = "queue"

        async def collect(self):
            return [RuntimeSignal(component="queue:dead_letters", health_ok=False,
                                  metadata={"transient_count": 1, "retryable_count": 1, "exhausted_count": 0, "dependency_healthy": False})]

    healer = HealingSupervisor([Probe()], [], audit_path=tmp_path / "h.jsonl", mode="act")
    healer._failed_verifications["queue:dead_letters"] = 2
    healer._quarantined.add("queue:dead_letters")
    assert asyncio.run(healer.tick()) == []
    assert "queue:dead_letters" in healer._quarantined and healer._failed_verifications["queue:dead_letters"] == 2


def test_config_snapshots_are_private_and_a_restore_keeps_the_config_mode(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("v: 1\n")
    config.chmod(0o600)
    snapshots = ConfigSnapshots(tmp_path / "snapshots")
    good = snapshots.save_known_good(config.read_bytes())
    config.write_text("v: 2\n")
    snapshots.restore(good, config)
    assert config.read_text() == "v: 1\n"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600  # a restore used to leave a 0600 config world-readable
    assert stat.S_IMODE(snapshots.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((snapshots.root / f"{good}.snapshot").stat().st_mode) == 0o600
