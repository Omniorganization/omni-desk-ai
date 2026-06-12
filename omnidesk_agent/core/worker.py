from __future__ import annotations

import asyncio
from typing import Any, Optional

from omnidesk_agent.core.job_queue import JobQueue


class WebhookWorker:
    """Background worker that drains queued webhook jobs into the orchestrator."""

    def __init__(self, queue: JobQueue, orchestrator: Any, *, poll_interval_seconds: float = 0.25, lease_seconds: int = 300):
        self.queue = queue
        self.orchestrator = orchestrator
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop: asyncio.Event | None = None
        self._stop_loop: asyncio.AbstractEventLoop | None = None

    def _get_stop_event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        if self._stop is None or self._stop_loop is not loop:
            self._stop = asyncio.Event()
            self._stop_loop = loop
        return self._stop

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.queue.recover_stale_running(lease_seconds=self.lease_seconds)
        self._get_stop_event().clear()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._get_stop_event().set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass

    async def run_forever(self) -> None:
        stop_event = self._get_stop_event()
        while not stop_event.is_set():
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self.poll_interval_seconds)

    async def run_once(self) -> bool:
        job = self.queue.claim_next()
        if not job:
            return False
        job_id = str(job["id"])
        try:
            message = JobQueue.message_from_payload(str(job["payload_json"]))
            result = await self.orchestrator.handle_message(message)
            self.queue.complete(job_id, result)
        except Exception as exc:  # worker must persist failure, not crash the loop
            self.queue.fail(job_id, exc)
        return True
