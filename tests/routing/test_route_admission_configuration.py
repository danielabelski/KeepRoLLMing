"""Validation and inheritance contracts for route admission settings."""

import pytest

from keeprollming.config import validate_resolved_route_admission
from keeprollming.routing.router import resolve_inherited_route
from keeprollming.types import Route


def test_admission_settings_inherit_from_parent_route() -> None:
    base = Route(name="base", pattern="base", max_concurrent=2, queue_timeout=1.5)
    child = Route(name="chat/child", pattern="chat/child", extends="base")

    resolved = resolve_inherited_route(child, {"base": base, "chat/child": child})

    assert resolved.max_concurrent == 2
    assert resolved.queue_timeout == 1.5


@pytest.mark.parametrize("field, value", [
    ("max_concurrent", 0),
    ("max_concurrent", True),
    ("queue_timeout", -1),
    ("queue_timeout", "not-a-number"),
])
def test_invalid_admission_settings_fail_config_validation(field, value) -> None:
    route = Route(name="bad", pattern="bad", **{field: value})

    with pytest.raises(ValueError, match=field):
        validate_resolved_route_admission([route])
