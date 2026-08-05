"""Assess reporting helper tests."""

from __future__ import annotations

from aware_models.buildspec import BuildSpec
from aware_models.executor import AgentTaskResult, AssessFinding, ExecutorRunResult
from runtime import reporting as reporting_module
from runtime.reporting import build_assessment_output


def test_component_extraction_does_not_return_linking_word() -> None:
    finding = AssessFinding(
        agent="MetricsAgent",
        kind="anomaly",
        source="metric.csv",
        summary="The affected component is frontend-579b9bff58-t2dbm due to CPU contention.",
        evidence=["component=frontend-579b9bff58-t2dbm"],
        severity="high",
    )

    assert reporting_module._component_from_finding(finding) == "frontend-579b9bff58-t2dbm"
    assert reporting_module._infer_root_cause_component([finding], reporting_module.Counter())[0] == (
        "frontend-579b9bff58-t2dbm"
    )
    assert reporting_module._infer_root_cause_reason([finding], [], reporting_module.Counter())[0] == (
        "cpu contention"
    )


def test_build_assessment_output_has_explicit_sections_and_no_unknown() -> None:
    buildspec = BuildSpec.model_validate(
        {
            "task_type": "task_7",
            "date": "2021-03-04",
            "filename_date": "2021_03_04",
            "failure_time_range": {"start": "14:30:00", "end": "15:00:00"},
            "failure_time_range_ts": {"start": 1614868200, "end": 1614870000},
            "failures_detected": 1,
            "uncertainty": {
                "root_cause_time": "unknown",
                "root_cause_component": "unknown",
                "root_cause_reason": "unknown",
            },
            "objective": "Identify time/component/reason.",
            "filename_date_directory": "/tmp/2021_03_04",
            "absolute_log_file": ["/tmp/2021_03_04/log/log_service.csv"],
            "absolute_trace_file": ["/tmp/2021_03_04/trace/trace_span.csv"],
            "absolute_metrics_file": ["/tmp/2021_03_04/metric/metric_app.csv"],
        }
    )
    finding = AssessFinding(
        agent="TraceAgent",
        kind="anomaly",
        source="/tmp/2021_03_04/trace/trace_span.csv",
        summary="Slow span detected for MG01 at 1614868200.",
        evidence=["1614868200123,MG01,parent,span,trace,900"],
        severity="high",
    )
    result = ExecutorRunResult(
        buildspec=buildspec,
        agents_instantiated=["TraceAgent"],
        task_results=[],
        findings=[finding],
        preliminary_causes=["Downstream timeout likely contributed to the incident."],
        confidence=0.9,
        summary="Executor completed Assess.",
    )

    output = build_assessment_output(buildspec, result)

    assert isinstance(output.get("findings"), list)
    assert isinstance(output.get("anomalies"), list)
    assert isinstance(output.get("preliminary_causes"), list)
    assert isinstance(output.get("root_cause_candidates"), list)
    synthesis = output.get("root_cause_synthesis")
    assert isinstance(synthesis, dict)
    assert synthesis.get("task_type") == "root_cause_synthesis"
    assert "metrics_summary" in synthesis
    assert "trace_summary" in synthesis
    assert "log_summary" in synthesis
    assert isinstance(synthesis.get("final_diagnosis"), dict)
    assert isinstance(synthesis.get("uncertainty"), dict)
    final_reporting = output.get("final_reporting")
    assert isinstance(final_reporting, dict)
    assert final_reporting.get("root cause component") != "unknown"
    assert final_reporting.get("root cause timestamp") != "unknown"
    assert final_reporting.get("root cause reason") != "unknown"


def test_build_assessment_output_respects_task_scope_task1() -> None:
    buildspec = BuildSpec.model_validate(
        {
            "task_type": "task_1",
            "date": "2021-03-04",
            "filename_date": "2021_03_04",
            "failure_time_range": {"start": "14:30:00", "end": "15:00:00"},
            "failure_time_range_ts": {"start": 1614868200, "end": 1614870000},
            "failures_detected": 1,
            "uncertainty": {
                "root_cause_time": "unknown",
                "root_cause_component": "known",
                "root_cause_reason": "known",
            },
            "objective": "Determine root cause time only.",
            "filename_date_directory": "/tmp/2021_03_04",
            "absolute_log_file": ["/tmp/2021_03_04/log/log_service.csv"],
            "absolute_trace_file": ["/tmp/2021_03_04/trace/trace_span.csv"],
            "absolute_metrics_file": ["/tmp/2021_03_04/metric/metric_app.csv"],
        }
    )
    finding = AssessFinding(
        agent="TraceAgent",
        kind="anomaly",
        source="/tmp/2021_03_04/trace/trace_span.csv",
        summary="Slow span detected for MG01 at 1614868200.",
        evidence=["1614868200123,MG01,parent,span,trace,900"],
        severity="high",
    )
    result = ExecutorRunResult(
        buildspec=buildspec,
        agents_instantiated=["TraceAgent"],
        task_results=[],
        findings=[finding],
        preliminary_causes=["Downstream timeout likely contributed to the incident."],
        confidence=0.9,
        summary="Executor completed Assess.",
    )

    output = build_assessment_output(buildspec, result)
    scope = output.get("buildspec_resolution_scope")
    assert isinstance(scope, dict)
    assert scope.get("requested_fields") == ["root_cause_time"]

    final_reporting = output.get("final_reporting")
    assert isinstance(final_reporting, dict)
    assert set(final_reporting.keys()) == {"root cause timestamp"}
    assert final_reporting.get("root cause timestamp") != "insufficient_evidence"


def test_build_assessment_output_time_fallback_from_task_result_detail() -> None:
    buildspec = BuildSpec.model_validate(
        {
            "task_type": "task_1",
            "date": "2021-03-04",
            "filename_date": "2021_03_04",
            "failure_time_range": {"start": "14:30:00", "end": "15:00:00"},
            "failure_time_range_ts": {"start": 1614868200, "end": 1614870000},
            "failures_detected": 1,
            "uncertainty": {
                "root_cause_time": "unknown",
                "root_cause_component": "known",
                "root_cause_reason": "known",
            },
            "objective": "Determine root cause time only.",
            "filename_date_directory": "/tmp/2021_03_04",
            "absolute_log_file": ["/tmp/2021_03_04/log/log_service.csv"],
            "absolute_trace_file": ["/tmp/2021_03_04/trace/trace_span.csv"],
            "absolute_metrics_file": ["/tmp/2021_03_04/metric/metric_app.csv"],
        }
    )
    result = ExecutorRunResult(
        buildspec=buildspec,
        agents_instantiated=["TraceAgent"],
        task_results=[
            AgentTaskResult(
                agent_name="TraceAgent",
                target_path="/tmp/2021_03_04/trace/trace_span.csv",
                status="ok",
                findings=[],
                detail=(
                    "Analyzed strict time-window slice: window=[1614868200,1614870000], "
                    "rows_in_window=12, total_rows=100, timestamp_field=timestamp. "
                    "Observed in-window timestamp bounds: min_ts=1614868200, max_ts=1614869920."
                ),
            )
        ],
        findings=[],
        preliminary_causes=[],
        confidence=0.4,
        summary="Executor completed Assess.",
    )

    output = build_assessment_output(buildspec, result)
    final_reporting = output.get("final_reporting")
    assert isinstance(final_reporting, dict)
    assert final_reporting.get("root cause timestamp") == 1614868200
    evidence = output.get("resolution_evidence", {})
    assert isinstance(evidence, dict)
    assert "timestamp_from_task_result_detail=1614868200" in evidence.get("root_cause_time", [])
