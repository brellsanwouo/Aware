"""Tests for the Nezha problem catalogue and scoring."""

from __future__ import annotations

import csv
import io
import json

from runtime.nezha_dataset import build_nezha_problems, problems_to_csv, score_diagnosis


def test_build_nezha_problem_csv_from_extracted_labels(tmp_path) -> None:
    root = tmp_path / "Nezha"
    dated = root / "rca_data" / "2023-01-30"
    dated.mkdir(parents=True)
    label = {
        "11": [
            {
                "inject_time": "2023-01-30 11:51:46",
                "inject_timestamp": "1675079506",
                "inject_pod": "ts-contacts-service-866bd68c97-dzqgd",
                "inject_type": "network_delay",
            }
        ]
    }
    (dated / "2023-01-30-fault_list.json").write_text(json.dumps(label), encoding="utf-8")

    problems = build_nezha_problems(root)
    rows = list(csv.DictReader(io.StringIO(problems_to_csv(problems))))

    assert len(rows) == 1
    assert rows[0]["problem_id"] == "nezha-001-2023-01-30-115146"
    assert rows[0]["start_time"] == "11:51:00"
    assert rows[0]["end_time"] == "11:53:59"
    assert rows[0]["timezone_offset_minutes"] == "0"
    assert rows[0]["expected_component"] == "ts-contacts-service-866bd68c97-dzqgd"
    assert rows[0]["source_path"] == str(root.resolve())


def test_score_nezha_diagnosis_uses_reason_alias_and_time_tolerance() -> None:
    problem = {
        "expected_component": "ts-contacts-service-866bd68c97-dzqgd",
        "expected_reason": "network_delay",
        "expected_time": "1675079506",
    }
    diagnosis = {
        "root_cause_component": "ts-contacts-service-866bd68c97-dzqgd",
        "root_cause_reason": "network delay",
        "root_cause_time": 1675079530,
    }

    score = score_diagnosis(diagnosis, problem)

    assert score == {
        "success": True,
        "component_ok": True,
        "reason_ok": True,
        "time_ok": True,
        "time_error_seconds": 24,
    }
