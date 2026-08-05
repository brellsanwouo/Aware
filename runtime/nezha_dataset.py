"""Build and score AWARE batch problems from the Nezha dataset."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


NEZHA_CSV_FIELDS = (
    "problem_id",
    "system",
    "date",
    "start_time",
    "end_time",
    "timezone_offset_minutes",
    "source_path",
    "expected_component",
    "expected_time",
    "expected_reason",
)


def discover_nezha_root(path: str | Path) -> Path:
    """Resolve an extracted Nezha directory to the repository containing rca_data."""
    candidate = Path(path).expanduser().resolve()
    if (candidate / "rca_data").is_dir():
        return candidate
    matches = sorted(item.parent for item in candidate.rglob("rca_data") if item.is_dir())
    if not matches:
        raise ValueError(f"No extracted Nezha rca_data directory found under: {candidate}")
    return matches[0]


def build_nezha_problems(source: str | Path) -> list[dict[str, str]]:
    """Create UI-compatible problems from extracted data or a Nezha zip archive."""
    source_path = Path(source).expanduser().resolve()
    if source_path.suffix.lower() == ".zip":
        return _build_from_zip(source_path)
    root = discover_nezha_root(source_path)
    labels = sorted((root / "rca_data").glob("*/*-fault_list.json"))
    return _problems_from_labels(
        ((path.name.removesuffix("-fault_list.json"), json.loads(path.read_text(encoding="utf-8"))) for path in labels),
        source_path=str(root),
    )


def problems_to_csv(problems: Iterable[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=NEZHA_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(problems)
    return output.getvalue()


def normalize_reason(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def score_diagnosis(
    diagnosis: dict[str, object],
    problem: dict[str, object],
    *,
    time_tolerance_seconds: int = 60,
) -> dict[str, object]:
    predicted_component = str(diagnosis.get("root_cause_component") or "").strip().lower()
    expected_component = str(problem.get("expected_component") or "").strip().lower()
    component_ok = bool(predicted_component and predicted_component == expected_component)
    reason_ok = normalize_reason(diagnosis.get("root_cause_reason")) == normalize_reason(
        problem.get("expected_reason")
    )
    try:
        predicted_time = int(float(str(diagnosis.get("root_cause_time"))))
        expected_time = int(float(str(problem.get("expected_time"))))
        time_error_seconds: int | None = abs(predicted_time - expected_time)
        time_ok = time_error_seconds <= int(time_tolerance_seconds)
    except (TypeError, ValueError):
        time_error_seconds = None
        time_ok = False
    return {
        "success": component_ok and reason_ok and time_ok,
        "component_ok": component_ok,
        "reason_ok": reason_ok,
        "time_ok": time_ok,
        "time_error_seconds": time_error_seconds,
    }


def _build_from_zip(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name for name in archive.namelist() if re.search(r"/rca_data/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}-fault_list\.json$", name)
        )
        labels = []
        for name in names:
            date_value = Path(name).name.removesuffix("-fault_list.json")
            labels.append((date_value, json.loads(archive.read(name).decode("utf-8"))))
    # A zip can construct the catalogue, but the agents need extracted telemetry to run it.
    return _problems_from_labels(labels, source_path="")


def _problems_from_labels(
    labels: Iterable[tuple[str, object]],
    *,
    source_path: str,
) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    sequence = 0
    for date_value, payload in labels:
        if not isinstance(payload, dict):
            continue
        system = "online-boutique" if date_value.startswith("2022-") else "train-ticket"
        incidents = [item for hour in sorted(payload) for item in payload.get(hour, [])]
        incidents.sort(key=lambda item: int(item.get("inject_timestamp", 0)))
        for incident in incidents:
            if not isinstance(incident, dict):
                continue
            sequence += 1
            injected = datetime.strptime(str(incident["inject_time"]), "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            start = injected.replace(second=0)
            end = start + timedelta(minutes=2, seconds=59)
            problem_id = f"nezha-{sequence:03d}-{date_value}-{injected:%H%M%S}"
            problems.append(
                {
                    "problem_id": problem_id,
                    "system": system,
                    "date": date_value,
                    "start_time": start.strftime("%H:%M:%S"),
                    "end_time": end.strftime("%H:%M:%S"),
                    "timezone_offset_minutes": "0",
                    "source_path": source_path,
                    "expected_component": str(incident["inject_pod"]),
                    "expected_time": str(incident["inject_timestamp"]),
                    "expected_reason": str(incident["inject_type"]),
                }
            )
    return problems
