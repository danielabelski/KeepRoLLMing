"""Per-route, event-loop-local admission control.

The shared HTTP pool limits sockets, not the work allowed to reach a backend.
This module provides the earlier boundary: a route can cap concurrent logical
requests and either reject excess work immediately or wait for a bounded time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .observability import events_request


@dataclass
class AdmissionLease:
    """One acquired route slot; releasing it is idempotent."""

    route_name: str
    semaphore: asyncio.Semaphore
    dispatcher: Any = None
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.semaphore.release()
        events_request.emit_admission_released(
            self.route_name, dispatcher=self.dispatcher
        )


class RouteAdmissionController:
    """Own semaphores per event loop and resolved route name.

    KRM's tests create several independent event loops in one interpreter;
    binding an ``asyncio.Semaphore`` to a process-global route would make those
    tests (and embedder use) unsafe. The loop identity is therefore part of the
    key while each real server process still has exactly one gate per route.
    """

    def __init__(self) -> None:
        self._semaphores: dict[tuple[int, str, int], asyncio.Semaphore] = {}

    def _semaphore(self, route_name: str, max_concurrent: int) -> asyncio.Semaphore:
        key = (id(asyncio.get_running_loop()), route_name, max_concurrent)
        semaphore = self._semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max_concurrent)
            self._semaphores[key] = semaphore
        return semaphore

    async def acquire(
        self,
        *,
        req_id: str,
        route_name: str,
        max_concurrent: int | None,
        queue_timeout: float | None,
        dispatcher: Any = None,
    ) -> AdmissionLease | None:
        """Acquire one route slot, or return ``None`` after bounded rejection."""
        if max_concurrent is None:
            return None
        semaphore = self._semaphore(route_name, max_concurrent)
        timeout = max(0.0, float(queue_timeout or 0.0))
        queued = semaphore.locked()
        if queued:
            events_request.emit_admission_queued(
                req_id,
                route=route_name,
                max_concurrent=max_concurrent,
                queue_timeout_ms=round(timeout * 1000.0),
                dispatcher=dispatcher,
            )
        try:
            if timeout == 0.0:
                if semaphore.locked():
                    raise TimeoutError
                await semaphore.acquire()
            else:
                await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
        except TimeoutError:
            events_request.emit_admission_rejected(
                req_id,
                route=route_name,
                max_concurrent=max_concurrent,
                queue_timeout_ms=round(timeout * 1000.0),
                dispatcher=dispatcher,
            )
            return None
        events_request.emit_admission_acquired(
            req_id,
            route=route_name,
            max_concurrent=max_concurrent,
            waited=queued,
            dispatcher=dispatcher,
        )
        return AdmissionLease(route_name, semaphore, dispatcher)


_controller = RouteAdmissionController()


def get_route_admission_controller() -> RouteAdmissionController:
    """Return the process-local controller used by the HTTP endpoints."""
    return _controller
