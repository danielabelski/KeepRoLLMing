"""Deterministic contracts for per-route admission control."""

import asyncio

import pytest

from keeprollming.admission import RouteAdmissionController
from keeprollming.observability import EventDispatcher


@pytest.mark.asyncio
async def test_rejects_when_route_is_saturated_without_queueing() -> None:
    controller = RouteAdmissionController()
    dispatcher = EventDispatcher()
    events = []
    dispatcher.subscribe("request.lifecycle.admission", events.append)

    first = await controller.acquire(
        req_id="first", route_name="chat/limited", max_concurrent=1,
        queue_timeout=0, dispatcher=dispatcher,
    )
    assert first is not None
    rejected = await controller.acquire(
        req_id="second", route_name="chat/limited", max_concurrent=1,
        queue_timeout=0, dispatcher=dispatcher,
    )

    assert rejected is None
    assert [event.type for event in events] == [
        "request.lifecycle.admission.acquired",
        "request.lifecycle.admission.queued",
        "request.lifecycle.admission.rejected",
    ]
    await first.release()


@pytest.mark.asyncio
async def test_bounded_queue_acquires_after_a_slot_is_released() -> None:
    controller = RouteAdmissionController()
    first = await controller.acquire(
        req_id="first", route_name="chat/limited", max_concurrent=1,
        queue_timeout=0,
    )
    assert first is not None

    waiting = asyncio.create_task(controller.acquire(
        req_id="second", route_name="chat/limited", max_concurrent=1,
        queue_timeout=0.5,
    ))
    await asyncio.sleep(0)
    assert not waiting.done()
    await first.release()
    second = await waiting
    assert second is not None
    await second.release()


@pytest.mark.asyncio
async def test_unlimited_route_does_not_create_a_gate() -> None:
    controller = RouteAdmissionController()
    assert await controller.acquire(
        req_id="unlimited", route_name="chat/unlimited", max_concurrent=None,
        queue_timeout=None,
    ) is None
