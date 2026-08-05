"""Tests for the evidence-independent V3 RCA strategy."""

from __future__ import annotations

from pathlib import Path

from agents.v3_expert_agent import V3MetricExpertAgent
from aware_models.buildspec import BuildSpec
from aware_models.executor import AssessFinding, CoordinatorDecision
from runtime import agent_factory
from runtime.causal_graph import build_causal_graph


def _buildspec(metric_path: Path) -> BuildSpec:
    return BuildSpec.model_validate(
        {
            "task_type": "task_7",
            "date": "2021-03-04",
            "filename_date": "2021_03_04",
            "failure_time_range": {"start": "00:00:00", "end": "00:05:00"},
            "failure_time_range_ts": {"start": 1000, "end": 1300},
            "failures_detected": 1,
            "uncertainty": {
                "root_cause_time": "unknown",
                "root_cause_component": "unknown",
                "root_cause_reason": "unknown",
            },
            "objective": "Identify the root cause",
            "filename_date_directory": str(metric_path.parent.resolve()),
            "absolute_log_file": [],
            "absolute_trace_file": [],
            "absolute_metrics_file": [str(metric_path.resolve())],
        }
    )


def _write_metrics(path: Path, rows: list[tuple[int, str, str, float]]) -> None:
    path.write_text(
        "timestamp,cmdb_id,kpi_name,value\n"
        + "".join(f"{ts},{component},{kpi},{value}\n" for ts, component, kpi, value in rows),
        encoding="utf-8",
    )


def test_v3_experts_detect_jvm_cpu_oom_and_redis_memory(tmp_path: Path) -> None:
    metric = tmp_path / "metric_container.csv"
    rows: list[tuple[int, str, str, float]] = []
    for ts, cpu, non_heap, redis_memory in (
        (1000, 0.2, 100_000_000, 10),
        (1060, 0.3, 100_000_000, 75),
        (1120, 50.0, 130_000_000, 75),
        (1180, 49.0, 132_000_000, 10),
    ):
        rows.extend(
            [
                (ts, "MG02", "JVM-Operating System_JVM_JVM_CPULoad", cpu),
                (ts, "MG03", "JVM-Memory_JVM_Memory_NoHeapMemoryUsed", non_heap),
                (ts, "Redis02", "OSLinux_MEMORY_NoCacheMemPerc", redis_memory),
                (ts, "Mysql01", "OSLinux_MEMORY_MEMUsedMemPerc", 98),
            ]
        )
    _write_metrics(metric, rows)
    buildspec = _buildspec(metric)

    findings = []
    for specialty in ("jvm", "mysql", "redis"):
        findings.extend(V3MetricExpertAgent(specialty).analyze([metric], buildspec).findings)

    hypotheses = {
        (item.evidence[0], item.evidence[1], item.evidence[-2]) for item in findings
    }
    assert ("component=MG02", "candidate_reason=high JVM CPU load", "peak_ts=1120") in hypotheses
    assert ("component=MG03", "candidate_reason=JVM Out of Memory (OOM) Heap", "peak_ts=1120") in hypotheses
    assert ("component=Redis02", "candidate_reason=high memory usage", "peak_ts=1060") in hypotheses
    assert not any(item.agent == "MySQLExpert" for item in findings)


def test_causal_graph_preserves_support_and_selected_outcome() -> None:
    finding = AssessFinding(
        agent="JVMExpert",
        kind="anomaly",
        source="metric_container.csv",
        summary="Independent JVM transition.",
        evidence=[
            "component=MG02",
            "candidate_reason=high JVM CPU load",
            "peak_ts=1120",
            "independent_evidence=true",
        ],
        severity="high",
    )
    decision = CoordinatorDecision(
        root_cause_component="MG02",
        root_cause_reason="high JVM CPU load",
        root_cause_time=1120,
        confidence="high",
    )

    graph = build_causal_graph([finding], decision)

    assert graph.root_node_id
    assert {node.kind for node in graph.nodes} == {
        "evidence",
        "component",
        "hypothesis",
        "outcome",
    }
    assert any(edge.relation == "supports" for edge in graph.edges)
    assert any(edge.relation == "causes" for edge in graph.edges)


def test_factory_selects_v3_strategy_and_knowledge(monkeypatch) -> None:
    monkeypatch.setenv("AWARE_RCA_VERSION", "v3")
    monkeypatch.delenv("AWARE_EXECUTOR_V3_KB_FILE", raising=False)
    agent_factory._EXECUTOR_AGENT = None

    executor = agent_factory.get_executor_agent(llm_client=None)

    assert executor.execution_mode == "v3"
    assert executor.knowledge_file == "knowledge/executor_rca_v3_kb.md"
    agent_factory._EXECUTOR_AGENT = None


def test_v3_knowledge_has_dataset_semantics_without_bank_answer_list() -> None:
    knowledge = Path("knowledge/executor_rca_v3_kb.md").read_text(encoding="utf-8")

    assert "### OpenRCA Market" in knowledge
    assert "### OpenRCA Telecom" in knowledge
    assert "startTime" in knowledge
    assert "timestamp,cmdb_id,kpi_name,value" in knowledge
    assert "## GENERAL RCA RULES" in knowledge
    assert "- MG01" not in knowledge
    assert "- Mysql02" not in knowledge
