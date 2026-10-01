"""Regression tests for the immutable endpoint RoutePlan boundary."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from keeprollming.core.config_types import RouteSettings
from keeprollming.routing import DefaultSettings, Route, RoutePlan


def _plan(*, filters=None, upstream_url="http://upstream:8000/v1"):
    route = Route(
        name="chat/main",
        pattern="chat/main",
        model="backend-model",
        upstream_url=upstream_url,
        upstream_headers={"X-Route": "main"},
        api_key="secret",
        filters=filters,
        overrides={"temperature": 0.2},
    )
    settings = RouteSettings.from_route(route, "backend-model")
    return RoutePlan.compile(
        route=route,
        client_model="chat/main",
        model="backend-model",
        settings=settings,
        context_window=32768,
        default_max_tokens=8192,
        upstream_url=upstream_url,
    )


def test_route_plan_snapshots_route_config_and_builds_endpoint_url():
    filters = {
        "timestamp": {"enabled": True, "template": "before"},
        "model_nudge": {"enabled": False},
    }
    plan = _plan(filters=filters)
    filters["timestamp"]["template"] = "after"

    assert plan.endpoint_url == "http://upstream:8000/v1/chat/completions"
    assert plan.route_name == "chat/main"
    assert plan.request_timeout == 120.0
    assert plan.summary_model == "backend-model"
    assert plan.enabled_filters == ("timestamp",)
    assert len(plan.fallback_attempts) == 1
    assert plan.fallback_attempts[0].route is plan.route
    assert plan.fallback_attempts[0].model == "backend-model"
    assert plan.fallback_attempts[0].endpoint_url == "http://upstream:8000/v1/chat/completions"
    assert plan.build_overrides() == {"temperature": 0.2}
    assert plan.filters["timestamp"]["template"] == "before"
    with pytest.raises(TypeError):
        plan.filters["new"] = {}  # type: ignore[index]


def test_route_plan_builds_request_scoped_pipeline_and_headers():
    plan = _plan(filters={"timestamp": {"enabled": True}})

    first = plan.build_pipeline()
    second = plan.build_pipeline()

    assert first is not second
    assert first._stream_filter_config == second._stream_filter_config
    assert plan.build_upstream_headers() == {
        "X-Route": "main",
        "Authorization": "Bearer secret",
    }


def test_route_plan_normalizes_base_url_without_v1_suffix():
    plan = _plan(upstream_url="http://upstream:8000")

    assert plan.endpoint_url == "http://upstream:8000/v1/chat/completions"


def test_route_plan_resolves_named_fallback_to_its_own_transport():
    primary = Route(
        name="chat/primary", pattern="chat/primary", model="primary-model",
        upstream_url="http://primary:8000", fallback_chain=["chat/secondary"],
    )
    secondary = Route(
        name="chat/secondary", pattern="chat/secondary", model="secondary-model",
        upstream_url="http://secondary:9000/v1", upstream_headers={"X-Fallback": "yes"},
        request_timeout=42,
    )
    plan = RoutePlan.compile(
        route=primary,
        client_model="chat/primary",
        model="primary-model",
        settings=RouteSettings.from_route(primary, "primary-model"),
        context_window=32768,
        default_max_tokens=8192,
        upstream_url="http://primary:8000",
        routes_by_name={primary.name: primary, secondary.name: secondary},
        defaults=DefaultSettings(),
    )

    assert [(attempt.route_name, attempt.model, attempt.endpoint_url) for attempt in plan.fallback_attempts] == [
        ("chat/primary", "primary-model", "http://primary:8000/v1/chat/completions"),
        ("chat/secondary", "secondary-model", "http://secondary:9000/v1/chat/completions"),
    ]
    assert dict(plan.fallback_attempts[1].upstream_headers) == {"X-Fallback": "yes"}
    assert plan.fallback_attempts[1].request_timeout == 42


def test_unspecified_child_fallback_chain_inherits_parent_chain():
    from keeprollming.config import load_user_routes
    from keeprollming.routing import resolve_inherited_route

    routes = load_user_routes({"routes": {
        "parent": {"fallback_chain": ["secondary"], "model": "primary-model"},
        "child": {"extends": "parent"},
        "secondary": {"model": "secondary-model", "upstream_url": "http://secondary"},
    }})
    by_name = {route.name: route for route in routes}
    resolved = resolve_inherited_route(by_name["child"], by_name, defaults=DefaultSettings())

    assert resolved.fallback_chain == ["secondary"]


def test_non_streaming_transport_failure_uses_fallback_endpoint():
    from keeprollming.endpoints.chat_completions import process_non_streaming_request

    async def run():
        primary = Route(
            name="primary", pattern="primary", model="primary-model",
            fallback_chain=["secondary"],
        )
        secondary = Route(
            name="secondary", pattern="secondary", model="secondary-model",
            upstream_url="http://secondary:9000",
        )
        plan = RoutePlan.compile(
            route=primary, client_model="primary", model="primary-model",
            settings=RouteSettings.from_route(primary, "primary-model"),
            context_window=1024, default_max_tokens=128,
            upstream_url="http://primary:8000",
            routes_by_name={primary.name: primary, secondary.name: secondary},
            defaults=DefaultSettings(),
        )

        calls = []

        class Client:
            async def post(self, target_url, **_kwargs):
                calls.append(target_url)
                if "primary" in target_url:
                    raise httpx.ConnectError("primary refused")
                return httpx.Response(200, json={
                    "model": "secondary-model",
                    "choices": [{"message": {"content": "fallback ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

        with patch("keeprollming.endpoints.chat_completions._exec.emit_fallback") as emit_fallback:
            response = await process_non_streaming_request(
                url="http://primary:8000/v1/chat/completions", client=Client(),
                payload={"model": "primary-model", "messages": []}, route_headers={},
                req_id="fallback-non-stream", upstream_model="primary-model",
                fallback_attempts=plan.fallback_attempts,
                t_start=0.0, route_name="primary", route=primary, request_timeout=10,
            )
        assert calls == [
            "http://primary:8000/v1/chat/completions",
            "http://secondary:9000/v1/chat/completions",
        ]
        assert response.status_code == 200
        assert b"fallback ok" in response.body
        assert emit_fallback.call_count == 1
        _req_id, source, target = emit_fallback.call_args.args
        assert source["route"] == "primary"
        assert source["model"] == "primary-model"
        assert target.route_name == "secondary"
        assert emit_fallback.call_args.kwargs == {
            "reason": "transport_error",
            "attempt": 1,
            "total_attempts": 2,
            "error_type": "ConnectError",
            "error": "primary refused",
        }

    asyncio.run(run())


def test_non_streaming_client_error_is_not_retried():
    from keeprollming.endpoints.chat_completions import process_non_streaming_request

    async def run():
        primary = Route(
            name="primary", pattern="primary", model="primary-model",
            fallback_chain=["secondary"],
        )
        secondary = Route(
            name="secondary", pattern="secondary", model="secondary-model",
            upstream_url="http://secondary:9000",
        )
        plan = RoutePlan.compile(
            route=primary, client_model="primary", model="primary-model",
            settings=RouteSettings.from_route(primary, "primary-model"),
            context_window=1024, default_max_tokens=128,
            upstream_url="http://primary:8000",
            routes_by_name={primary.name: primary, secondary.name: secondary},
            defaults=DefaultSettings(),
        )
        calls = []
        error = {
            "error": {
                "message": "maximum context length exceeded",
                "type": "BadRequestError",
                "param": "input_tokens",
                "code": 400,
            }
        }

        class Client:
            async def post(self, target_url, **_kwargs):
                calls.append(target_url)
                return httpx.Response(400, json=error)

        with patch("keeprollming.endpoints.chat_completions._exec.emit_fallback") as emit_fallback:
            response = await process_non_streaming_request(
                url="http://primary:8000/v1/chat/completions", client=Client(),
                payload={"model": "primary-model", "messages": []}, route_headers={},
                req_id="client-error-non-stream", upstream_model="primary-model",
                fallback_attempts=plan.fallback_attempts,
                t_start=0.0, route_name="primary", route=primary, request_timeout=10,
            )

        assert calls == ["http://primary:8000/v1/chat/completions"]
        assert response.status_code == 400
        assert json.loads(response.body) == error
        assert emit_fallback.call_count == 0

    asyncio.run(run())
