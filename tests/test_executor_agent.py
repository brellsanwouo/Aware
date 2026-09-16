"""ExecutorAgent workflow tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.adk.agents import BaseAgent

from agents import executor_agent as executor_module
from agents.executor_agent import ExecutorAgent
from aware_models.executor import AssessFinding
from aware_models.buildspec import BuildSpec
from runtime.agent_factory import create_executor_agent
from runtime import agent_factory as agent_factory_module


class _DecisionStubLLM:
    """Deterministic LLM test double for analyzer decisions."""

    provider_name = "test-decision"

    def complete(self, system_prompt: str, user_prompt: str, response_format: dict | None = None) -> str:
        return '{"summary":"stub","findings":[]}'


@pytest.fixture(autouse=True)
def _reset_executor_singleton_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWARE_ENABLE_REASONING", "true")
    monkeypatch.setenv("AWARE_ENABLE_MEMORY", "true")
    monkeypatch.delenv("AWARE_RCA_VERSION", raising=False)
    agent_factory_module._EXECUTOR_AGENT = None


def _buildspec_for_tmp(tmp_path) -> BuildSpec:
    date_dir = tmp_path / "2021_03_04"
    log_path = date_dir / "log" / "log_service.csv"
    trace_path = date_dir / "trace" / "trace_span.csv"
    metrics_path = date_dir / "metric" / "metric_container.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("timeout error retry timeout\n", encoding="utf-8")
    trace_path.write_text("checkout -> payment timeout 950ms\n", encoding="utf-8")
    metrics_path.write_text("cpu=93 latency=480 error_rate=7\n", encoding="utf-8")

    return BuildSpec.model_validate(
        {
            "task_type": "task_7",
            "date": "2021-03-04",
            "filename_date": "2021_03_04",
            "failure_time_range": {"start": "18:30:00", "end": "19:00:00"},
            "failure_time_range_ts": {"start": 1614879000, "end": 1614880800},
            "failures_detected": 1,
            "uncertainty": {
                "root_cause_time": "unknown",
                "root_cause_component": "unknown",
                "root_cause_reason": "unknown",
            },
            "objective": "Identify the root cause component, the exact root cause datetime and reason for the failure",
            "filename_date_directory": str(date_dir.resolve()),
            "absolute_log_file": [str(log_path.resolve())],
            "absolute_trace_file": [str(trace_path.resolve())],
            "absolute_metrics_file": [str(metrics_path.resolve())],
        }
    )


def test_executor_agent_is_google_adk_agent() -> None:
    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]
    assert isinstance(agent, BaseAgent)
    assert isinstance(agent, ExecutorAgent)


def test_executor_agent_runs_specialized_agents_and_emits_events(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]
    events: list[tuple[str, str]] = []

    def on_event(event: object) -> None:
        phase = getattr(event, "phase", "")
        sender = getattr(event, "sender", "")
        events.append((sender, phase))

    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
        on_event=on_event,
    )

    assert "LogsAgent" in result.agents_instantiated
    assert "TraceAgent" in result.agents_instantiated
    assert "MetricsAgent" in result.agents_instantiated
    assert any(item.kind == "anomaly" for item in result.findings)
    phases = [phase for _, phase in events]
    assert "instantiate_agent" in phases
    assert "dispatch" in phases
    assert "summary" in phases


def test_executor_skips_an_unavailable_telemetry_domain(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path).model_copy(
        update={"absolute_log_file": []}
    )
    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
    )

    assert "LogsAgent" not in result.agents_instantiated
    assert "TraceAgent" in result.agents_instantiated
    assert "MetricsAgent" in result.agents_instantiated


def test_executor_filters_rows_strictly_by_failure_time_range(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    log_path = Path(buildspec.absolute_log_file[0])
    trace_path = Path(buildspec.absolute_trace_file[0])
    metric_path = Path(buildspec.absolute_metrics_file[0])

    log_path.write_text(
        (
            "log_id,timestamp,cmdb_id,log_name,value\n"
            "a,1614878000,svc,gc,timeout error outside\n"
            "b,1614879500,svc,gc,timeout error inside\n"
        ),
        encoding="utf-8",
    )
    trace_path.write_text(
        (
            "timestamp,cmdb_id,parent_id,span_id,trace_id,duration\n"
            "1614879500000,dockerA2,p,s,t,900\n"
        ),
        encoding="utf-8",
    )
    metric_path.write_text(
        (
            "timestamp,rr,sr,cnt,mrt,tc\n"
            "1614879500,100.0,92.0,42,650.0,ServiceTest1\n"
        ),
        encoding="utf-8",
    )

    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]
    result = agent.execute_assess(buildspec=buildspec, repository_path=str(tmp_path))
    by_name = {item.agent_name: item for item in result.task_results}
    logs_result = by_name["LogsAgent"]
    assert "rows_in_window=1" in logs_result.detail
    assert "total_rows=2" in logs_result.detail
    assert any("Detected 1 timeout indicators in logs." in finding.summary for finding in logs_result.findings)


def test_executor_groups_multiple_files_into_one_domain_agent(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    date_dir = Path(buildspec.filename_date_directory)
    extra_log = date_dir / "log" / "log_service_extra.csv"
    extra_log.write_text(
        (
            "log_id,timestamp,cmdb_id,log_name,value\n"
            "x,1614879500,svc,app,timeout in extra file\n"
        ),
        encoding="utf-8",
    )
    buildspec = buildspec.model_copy(
        update={"absolute_log_file": [*buildspec.absolute_log_file, str(extra_log.resolve())]}
    )

    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]
    result = agent.execute_assess(buildspec=buildspec, repository_path=str(tmp_path))
    log_agents = [name for name in result.agents_instantiated if name.startswith("LogsAgent")]
    assert len(log_agents) == 1
    assert any("need_sources=2" in item.detail for item in result.task_results)


def test_executor_respects_max_agents_cap(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    date_dir = Path(buildspec.filename_date_directory)
    extra_log = date_dir / "log" / "log_service_extra.csv"
    extra_metric = date_dir / "metric" / "metric_extra.csv"
    extra_log.write_text(
        (
            "log_id,timestamp,cmdb_id,log_name,value\n"
            "x,1614879500,svc,app,timeout in extra file\n"
        ),
        encoding="utf-8",
    )
    extra_metric.write_text(
        (
            "timestamp,cpu,latency,error_rate\n"
            "1614879500,90,420,9\n"
        ),
        encoding="utf-8",
    )
    buildspec = buildspec.model_copy(
        update={
            "absolute_log_file": [*buildspec.absolute_log_file, str(extra_log.resolve())],
            "absolute_metrics_file": [*buildspec.absolute_metrics_file, str(extra_metric.resolve())],
        }
    )

    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]
    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
        max_agents=2,
    )
    assert len(result.agents_instantiated) == 2


def test_executor_without_budget_covers_every_discovered_target(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    metric_dir = Path(buildspec.filename_date_directory) / "metric"
    expected_metric_paths = {Path(buildspec.absolute_metrics_file[0]).resolve()}
    for index in range(12):
        path = metric_dir / f"service-{index:02d}_metric.csv"
        path.write_text(
            "timestamp,cpu,latency,error_rate\n1614879500,10,20,0\n",
            encoding="utf-8",
        )
        expected_metric_paths.add(path.resolve())
    buildspec = buildspec.model_copy(
        update={"absolute_metrics_file": [str(path) for path in sorted(expected_metric_paths)]}
    )
    events: list[str] = []
    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
        max_agents=None,
        on_event=lambda event: events.append(event.phase),
    )

    analyzed_paths: set[Path] = set()
    for item in result.task_results:
        try:
            values = json.loads(item.target_path)
        except json.JSONDecodeError:
            values = [item.target_path]
        if not isinstance(values, list):
            values = [item.target_path]
        analyzed_paths.update(Path(value).resolve() for value in values)
    assert expected_metric_paths <= analyzed_paths
    assert result.agents_instantiated == [
        "MetricsAgent",
        "LogsAgent",
        "TraceAgent",
        "JVMExpert",
        "MySQLExpert",
        "RedisExpert",
    ]
    assert "cap_reached" not in events
    assert "converged" in events
    assert events.count("terminate_agent") == len(result.agents_instantiated)


def test_executor_skips_expansion_for_cross_domain_component_support(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    log_path = Path(buildspec.absolute_log_file[0])
    trace_path = Path(buildspec.absolute_trace_file[0])
    metric_path = Path(buildspec.absolute_metrics_file[0])

    log_path.write_text(
        (
            "log_id,timestamp,cmdb_id,log_name,value\n"
            "a,1614879500,Tomcat02,app,timeout error\n"
        ),
        encoding="utf-8",
    )
    trace_path.write_text(
        (
            "timestamp,cmdb_id,parent_id,span_id,trace_id,duration\n"
            "1614879500000,Tomcat02,p,s,t,900\n"
        ),
        encoding="utf-8",
    )
    metric_path.write_text(
        (
            "timestamp,cmdb_id,kpi_name,value\n"
            "1614879500,Tomcat02,cpu,95\n"
        ),
        encoding="utf-8",
    )

    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]
    events: list[str] = []
    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
        max_agents=10,
        on_event=lambda event: events.append(event.phase),
    )
    assert result.agents_instantiated == [
        "MetricsAgent",
        "LogsAgent",
        "TraceAgent",
        "JVMExpert",
        "MySQLExpert",
        "RedisExpert",
    ]
    assert "expansion_skipped" in events


def test_executor_limits_weak_component_followups_to_two(tmp_path) -> None:
    buildspec = _buildspec_for_tmp(tmp_path)
    Path(buildspec.absolute_log_file[0]).write_text(
        "log_id,timestamp,cmdb_id,log_name,value\n"
        "a,1614879500,Tomcat02,app,timeout error\n",
        encoding="utf-8",
    )
    Path(buildspec.absolute_trace_file[0]).write_text(
        "timestamp,duration\n1614879500000,900\n",
        encoding="utf-8",
    )
    Path(buildspec.absolute_metrics_file[0]).write_text(
        "timestamp,value\n1614879500,95\n",
        encoding="utf-8",
    )
    events: list[str] = []
    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
        on_event=lambda event: events.append(event.phase),
    )

    assert len(result.agents_instantiated) == 8
    assert result.agents_instantiated[:6] == [
        "MetricsAgent",
        "LogsAgent",
        "TraceAgent",
        "JVMExpert",
        "MySQLExpert",
        "RedisExpert",
    ]
    assert events.count("expand_enqueue") == 1
    assert sum("component_focus=Tomcat02" in item.detail for item in result.task_results) == 2


def test_executor_expansion_budget_can_be_reduced_to_one(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXECUTOR_MAX_EXPANSIONS", "1")
    buildspec = _buildspec_for_tmp(tmp_path)
    Path(buildspec.absolute_log_file[0]).write_text(
        "log_id,timestamp,cmdb_id,log_name,value\n"
        "a,1614879500,Tomcat02,app,timeout error\n",
        encoding="utf-8",
    )
    Path(buildspec.absolute_trace_file[0]).write_text(
        "timestamp,duration\n1614879500000,900\n",
        encoding="utf-8",
    )
    Path(buildspec.absolute_metrics_file[0]).write_text(
        "timestamp,value\n1614879500,95\n",
        encoding="utf-8",
    )
    agent = create_executor_agent(llm_client=_DecisionStubLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(buildspec=buildspec, repository_path=str(tmp_path))

    assert len(result.agents_instantiated) == 7


def test_executor_honors_requested_followups_for_a_named_component(tmp_path) -> None:
    class _WeakCheckpointLLM:
        provider_name = "test"

        def complete(self, system_prompt: str, user_prompt: str, response_format=None) -> str:
            schema_name = (response_format or {}).get("json_schema", {}).get("name")
            if schema_name == "rca_coordinator_checkpoint":
                return (
                    '{"root_cause_component":"Tomcat02",'
                    '"root_cause_reason":"possible latency",'
                    '"root_cause_time":0,"confidence":"medium",'
                    '"unresolved_fields":["root_cause_time"],'
                    '"focus_component":"Tomcat02",'
                    '"followup_domains":["logs","trace"],'
                    '"rationale":"The named component still needs targeted verification."}'
                )
            return '{"summary":"stub","findings":[]}'

    buildspec = _buildspec_for_tmp(tmp_path)
    events: list[str] = []
    agent = create_executor_agent(llm_client=_WeakCheckpointLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
        on_event=lambda event: events.append(event.phase),
    )

    assert result.agents_instantiated == [
        "MetricsAgent",
        "LogsAgent",
        "TraceAgent",
        "JVMExpert",
        "MySQLExpert",
        "RedisExpert",
        "LogsAgent_2",
        "TraceAgent_2",
    ]
    assert "expand_enqueue" in events


def test_executor_verifies_a_medium_named_component_without_explicit_request(tmp_path) -> None:
    class _MediumCheckpointLLM:
        provider_name = "test"

        def complete(self, system_prompt: str, user_prompt: str, response_format=None) -> str:
            schema_name = (response_format or {}).get("json_schema", {}).get("name")
            if schema_name == "rca_coordinator_checkpoint":
                return (
                    '{"root_cause_component":"Tomcat02",'
                    '"root_cause_reason":"possible latency",'
                    '"root_cause_time":1614879500,"confidence":"medium",'
                    '"unresolved_fields":[],"focus_component":"Tomcat02",'
                    '"followup_domains":[],"rationale":"Only one domain supports the component."}'
                )
            return '{"summary":"stub","findings":[]}'

    buildspec = _buildspec_for_tmp(tmp_path)
    Path(buildspec.absolute_log_file[0]).write_text(
        "log_id,timestamp,cmdb_id,log_name,value\n"
        "a,1614879500,Tomcat02,app,timeout error\n",
        encoding="utf-8",
    )
    agent = create_executor_agent(llm_client=_MediumCheckpointLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(buildspec=buildspec, repository_path=str(tmp_path))

    assert len(result.agents_instantiated) == 8
    assert result.agents_instantiated[:6] == [
        "MetricsAgent",
        "LogsAgent",
        "TraceAgent",
        "JVMExpert",
        "MySQLExpert",
        "RedisExpert",
    ]


def test_coordinator_keeps_reason_as_a_concise_explicit_label(tmp_path) -> None:
    class _CoordinatorLLM:
        provider_name = "test"

        def complete(self, system_prompt: str, user_prompt: str, response_format=None) -> str:
            return (
                '{"root_cause_component":"currencyservice-abc-12345",'
                '"root_cause_reason":"progressive latency escalation",'
                '"root_cause_time":1614879500,"confidence":"high",'
                '"unresolved_fields":[],"focus_component":"frontend-abc-12345",'
                '"followup_domains":[],"rationale":"Explicit metric evidence."}'
            )

    decision = executor_module._coordinate_findings(
        llm_client=_CoordinatorLLM(),  # type: ignore[arg-type]
        buildspec=_buildspec_for_tmp(tmp_path),
        findings=[
            AssessFinding(
                agent="MetricsAgent",
                kind="anomaly",
                source="metric.csv",
                summary="Elevated pod CPU for component frontend-abc-12345.",
                evidence=[
                    "component=frontend-abc-12345",
                    "candidate_reason=cpu contention",
                ],
                severity="high",
            )
        ],
    )

    assert decision is not None
    assert decision.root_cause_component == "frontend-abc-12345"
    assert decision.root_cause_reason == "cpu contention"


def test_coordinator_prefers_repeated_exception_over_moderate_cpu_symptom(tmp_path) -> None:
    class _CoordinatorLLM:
        provider_name = "test"

        def complete(self, system_prompt: str, user_prompt: str, response_format=None) -> str:
            return (
                '{"root_cause_component":"currency-abc","root_cause_reason":"cpu contention",'
                '"root_cause_time":1614879500,"confidence":"medium","unresolved_fields":[],'
                '"focus_component":"currency-abc","followup_domains":[],"rationale":"Indirect."}'
            )

    decision = executor_module._coordinate_findings(
        llm_client=_CoordinatorLLM(),  # type: ignore[arg-type]
        buildspec=_buildspec_for_tmp(tmp_path),
        findings=[
            AssessFinding(
                agent="MetricsAgent",
                kind="anomaly",
                source="metric.csv",
                summary="Moderately elevated CPU.",
                evidence=[
                    "component=currency-abc",
                    "candidate_reason=cpu contention",
                    "pod_cpu_usage_rate_max=75.9",
                ],
                severity="high",
            ),
            AssessFinding(
                agent="LogsAgent",
                kind="anomaly",
                source="log.csv",
                summary="Repeated exception signature.",
                evidence=[
                    "component=frontend-abc",
                    "candidate_reason=exception",
                    "failure_signature_count=52",
                ],
                severity="high",
            ),
        ],
    )

    assert decision is not None
    assert decision.root_cause_component == "frontend-abc"
    assert decision.root_cause_reason == "exception"


def test_coordinator_prefers_corrobated_network_delay_over_moderate_cpu(tmp_path) -> None:
    class _CoordinatorLLM:
        provider_name = "test"

        def complete(self, system_prompt: str, user_prompt: str, response_format=None) -> str:
            return (
                '{"root_cause_component":"cart-abc","root_cause_reason":"latency",'
                '"root_cause_time":1614879500,"confidence":"high","unresolved_fields":[],'
                '"focus_component":"cart-abc","followup_domains":[],"rationale":"Trace."}'
            )

    findings = [
        AssessFinding(
            agent="MetricsAgent",
            kind="anomaly",
            source="metric.csv",
            summary="Moderately elevated CPU.",
            evidence=[
                "component=currency-abc",
                "candidate_reason=cpu contention",
                "pod_cpu_usage_rate_max=75.9",
            ],
            severity="high",
        )
    ]
    for index in range(3):
        findings.append(
            AssessFinding(
                agent="TraceAgent",
                kind="anomaly",
                source=f"trace-{index}.csv",
                summary="Sustained network delay.",
                evidence=[
                    "component=cart-abc",
                    "candidate_reason=network delay",
                    "network_p90_ms=252.0",
                ],
                severity="high",
            )
        )

    decision = executor_module._coordinate_findings(
        llm_client=_CoordinatorLLM(),  # type: ignore[arg-type]
        buildspec=_buildspec_for_tmp(tmp_path),
        findings=findings,
    )

    assert decision is not None
    assert decision.root_cause_component == "cart-abc"
    assert decision.root_cause_reason == "network delay"


def test_semantic_findings_distinguish_cpu_consumed_from_contention() -> None:
    findings = executor_module._semantic_context_findings(
        domain="metrics",
        tool_context={
            "semantic_columns": {"component_columns": ["PodName"]},
            "semantic_summary": {
                "top_component_values": [{"value": "frontend-abc", "count": 3}],
                "top_reason_values": [],
                "numeric_ranges": {"CpuUsageRate(%)": {"max": 99.8}},
                "window_observed_time_bounds": {},
            },
        },
        source_path="metric.csv",
        agent_name="MetricsAgent",
        known_components=[],
    )

    assert any("candidate_reason=cpu contention" in item.evidence for item in findings)

    consumed = executor_module._semantic_context_findings(
        domain="metrics",
        tool_context={
            "semantic_columns": {"component_columns": ["PodName"]},
            "semantic_summary": {
                "top_component_values": [{"value": "frontend-abc", "count": 3}],
                "top_reason_values": [],
                "numeric_ranges": {
                    "CpuUsageRate(%)": {"max": 99.8},
                    "NodeCpuUsageRate(%)": {"max": 4.8},
                },
                "window_observed_time_bounds": {},
            },
        },
        source_path="metric.csv",
        agent_name="MetricsAgent",
        known_components=[],
    )
    assert any("candidate_reason=cpu consumed" in item.evidence for item in consumed)


def test_semantic_findings_turn_repeated_log_failure_into_explicit_hypothesis() -> None:
    findings = executor_module._semantic_context_findings(
        domain="logs",
        tool_context={
            "semantic_columns": {"component_columns": ["PodName"]},
            "semantic_summary": {},
            "log_failure_signals": [
                {
                    "category": "exception",
                    "component": "frontend-abc",
                    "count": 8,
                    "first_ts": 1_661_140_920,
                    "sample": "failed to complete request",
                }
            ],
        },
        source_path="log.csv",
        agent_name="LogsAgent",
        known_components=[],
    )

    assert findings[0].evidence[:2] == [
        "component=frontend-abc",
        "candidate_reason=exception",
    ]


def test_semantic_trace_latency_exposes_network_delay_reason() -> None:
    findings = executor_module._semantic_context_findings(
        domain="trace",
        tool_context={
            "semantic_columns": {},
            "semantic_summary": {},
            "inter_service_network_latency": [
                {
                    "component": "cartservice-abc",
                    "p90_ms": 840.0,
                    "max_ms": 1200.0,
                    "peak_ts": 1_661_142_436,
                }
            ],
        },
        source_path="trace.csv",
        agent_name="TraceAgent",
        known_components=[],
    )

    assert findings[0].evidence[:2] == [
        "component=cartservice-abc",
        "candidate_reason=network delay",
    ]


def test_unique_structured_metric_can_converge_before_other_domains() -> None:
    assert executor_module._has_decisive_structured_metric(
        [
            AssessFinding(
                agent="MetricsAgent",
                kind="anomaly",
                source="metric.csv",
                summary="Explicit KPI anomaly.",
                evidence=[
                    "component=MG01",
                    "candidate_reason=high memory usage",
                    "kpi_range=30.0",
                    "peak_ts=1614789240",
                ],
                severity="high",
            )
        ]
    )

    assert not executor_module._has_decisive_structured_metric(
        [
            AssessFinding(
                agent="MetricsAgent",
                kind="anomaly",
                source="metric.csv",
                summary="Two competing KPI anomalies.",
                evidence=[
                    "component=MG01",
                    "candidate_reason=high memory usage",
                    "kpi_range=30.0",
                ],
                severity="high",
            ),
            AssessFinding(
                agent="MetricsAgent",
                kind="anomaly",
                source="metric.csv",
                summary="Competing KPI anomaly.",
                evidence=[
                    "component=MG02",
                    "candidate_reason=high CPU usage",
                    "kpi_range=42.0",
                ],
                severity="high",
            ),
        ]
    )


def test_repeated_direct_log_failure_can_skip_trace_agent() -> None:
    assert executor_module._has_decisive_direct_log_failure(
        [
            AssessFinding(
                agent="LogsAgent",
                kind="anomaly",
                source="log.csv",
                summary="Repeated local early return.",
                evidence=[
                    "component=checkout-abc",
                    "candidate_reason=return",
                    "failure_signature_count=25",
                    "propagated_failure=false",
                ],
                severity="high",
            )
        ]
    )


def test_executor_uses_llm_even_when_reasoning_disabled(tmp_path) -> None:
    class _CountingDecisionLLM:
        provider_name = "test"

        def __init__(self) -> None:
            self.call_count = 0

        def complete(self, system_prompt: str, user_prompt: str, response_format: dict | None = None) -> str:
            self.call_count += 1
            return (
                '{"summary":"LLM analyzed tool outputs.","findings":'
                '[{"kind":"observation","summary":"LLM-based observation.","severity":"low","evidence":["ok"]}]}'
            )

    buildspec = _buildspec_for_tmp(tmp_path)
    llm = _CountingDecisionLLM()
    agent = ExecutorAgent(
        llm_client=llm,  # type: ignore[arg-type]
        enable_reasoning=False,
    )
    result = agent.execute_assess(
        buildspec=buildspec,
        repository_path=str(tmp_path),
    )
    assert result.task_results
    llm_backed_results = [
        item for item in result.task_results if not item.agent_name.endswith("Expert")
    ]
    assert llm.call_count >= len(llm_backed_results)
    assert all("decision_source=llm" in item.detail for item in llm_backed_results)


def test_executor_preserves_novel_telemetry_backed_causes(tmp_path) -> None:
    class _NovelCauseLLM:
        provider_name = "test"

        def complete(self, system_prompt: str, user_prompt: str, response_format: dict | None = None) -> str:
            assert "non-exhaustive" in system_prompt
            return (
                '{"summary":"novel evidence","findings":'
                '[{"kind":"anomaly","summary":"Thread-pool starvation caused request rejection.",'
                '"severity":"high","evidence":["active_workers=64 queue_depth=900"]}]}'
            )

    buildspec = _buildspec_for_tmp(tmp_path)
    agent = ExecutorAgent(llm_client=_NovelCauseLLM())  # type: ignore[arg-type]

    result = agent.execute_assess(buildspec=buildspec, repository_path=str(tmp_path))

    assert "Thread-pool starvation caused request rejection." in result.preliminary_causes
