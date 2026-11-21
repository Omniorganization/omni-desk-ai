"""Run blocking AppSync store calls off the asyncio event loop.

Store methods do synchronous database and file I/O and may wait for row locks.
Awaiting them through :func:`run_blocking` keeps every other request, WebSocket
and SSE stream on the process responsive while one call waits. The pool is
bounded; callers beyond it queue here instead of on the loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def _worker_count() -> int:
    raw = os.getenv("OMNIDESK_APPSYNC_DB_THREADS", "").strip()
    try:
        value = int(raw) if raw else 32
    except ValueError as exc:
        raise RuntimeError("OMNIDESK_APPSYNC_DB_THREADS must be an integer") from exc
    return max(2, min(value, 256))


_EXECUTOR = ThreadPoolExecutor(max_workers=_worker_count(), thread_name_prefix="omnidesk-appsync-db")


async def run_blocking(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Await ``function(*args, **kwargs)`` on the AppSync worker pool.

    Context variables (request ids, active chat reservations) are carried into
    the worker, as ``asyncio.to_thread`` does.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    return await loop.run_in_executor(_EXECUTOR, functools.partial(context.run, function, *args, **kwargs))
