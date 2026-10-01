"""Deterministic state-machine tests for route circuit breakers."""

from keeprollming.circuit_breaker import CircuitBreakerRegistry


def test_breaker_opens_then_recovers_through_one_half_open_probe() -> None:
    now = [0.0]
    breaker = CircuitBreakerRegistry(clock=lambda: now[0])

    first = breaker.allow(
        route_name="chat/main", enabled=True, recovery_timeout=10,
        req_id="one",
    )
    assert first is not None
    breaker.record_failure(first, req_id="one", failure_threshold=2)
    assert breaker.snapshot("chat/main") == {
        "state": "closed", "consecutive_failures": 1,
    }

    second = breaker.allow(
        route_name="chat/main", enabled=True, recovery_timeout=10,
        req_id="two",
    )
    assert second is not None
    breaker.record_failure(second, req_id="two", failure_threshold=2)
    assert breaker.snapshot("chat/main")["state"] == "open"
    assert breaker.allow(
        route_name="chat/main", enabled=True, recovery_timeout=10,
        req_id="blocked",
    ) is None

    now[0] = 10.0
    probe = breaker.allow(
        route_name="chat/main", enabled=True, recovery_timeout=10,
        req_id="probe",
    )
    assert probe is not None
    assert breaker.allow(
        route_name="chat/main", enabled=True, recovery_timeout=10,
        req_id="second-probe",
    ) is None
    breaker.record_success(probe, req_id="probe")
    assert breaker.snapshot("chat/main") == {
        "state": "closed", "consecutive_failures": 0,
    }


def test_disabled_breaker_never_retains_failure_state() -> None:
    breaker = CircuitBreakerRegistry()
    attempt = breaker.allow(
        route_name="chat/main", enabled=False, recovery_timeout=1, req_id="r",
    )
    assert attempt is not None
    breaker.record_failure(attempt, req_id="r", failure_threshold=1)
    assert breaker.snapshot("chat/main") == {
        "state": "closed", "consecutive_failures": 0,
    }
