"""CLI batch and causal-graph export tests."""

from __future__ import annotations

import csv
from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from aware_models.executor import CausalGraph, CausalGraphEdge, CausalGraphNode
from cli import main as cli_module
from runtime import agent_factory
from runtime.graph_export import export_causal_graph_png


def test_exports_causal_graph_as_real_png(tmp_path: Path) -> None:
    graph = CausalGraph(
        nodes=[
            CausalGraphNode(id="e1", kind="evidence", label="CPU transition", source_agent="JVMExpert"),
            CausalGraphNode(id="c1", kind="component", label="MG02", component="MG02"),
            CausalGraphNode(id="h1", kind="hypothesis", label="MG02 · JVM CPU", component="MG02"),
            CausalGraphNode(id="o1", kind="outcome", label="Selected root cause", component="MG02"),
        ],
        edges=[
            CausalGraphEdge(source="e1", target="c1", relation="observed_on", confidence=0.8),
            CausalGraphEdge(source="c1", target="h1", relation="causes", confidence=0.8),
            CausalGraphEdge(source="h1", target="o1", relation="supports", confidence=0.9),
        ],
        root_node_id="h1",
    )

    output = export_causal_graph_png(graph, tmp_path / "graph.png")

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width >= 2000


def test_batch_command_is_exposed_with_graph_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli_module.app, ["batch", "--help"])

    assert result.exit_code == 0
    assert "--problems-csv" in result.stdout
    assert "--results-csv" in result.stdout
    assert "--graph-dir" in result.stdout


def test_batch_command_runs_one_incident_and_writes_outputs(tmp_path, monkeypatch) -> None:
    from llm.mock import MockLLMClient

    dated = tmp_path / "telemetry" / "2021_03_04"
    metric = dated / "metric" / "metric_container.csv"
    metric.parent.mkdir(parents=True)
    metric.write_text(
        "timestamp,cmdb_id,kpi_name,value\n"
        "1614816000,MG02,JVM-Operating System_JVM_JVM_CPULoad,0.2\n"
        "1614816060,MG02,JVM-Operating System_JVM_JVM_CPULoad,0.3\n"
        "1614816120,MG02,JVM-Operating System_JVM_JVM_CPULoad,50.0\n"
        "1614816180,MG02,JVM-Operating System_JVM_JVM_CPULoad,49.0\n",
        encoding="utf-8",
    )
    log = dated / "log" / "log_service.csv"
    trace = dated / "trace" / "trace_span.csv"
    log.parent.mkdir(parents=True)
    trace.parent.mkdir(parents=True)
    log.write_text(
        "log_id,timestamp,cmdb_id,log_name,value\n"
        "l1,1614816120,MG02,application,healthy\n",
        encoding="utf-8",
    )
    trace.write_text(
        "timestamp,cmdb_id,parent_id,span_id,trace_id,duration\n"
        "1614816120000,MG02,p1,s1,t1,10\n",
        encoding="utf-8",
    )
    problems = tmp_path / "problems.csv"
    with problems.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "problem_id", "system", "date", "start_time", "end_time",
                "timezone_offset_minutes", "source_path", "expected_component",
                "expected_time", "expected_reason",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "problem_id": "nezha-cli-001",
                "system": "test",
                "date": "2021-03-04",
                "start_time": "00:00:00",
                "end_time": "00:05:00",
                "timezone_offset_minutes": "0",
                "source_path": str(tmp_path / "telemetry"),
                "expected_component": "MG02",
                "expected_time": "1614816120",
                "expected_reason": "high JVM CPU load",
            }
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWARE_ENABLE_MEMORY", "false")
    monkeypatch.setattr(cli_module, "create_llm_client", lambda **_: MockLLMClient())
    agent_factory._PARSER_AGENT = None
    agent_factory._EXECUTOR_AGENT = None

    result = CliRunner().invoke(
        cli_module.app,
        [
            "batch", "--problems-csv", str(problems),
            "--batch-id", "batch-cli-test", "--limit", "1",
        ],
    )

    assert result.exit_code == 0, result.stdout
    results_path = tmp_path / "output" / "batches" / "batch-cli-test" / "results.csv"
    rows = list(csv.DictReader(results_path.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["problem_id"] == "nezha-cli-001"
    assert Path(rows[0]["graph_png_path"]).is_file()
    assert (results_path.parent / "manifest.json").is_file()
    agent_factory._PARSER_AGENT = None
    agent_factory._EXECUTOR_AGENT = None
