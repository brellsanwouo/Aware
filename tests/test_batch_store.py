"""Tests for durable browser batch journals."""

from __future__ import annotations

import json

import pytest

from runtime.batch_store import batch_directory, incident_log_path, record_batch_incident


def test_records_each_incident_and_accumulates_batch_manifest(tmp_path) -> None:
    for index, agents in ((1, 3), (2, 5)):
        problem_id = f"nezha-{index:03d}"
        record_batch_incident(
            batch_id="batch-test",
            problem_id=problem_id,
            run_id=f"run-{index}",
            status="success",
            agents_created=agents,
            events=[
                {
                    "timestamp": "2026-08-05T12:00:00+00:00",
                    "sender": "ExecutorAgent",
                    "recipient": "Runtime",
                    "phase": "instantiate_agent",
                    "content": f"Created agent {index}",
                }
            ],
            artifacts={"json_path": f"output/run-{index}.json"},
            result_payload={"incident": problem_id},
            error_message=None,
            output_root=tmp_path,
        )

    manifest = json.loads(
        (batch_directory("batch-test", tmp_path) / "manifest.json").read_text()
    )
    assert [item["problem_id"] for item in manifest["incidents"]] == [
        "nezha-001",
        "nezha-002",
    ]
    assert [item["agents_created"] for item in manifest["incidents"]] == [3, 5]

    incident_path = incident_log_path("batch-test", "nezha-002", tmp_path)
    incident = json.loads(incident_path.read_text())
    assert incident["agents_created"] == 5
    assert incident["events"][0]["phase"] == "instantiate_agent"
    assert incident["result"] == {"incident": "nezha-002"}
    text_log = incident_path.with_suffix(".log").read_text()
    assert "agents_created: 5" in text_log
    assert "Created agent 2" in text_log
    assert '"incident": "nezha-002"' in text_log


@pytest.mark.parametrize("identifier", ["../escape", "with/slash", "", "a" * 121])
def test_rejects_unsafe_batch_identifiers(tmp_path, identifier) -> None:
    with pytest.raises(ValueError):
        batch_directory(identifier, tmp_path)
