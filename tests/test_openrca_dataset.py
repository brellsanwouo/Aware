"""Tests for simple OpenRCA Bank, Market, and Telecom catalogues."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime

from runtime.openrca_dataset import build_openrca_problems, openrca_problems_to_csv


def _write_record(path, rows) -> None:
    path.parent.mkdir(parents=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["level", "component", "timestamp", "datetime", "reason"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_queries(path, windows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["task_index", "instruction", "scoring_points"],
        )
        writer.writeheader()
        for start, end in windows:
            writer.writerow(
                {
                    "task_index": "task_7",
                    "instruction": (
                        f"On {start:%B %-d, %Y}, within the time range of "
                        f"{start:%H:%M} to {end:%H:%M}, one failure occurred."
                    ),
                    "scoring_points": "full RCA",
                }
            )


def _row(component, timestamp, datetime_value, reason):
    return {
        "level": "pod",
        "component": component,
        "timestamp": timestamp,
        "datetime": datetime_value,
        "reason": reason,
    }


def test_builds_simple_bank_incidents_in_utc_plus_8(tmp_path) -> None:
    bank = tmp_path / "Bank-003" / "Bank"
    _write_record(
        bank / "record.csv",
        [
            _row("Redis02", "1614852540.0", "2021-03-04 18:09:00", "high memory usage"),
            _row("Mysql02", "1614841020.0", "2021-03-04 14:57:00", "high memory usage"),
        ],
    )
    _write_queries(
        bank / "query.csv",
        [
            (datetime(2021, 3, 4, 14, 30), datetime(2021, 3, 4, 15, 0)),
            (datetime(2021, 3, 4, 18, 0), datetime(2021, 3, 4, 18, 30)),
        ],
    )

    problems = build_openrca_problems(tmp_path, "bank")

    assert len(problems) == 2
    assert problems[0]["problem_id"].startswith("openrca-bank-001-bank-")
    assert problems[0]["start_time"] == "14:30:00"
    assert problems[0]["end_time"] == "14:59:59"
    assert problems[0]["timezone_offset_minutes"] == "480"
    assert problems[0]["expected_time"] == "1614841020"
    assert problems[0]["source_path"] == str(bank.resolve())


def test_market_catalogue_keeps_each_cloudbed_source_separate(tmp_path) -> None:
    market = tmp_path / "Market-002" / "Market"
    _write_record(
        market / "cloudbed-1" / "record.csv",
        [_row("shippingservice-1", "1647738546", "2022-03-20 09:09:06", "container read I/O load")],
    )
    _write_queries(
        market / "cloudbed-1" / "query.csv",
        [(datetime(2022, 3, 20, 9, 0), datetime(2022, 3, 20, 9, 30))],
    )
    _write_record(
        market / "cloudbed-2" / "record.csv",
        [_row("frontend-0", "1647746749", "2022-03-20 11:25:49", "container CPU load")],
    )
    _write_queries(
        market / "cloudbed-2" / "query.csv",
        [(datetime(2022, 3, 20, 11, 0), datetime(2022, 3, 20, 11, 30))],
    )

    rows = list(
        csv.DictReader(
            io.StringIO(
                openrca_problems_to_csv(build_openrca_problems(tmp_path, "market"))
            )
        )
    )

    assert [row["system"] for row in rows] == ["cloudbed-1", "cloudbed-2"]
    assert rows[0]["source_path"].endswith("Market/cloudbed-1")
    assert rows[1]["source_path"].endswith("Market/cloudbed-2")


def test_catalogue_splits_multiple_incidents_inside_same_half_hour(tmp_path) -> None:
    bank = tmp_path / "Bank-003" / "Bank"
    _write_record(
        bank / "record.csv",
        [
            _row("MG01", "1614789240", "2021-03-04 00:34:00", "high memory usage"),
            _row("MG02", "1614790080", "2021-03-04 00:48:00", "high CPU usage"),
        ],
    )
    _write_queries(
        bank / "query.csv",
        [(datetime(2021, 3, 4, 0, 30), datetime(2021, 3, 4, 1, 0))],
    )

    problems = build_openrca_problems(tmp_path, "bank")

    assert problems[0]["end_time"] < problems[1]["start_time"]


def test_catalogue_uses_official_query_window_instead_of_deriving_one(tmp_path) -> None:
    bank = tmp_path / "Bank-003" / "Bank"
    _write_record(
        bank / "record.csv",
        [_row("MG01", "1614841020", "2021-03-04 14:57:00", "high memory usage")],
    )
    _write_queries(
        bank / "query.csv",
        [(datetime(2021, 3, 4, 14, 25), datetime(2021, 3, 4, 15, 5))],
    )

    problem = build_openrca_problems(tmp_path, "bank")[0]

    assert problem["start_time"] == "14:25:00"
    assert problem["end_time"] == "15:04:59"


def test_builds_telecom_catalogue_metadata_from_zip(tmp_path) -> None:
    archive_path = tmp_path / "Telecom-001.zip"
    csv_text = (
        "level,reason,component,timestamp,datetime\n"
        "pod,CPU fault,docker_003,1586534700,2020-04-11 00:05:00\n"
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Telecom/record.csv", csv_text)
        archive.writestr(
            "Telecom/query.csv",
            "task_index,instruction,scoring_points\n"
            'task_7,"On April 11, 2020, from 00:00 to 00:30, one failure occurred.",full RCA\n',
        )

    problems = build_openrca_problems(archive_path, "telecom")

    assert len(problems) == 1
    assert problems[0]["expected_component"] == "docker_003"
    assert problems[0]["expected_reason"] == "CPU fault"
    assert problems[0]["source_path"] == ""
