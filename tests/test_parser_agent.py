"""ParserAgent workflow tests."""

from __future__ import annotations

import pytest
from google.adk.agents import BaseAgent

from agents import parser_agent as parser_module
from llm.mock import MockLLMClient
from aware_models.buildspec import validate_buildspec
from runtime.agent_factory import create_parser_agent
from runtime import agent_factory as agent_factory_module


@pytest.fixture(autouse=True)
def _reset_parser_singleton_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWARE_ENABLE_REASONING", "true")
    monkeypatch.setenv("AWARE_ENABLE_MEMORY", "true")
    agent_factory_module._PARSER_AGENT = None


def test_parser_agent_is_google_adk_agent() -> None:
    agent = create_parser_agent(llm_client=MockLLMClient(), max_attempts=3)
    assert isinstance(agent, BaseAgent)


def test_parser_agent_retries_until_valid_buildspec() -> None:
    llm = MockLLMClient(invalid_attempts=2)
    agent = create_parser_agent(llm_client=llm, max_attempts=5)

    result = agent.generate_buildspec(
        user_query="On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout",
        repository_path="/agentfactory/data/Bank/telemetry",
    )

    assert result.attempts == 3
    assert len(result.errors_by_attempt) == 2
    assert result.buildspec.filename_date == "2021_03_04"
    assert result.buildspec.task_type == "task_7"


def test_parser_agent_emits_conversation_events() -> None:
    llm = MockLLMClient(invalid_attempts=1)
    agent = create_parser_agent(llm_client=llm, max_attempts=4)
    events: list[tuple[str, str]] = []

    def on_event(event: object) -> None:
        phase = getattr(event, "phase", "")
        content = getattr(event, "content", "")
        events.append((phase, content))

    result = agent.generate_buildspec(
        user_query="On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout",
        repository_path="/agentfactory/data/Bank/telemetry",
        on_event=on_event,
    )

    assert result.attempts == 2
    phases = [item[0] for item in events]
    assert "init" in phases
    assert "load_knowledge" in phases
    assert "thinking" in phases
    assert "validation" in phases
    assert "success" in phases


def test_normalization_does_not_force_semantic_decisions(tmp_path) -> None:
    payload = {
        "task_type": "task_7",
        "date": "2021-03-04",
        "filename_date": "20210304",
        "failure_time_range": {"start": "2021-03-04T18:30:00Z", "end": "2021-03-04T19:00:00Z"},
        "failure_time_range_ts": {"start": "1614882600", "end": "1614884400"},
        "failures_detected": "1 failure detected in auth-service",
        "uncertainty": "inferred from sparse traces",
        "objective": "find root cause",
        "filename_date_directory": "/repo/.buildspecs/2021_03_04",
        "absolute_log_file": ["/repo/logs/service.log"],
        "absolute_trace_file": ["/repo/traces/trace.json"],
        "absolute_metrics_file": ["/repo/metrics/metrics.json"],
    }
    normalized = parser_module._normalize_candidate_payload(
        payload=payload,
        repository_path=tmp_path.resolve(),
        user_query="On 2021-03-04 between 18:30 and 19:00 one failure occurred.",
    )
    validation = validate_buildspec(
        payload=normalized,
        expected_repository_path=str(tmp_path.resolve()),
    )
    assert validation.is_valid is False


def test_normalization_recomputes_time_range_ts_with_utc_plus_8_tool(tmp_path) -> None:
    payload = {
        "task_type": "task_7",
        "date": "2021-03-04",
        "filename_date": "2021_03_04",
        "failure_time_range": {"start": "18:30:00", "end": "19:00:00"},
        "failure_time_range_ts": {"start": 66600, "end": 68400},
        "failures_detected": 1,
        "uncertainty": {
            "root_cause_time": "unknown",
            "root_cause_component": "unknown",
            "root_cause_reason": "unknown",
        },
        "objective": "Identify the root cause component, the exact root cause datetime and reason for the failure",
        "filename_date_directory": str((tmp_path / "2021_03_04").resolve()),
        "absolute_log_file": [str((tmp_path / "2021_03_04" / "log.csv").resolve())],
        "absolute_trace_file": [str((tmp_path / "2021_03_04" / "trace.csv").resolve())],
        "absolute_metrics_file": [str((tmp_path / "2021_03_04" / "metric.csv").resolve())],
    }
    normalized = parser_module._normalize_candidate_payload(
        payload=payload,
        repository_path=tmp_path.resolve(),
        user_query="On 2021-03-04 between 18:30 and 19:00 one failure occurred.",
    )
    assert normalized["failure_time_range_ts"]["start"] == 1614853800
    assert normalized["failure_time_range_ts"]["end"] == 1614855600


def test_nezha_normalization_uses_utc_and_selects_window_shards(tmp_path) -> None:
    dated = tmp_path / "snapshot" / "rca_data" / "2023-01-30"
    relative_files: list[str] = []
    for domain, names in {
        "log": ["11_49_log.csv", "11_50_log.csv", "11_51_log.csv", "11_52_log.csv", "11_53_log.csv", "11_54_log.csv"],
        "trace": ["11_50_trace.csv", "11_51_trace.csv", "11_52_trace.csv", "11_53_trace.csv"],
        "metric": ["container_cpu_usage_seconds_total.csv", "node_network_receive_bytes_total.csv"],
    }.items():
        directory = dated / domain
        directory.mkdir(parents=True)
        for name in names:
            path = directory / name
            path.write_text("header\n", encoding="utf-8")
            relative_files.append(path.relative_to(tmp_path).as_posix())

    payload = {
        "task_type": "task_7",
        "date": "2023-01-30",
        "filename_date": "2023-01-30",
        "failure_time_range": {"start": "11:50:30", "end": "11:53:30"},
        "failures_detected": 1,
        "uncertainty": {
            "root_cause_time": "unknown",
            "root_cause_component": "unknown",
            "root_cause_reason": "unknown",
        },
        "objective": "Identify the component, occurrence time, and reason",
    }
    normalized = parser_module._normalize_candidate_payload(
        payload=payload,
        repository_path=tmp_path.resolve(),
        repository_files=relative_files,
        user_query="On January 30, 2023 between 11:50:30 and 11:53:30 UTC",
        timezone_offset_minutes=0,
        dataset_profile="nezha",
    )

    assert normalized["failure_time_range_ts"] == {"start": 1675079430, "end": 1675079610}
    assert [path.rsplit("/", 1)[-1] for path in normalized["absolute_log_file"]] == [
        "11_50_log.csv",
        "11_51_log.csv",
        "11_52_log.csv",
        "11_53_log.csv",
    ]
    assert len(normalized["absolute_trace_file"]) == 4
    assert len(normalized["absolute_metrics_file"]) == 2
    assert normalized["filename_date_directory"].endswith("rca_data/2023-01-30")


def test_canonical_nezha_incident_skips_parser_llm_and_bounds_file_events(tmp_path) -> None:
    dated = tmp_path / "snapshot" / "rca_data" / "2023-01-30"
    for domain, names in {
        "log": ["11_50_log.csv", "11_51_log.csv", "11_52_log.csv"],
        "trace": ["11_50_trace.csv", "11_51_trace.csv", "11_52_trace.csv"],
        "metric": [f"service-{index}_metric.csv" for index in range(30)],
    }.items():
        directory = dated / domain
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_text("timestamp,value\n", encoding="utf-8")
    llm = MockLLMClient()
    events = []
    agent = parser_module.ParserAgent(llm_client=llm)

    result = agent.generate_buildspec(
        user_query=(
            "On 2023-01-30, between 11:50:00 and 11:52:59 UTC, one failure occurred. "
            "Identify the root cause component, occurrence time, and reason."
        ),
        repository_path=str(tmp_path),
        timezone_offset_minutes=0,
        dataset_profile="nezha",
        on_event=events.append,
    )

    assert result.attempts == 0
    assert llm.call_count == 0
    assert result.buildspec.failure_time_range_ts.start == 1_675_079_400
    assert len([event for event in events if event.phase == "discover_file"]) == 20
    assert any(event.phase == "discover_file_summary" for event in events)
    assert any(event.phase == "deterministic_buildspec" for event in events)


def test_openrca_telecom_selects_date_files_and_accepts_missing_logs(tmp_path) -> None:
    dated = tmp_path / "telemetry" / "2020_04_11"
    relative_files = []
    for domain, names in {
        "trace": ["trace_span.csv"],
        "metric": ["metric_node.csv", "metric_service.csv"],
    }.items():
        directory = dated / domain
        directory.mkdir(parents=True)
        for name in names:
            path = directory / name
            path.write_text("timestamp,value\n1586534700,1\n", encoding="utf-8")
            relative_files.append(path.relative_to(tmp_path).as_posix())
    payload = {
        "task_type": "task_7",
        "date": "2020-04-11",
        "filename_date": "2020_04_11",
        "failure_time_range": {"start": "00:05:00", "end": "00:05:59"},
        "failures_detected": 1,
        "uncertainty": {
            "root_cause_time": "unknown",
            "root_cause_component": "unknown",
            "root_cause_reason": "unknown",
        },
        "objective": "Identify the component, occurrence time, and reason",
        "absolute_log_file": [str((dated / "invented.log").resolve())],
    }

    normalized = parser_module._normalize_candidate_payload(
        payload=payload,
        repository_path=tmp_path.resolve(),
        repository_files=relative_files,
        user_query="On April 11, 2020 between 00:05:00 and 00:05:59 UTC+08:00",
        timezone_offset_minutes=480,
        dataset_profile="openrca_telecom",
    )

    assert normalized["failure_time_range_ts"] == {
        "start": 1586534700,
        "end": 1586534759,
    }
    assert normalized["absolute_log_file"] == []
    assert len(normalized["absolute_trace_file"]) == 1
    assert len(normalized["absolute_metrics_file"]) == 2
    assert normalized["filename_date_directory"].endswith("telemetry/2020_04_11")


def test_normalization_keeps_llm_task_type(tmp_path) -> None:
    payload = {
        "task_type": "task_7",
        "date": "2021-03-09",
        "filename_date": "2021_03_09",
        "failure_time_range": {"start": "09:00:00", "end": "09:30:00"},
        "failure_time_range_ts": {"start": 1615280400, "end": 1615282200},
        "failures_detected": 1,
        "uncertainty": {
            "root_cause_time": "unknown",
            "root_cause_component": "known",
            "root_cause_reason": "known",
        },
        "objective": "Pinpoint the root cause occurrence datetime",
        "filename_date_directory": str((tmp_path / "2021_03_09").resolve()),
        "absolute_log_file": [str((tmp_path / "2021_03_09" / "log.csv").resolve())],
        "absolute_trace_file": [str((tmp_path / "2021_03_09" / "trace.csv").resolve())],
        "absolute_metrics_file": [str((tmp_path / "2021_03_09" / "metric.csv").resolve())],
    }
    normalized = parser_module._normalize_candidate_payload(
        payload=payload,
        repository_path=tmp_path.resolve(),
        user_query="Component and reason are known; find the exact occurrence time.",
    )
    assert normalized["task_type"] == "task_7"


def test_task_type_validation_matches_query_intent() -> None:
    errors = parser_module._validate_task_type_against_query(
        payload={"task_type": "task_7"},
        user_query="Determine root cause time only.",
    )
    assert errors
    assert "expected `task_1`" in errors[0]

    ok = parser_module._validate_task_type_against_query(
        payload={"task_type": "task_1"},
        user_query="Determine root cause time only.",
    )
    assert ok == []


def test_parser_uses_llm_even_when_reasoning_disabled() -> None:
    llm = MockLLMClient(invalid_attempts=1)
    agent = parser_module.ParserAgent(
        llm_client=llm,
        max_attempts=4,
        enable_reasoning=False,
    )
    result = agent.generate_buildspec(
        user_query="On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout",
        repository_path="/agentfactory/data/Bank/telemetry",
    )
    assert llm.call_count >= 2
    assert result.attempts == 2
    assert result.raw_responses


def test_parser_self_configures_llm_from_env_when_missing(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    created: dict[str, object] = {}

    def _factory(**kwargs):
        created.update(kwargs)
        return MockLLMClient()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    monkeypatch.setattr(parser_module, "create_llm_client", _factory)

    agent = parser_module.ParserAgent(
        llm_client=None,
        max_attempts=3,
    )
    result = agent.generate_buildspec(
        user_query="On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout",
        repository_path=str(tmp_path),
    )

    assert result.attempts >= 1
    assert agent.llm_client is not None
    assert created.get("provider") == "openai-compatible"
    assert created.get("openai_model") == "gpt-5-mini"
