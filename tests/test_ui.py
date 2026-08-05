"""UI server tests."""

from __future__ import annotations

import json
import csv
import io

from fastapi.testclient import TestClient

from llm.mock import MockLLMClient
from ui.server import create_app


def test_ui_health() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ui_index_exposes_assessment_workspace() -> None:
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="assessForm"' in response.text
    assert 'id="diagnosisComponent"' in response.text
    assert 'id="view-findings"' in response.text
    assert 'id="view-graph"' in response.text
    assert 'id="causalGraphOutput"' in response.text
    assert 'id="graphIncidentSelect"' in response.text
    assert 'id="downloadGraphPng"' in response.text
    assert "downloadCausalGraphPng" in response.text
    assert "showIncidentGraph" in response.text
    assert "causal_graph:payload?.assessment_output?.causal_graph" in response.text
    assert 'id="versionState"' in response.text
    assert 'id="stopBtn"' in response.text
    assert 'id="datasetProfile"' in response.text
    assert 'id="timezoneOffset"' in response.text
    assert 'id="batchCsv"' in response.text
    assert "loadDatasetCatalogue" in response.text
    assert "incidents loaded automatically" in response.text
    assert "optional override" in response.text
    assert 'id="batchSuccessRate"' in response.text
    assert 'id="view-batch"' in response.text
    assert 'id="agentCount"' in response.text
    assert 'id="batchManifestLink"' in response.text
    assert "<th>Agents</th>" in response.text
    assert "<th>Logs</th>" in response.text
    assert "<th>Graph</th>" in response.text
    assert "OpenRCA · Bank" in response.text
    assert "OpenRCA · Market" in response.text
    assert "OpenRCA · Telecom" in response.text


def test_ui_downloads_nezha_problem_catalogue(tmp_path) -> None:
    dated = tmp_path / "rca_data" / "2023-01-30"
    dated.mkdir(parents=True)
    (dated / "2023-01-30-fault_list.json").write_text(
        json.dumps(
            {
                "11": [
                    {
                        "inject_time": "2023-01-30 11:51:46",
                        "inject_timestamp": "1675079506",
                        "inject_pod": "contacts-pod",
                        "inject_type": "network_delay",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app())

    response = client.get("/api/nezha/problems.csv", params={"source_path": str(tmp_path)})

    assert response.status_code == 200
    assert "nezha_incidents.csv" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["expected_reason"] == "network_delay"


def test_ui_downloads_openrca_bank_problem_catalogue(tmp_path) -> None:
    bank = tmp_path / "Bank"
    bank.mkdir()
    (bank / "record.csv").write_text(
        "level,component,timestamp,datetime,reason\n"
        "pod,Mysql02,1614841020.0,2021-03-04 14:57:00,high memory usage\n",
        encoding="utf-8",
    )
    (bank / "query.csv").write_text(
        "task_index,instruction,scoring_points\n"
        'task_7,"On March 4, 2021, from 14:30 to 15:00, one failure occurred.",full RCA\n',
        encoding="utf-8",
    )
    client = TestClient(create_app())

    response = client.get(
        "/api/openrca/bank/problems.csv",
        params={"source_path": str(bank)},
    )

    assert response.status_code == 200
    assert "openrca_bank_incidents.csv" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["timezone_offset_minutes"] == "480"
    assert rows[0]["expected_component"] == "Mysql02"


def test_ui_parse_stream_returns_result(tmp_path, monkeypatch) -> None:
    from ui import server as ui_server
    from agents import parser_agent as parser_module

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(ui_server, "create_llm_client", lambda **_: MockLLMClient(invalid_attempts=1))
    monkeypatch.setattr(parser_module, "create_llm_client", lambda **_: MockLLMClient(invalid_attempts=1))
    original_record = ui_server.record_batch_incident

    def record_in_test_output(**kwargs):
        return original_record(**kwargs, output_root=tmp_path / "backend-output")

    monkeypatch.setattr(ui_server, "record_batch_incident", record_in_test_output)
    client = TestClient(create_app())
    params = {
        "query": "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout",
        "repo": str(tmp_path),
        "max_attempts": 4,
        "dataset_profile": "nezha",
        "timezone_offset_minutes": 0,
        "batch_id": "batch-integration",
        "problem_id": "nezha-001",
    }

    with client.stream("GET", "/api/parse-stream", params=params) as response:
        assert response.status_code == 200
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk

    payload_lines = [line for line in buffer.splitlines() if line.startswith("data: ")]
    assert payload_lines
    payloads = [json.loads(line[6:]) for line in payload_lines]
    assert any(item.get("type") == "event" for item in payloads)
    assert any(item.get("type") == "result" for item in payloads)
    parser_payload = next(item["payload"] for item in payloads if item.get("type") == "parser_result")
    assert parser_payload["dataset_profile"] == "nezha"
    assert parser_payload["timezone_offset_minutes"] == 0
    result = next(item["payload"] for item in payloads if item.get("type") == "result")
    assert result["execution"]["batch_id"] == "batch-integration"
    assert result["execution"]["problem_id"] == "nezha-001"
    assert result["execution"]["agents_created"] == len(
        result["executor"]["agents_instantiated"]
    )
    assert result["batch_log"]["incident_log_url"].endswith(
        "/batch-integration/incidents/nezha-001"
    )

    incident = json.loads(
        (
            tmp_path
            / "backend-output"
            / "batches"
            / "batch-integration"
            / "incidents"
            / "nezha-001.json"
        ).read_text()
    )
    assert incident["run_id"] == result["execution"]["run_id"]
    assert incident["agents_created"] == result["execution"]["agents_created"]
    assert incident["events"]
    assert incident["result"]["assessment_output"] == result["assessment_output"]
