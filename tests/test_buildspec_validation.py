"""BuildSpec validation tests."""

from __future__ import annotations

from aware_models.buildspec import validate_buildspec


def _payload() -> dict[str, object]:
    return {
        "task_type": "task_7",
        "date": "2021-03-04",
        "filename_date": "2021_03_04",
        "failure_time_range": {"start": "18:30:00", "end": "19:00:00"},
        "failure_time_range_ts": {"start": 1614853800, "end": 1614855600},
        "failures_detected": 1,
        "uncertainty": {
            "root_cause_time": "unknown",
            "root_cause_component": "unknown",
            "root_cause_reason": "unknown",
        },
        "objective": "Identify the root cause component, the exact root cause datetime and reason for the failure",
        "filename_date_directory": "/agentfactory/data/Bank/telemetry/2021_03_04",
        "absolute_log_file": ["/agentfactory/data/Bank/telemetry/2021_03_04/log/log_service.csv"],
        "absolute_trace_file": ["/agentfactory/data/Bank/telemetry/2021_03_04/trace/trace_span.csv"],
        "absolute_metrics_file": ["/agentfactory/data/Bank/telemetry/2021_03_04/metric/metric_container.csv"],
    }


def test_validate_buildspec_valid_payload() -> None:
    result = validate_buildspec(_payload(), expected_repository_path="/agentfactory/data/Bank/telemetry")
    assert result.is_valid is True
    assert result.normalized is not None


def test_validate_buildspec_task_type_is_allowed_value() -> None:
    payload = _payload()
    payload["task_type"] = "task_99"
    result = validate_buildspec(payload)
    assert result.is_valid is False
    assert any("task_type" in item for item in result.errors)


def test_validate_buildspec_accepts_multiple_absolute_files_per_domain() -> None:
    payload = _payload()
    payload["absolute_log_file"] = [
        "/agentfactory/data/Bank/telemetry/2021_03_04/log/log_service.csv",
        "/agentfactory/data/Bank/telemetry/2021_03_04/log/log_gateway.csv",
    ]
    payload["absolute_trace_file"] = [
        "/agentfactory/data/Bank/telemetry/2021_03_04/trace/trace_span.csv",
        "/agentfactory/data/Bank/telemetry/2021_03_04/trace/trace_service.csv",
    ]
    payload["absolute_metrics_file"] = [
        "/agentfactory/data/Bank/telemetry/2021_03_04/metric/metric_container.csv",
        "/agentfactory/data/Bank/telemetry/2021_03_04/metric/metric_service.csv",
    ]
    result = validate_buildspec(payload, expected_repository_path="/agentfactory/data/Bank/telemetry")
    assert result.is_valid is True
    assert result.normalized is not None


def test_validate_buildspec_accepts_an_explicitly_unavailable_signal_domain() -> None:
    payload = _payload()
    payload["absolute_log_file"] = []

    result = validate_buildspec(payload)

    assert result.is_valid is True
    assert result.normalized is not None
    assert result.normalized.absolute_log_file == []
