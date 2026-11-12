"""CLI batch and causal-graph export tests."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw
import pytest
from typer.testing import CliRunner

from aware_models.executor import CausalGraph, CausalGraphEdge, CausalGraphNode
from cli import main as cli_module
from runtime import agent_factory
from runtime.graph_export import export_causal_graph_png


def test_exports_causal_graph_as_real_png(tmp_path: Path, monkeypatch) -> None:
    graph = CausalGraph(
        nodes=[
            CausalGraphNode(id="e1", kind="evidence", label="CPU transition", source_agent="JVMExpert"),
            CausalGraphNode(id="e2", kind="evidence", label="Unlinked note", source_agent="LogsAgent"),
            CausalGraphNode(id="c1", kind="component", label="MG02", component="MG02"),
            CausalGraphNode(id="h1", kind="hypothesis", label="MG02 · JVM CPU", component="MG02"),
            CausalGraphNode(
                id="o1", kind="outcome", label="Selected root cause", component="MG02",
                reason="JVM CPU load", timestamp=1614816120, confidence=0.9,
            ),
        ],
        edges=[
            CausalGraphEdge(source="e1", target="c1", relation="observed_on", confidence=0.8),
            CausalGraphEdge(source="c1", target="h1", relation="causes", confidence=0.8),
            CausalGraphEdge(source="h1", target="o1", relation="supports", confidence=0.9),
        ],
        root_node_id="h1",
    )

    labels: list[str] = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(self, xy, text, *args, **kwargs):
        labels.append(str(text))
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    output = export_causal_graph_png(graph, tmp_path / "graph.png")

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "Selected Root Cause · 1" in labels
    assert "Outcome · 1" not in labels
    assert "MG02" in labels
    assert "90% confidence" in labels
    assert "2021-03-04 00:02:00 UTC" in labels
    assert "Other findings · 1" in labels
    assert "No graph relation shown for these observations." in labels
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width >= 3000
    labels.clear()
    export_causal_graph_png(graph, tmp_path / "graph-utc8.png", timezone_offset_minutes=480)
    assert "2021-03-04 08:02:00 UTC+08:00" in labels


def test_batch_command_is_exposed_with_graph_options() -> None:
    runner = CliRunner()

    result = runner.invoke(cli_module.app, ["batch", "--help"])

    assert result.exit_code == 0
    assert "--problems-csv" in result.stdout
    assert "--dataset" in result.stdout
    assert "--source" in result.stdout
    assert "--results-csv" in result.stdout
    assert "--graph-dir" in result.stdout
    assert "openrca_bank" in result.stdout


@pytest.mark.parametrize("input_mode", ["csv", "nezha"])
def test_batch_command_runs_one_incident_and_writes_outputs(tmp_path, monkeypatch, input_mode) -> None:
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
    labels = tmp_path / "telemetry" / "rca_data" / "2021-03-04"
    labels.mkdir(parents=True)
    (labels / "2021-03-04-fault_list.json").write_text(
        json.dumps({"00": [{
            "inject_time": "2021-03-04 00:02:00",
            "inject_timestamp": "1614816120",
            "inject_pod": "MG02",
            "inject_type": "high JVM CPU load",
        }]}),
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
    monkeypatch.setenv("AWARE_ENABLE_MEMORY", "true" if input_mode == "nezha" else "false")
    monkeypatch.setenv("AWARE_ENABLE_REASONING", "false")
    monkeypatch.setenv("AWARE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-cli-test")
    monkeypatch.setattr(cli_module, "create_llm_client", lambda **_: MockLLMClient())
    agent_factory._PARSER_AGENT = None
    agent_factory._EXECUTOR_AGENT = None

    source_args = ["--problems-csv", str(problems)] if input_mode == "csv" else [
        "--dataset", "nezha", "--source", str(tmp_path / "telemetry")
    ]
    result = CliRunner().invoke(cli_module.app, [
        "batch", *source_args, "--batch-id", "batch-cli-test", "--limit", "1",
    ])

    assert result.exit_code == 0, result.stdout
    assert "Provider: openai | Model: gpt-cli-test" in result.stdout
    assert f"Reasoning: Disabled | Memory: {'Enabled' if input_mode == 'nezha' else 'Disabled'}" in result.stdout
    results_path = tmp_path / "output" / "batches" / "batch-cli-test" / "results.csv"
    rows = list(csv.DictReader(results_path.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["problem_id"] == (
        "nezha-cli-001" if input_mode == "csv" else "nezha-001-2021-03-04-000200"
    )
    assert Path(rows[0]["graph_png_path"]).is_file()
    assert Path(rows[0]["artifact_json_path"]).parent == results_path.parent / "json"
    assert Path(rows[0]["artifact_text_path"]).parent == results_path.parent / "txt"
    assert Path(rows[0]["artifact_json_path"]).is_file()
    assert Path(rows[0]["artifact_text_path"]).is_file()
    assert (results_path.parent / "manifest.json").is_file()
    config = json.loads((results_path.parent / "config.json").read_text(encoding="utf-8"))
    assert config["llm_provider"] == "openai"
    assert config["llm_model"] == "gpt-cli-test"
    assert config["reasoning_enabled"] is False
    assert config["memory_enabled"] is (input_mode == "nezha")
    assert "api_key" not in config
    catalogue = results_path.parent / "problems.csv"
    assert catalogue.is_file()
    with catalogue.open(encoding="utf-8") as handle:
        generated = list(csv.DictReader(handle))
    assert len(generated) == 1
    assert generated[0]["source_path"] == str((tmp_path / "telemetry").resolve())
    memory_files = list((results_path.parent / "memory").glob("*.db"))
    assert len(memory_files) == (1 if input_mode == "nezha" else 0)
    if memory_files:
        assert memory_files[0].name == f"{rows[0]['run_id']}.db"
        assert rows[0]["memory_db_path"] == str(memory_files[0])
    else:
        assert rows[0]["memory_db_path"] == ""
    agent_factory._PARSER_AGENT = None
    agent_factory._EXECUTOR_AGENT = None


@pytest.mark.parametrize("dataset", ["openrca_bank", "openrca_market", "openrca_telecom"])
def test_batch_command_prepares_openrca_dataset_directly(tmp_path, monkeypatch, dataset) -> None:
    from llm.mock import MockLLMClient

    variant = dataset.removeprefix("openrca_")
    root = tmp_path / variant.title()
    systems = ("cloudbed-1", "cloudbed-2") if variant == "market" else (None,)
    for system in systems:
        source = root / system if system else root
        source.mkdir(parents=True, exist_ok=True)
        (source / "record.csv").write_text(
            "level,component,timestamp,datetime,reason\n"
            "pod,MG02,1614841020,2021-03-04 14:57:00,high CPU usage\n",
            encoding="utf-8",
        )
        (source / "query.csv").write_text(
            "task_index,instruction,scoring_points\n"
            'task_7,"On March 4, 2021, from 14:30 to 15:00, one failure occurred.",full RCA\n',
            encoding="utf-8",
        )
        metric_dir = source / "telemetry" / "2021_03_04" / "metric"
        metric_dir.mkdir(parents=True)
        (metric_dir / "metric_container.csv").write_text(
            "timestamp,cmdb_id,kpi_name,value\n"
            "1614840900,MG02,cpu,0.2\n"
            "1614841020,MG02,cpu,50.0\n",
            encoding="utf-8",
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWARE_ENABLE_MEMORY", "false")
    monkeypatch.setenv("AWARE_ENABLE_REASONING", "false")
    monkeypatch.setenv("AWARE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-cli-test")
    monkeypatch.setattr(cli_module, "create_llm_client", lambda **_: MockLLMClient())
    agent_factory._PARSER_AGENT = None
    agent_factory._EXECUTOR_AGENT = None

    selected_source = (
        root / "cloudbed-1" / "telemetry" / "2021_03_04"
        if variant == "market" else root / "telemetry" / "2021_03_04"
    )
    id_args = [] if variant == "bank" else ["--batch-id", f"batch-{variant}"]
    result = CliRunner().invoke(cli_module.app, [
        "batch", "--dataset", dataset, "--source", str(selected_source),
        "--limit", "1", *id_args,
    ])

    assert result.exit_code == 0, result.stdout
    batch_dirs = list((tmp_path / "output" / "batches").iterdir())
    assert len(batch_dirs) == 1
    batch_root = batch_dirs[0]
    if variant == "bank":
        assert re.fullmatch(r"batch-cli-\d{8}T\d{6}Z-[0-9a-f]{6}", batch_root.name)
    else:
        assert batch_root.name == f"batch-{variant}"
    config = json.loads((batch_root / "config.json").read_text(encoding="utf-8"))
    assert config["dataset"] == dataset
    assert config["source_path"] == str(selected_source.resolve())
    with (batch_root / "problems.csv").open(encoding="utf-8", newline="") as handle:
        problems = list(csv.DictReader(handle))
    assert len(problems) == 1
    assert problems[0]["problem_id"].startswith(f"openrca-{variant}-")
    assert problems[0]["timezone_offset_minutes"] == "480"
    assert Path(problems[0]["source_path"]).is_dir()
    with (batch_root / "results.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["problem_id"] == problems[0]["problem_id"]
    assert (batch_root / "manifest.json").is_file()
    agent_factory._PARSER_AGENT = None
    agent_factory._EXECUTOR_AGENT = None
