"""Build simple, full-RCA UI problems from OpenRCA ground-truth records."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from runtime.nezha_dataset import NEZHA_CSV_FIELDS


OPENRCA_DATASETS = ("bank", "market", "telecom")
_RECORD_PATHS = {
    "bank": re.compile(r"(?:^|/)Bank/record\.csv$", re.IGNORECASE),
    "market": re.compile(r"(?:^|/)Market/cloudbed-[^/]+/record\.csv$", re.IGNORECASE),
    "telecom": re.compile(r"(?:^|/)Telecom/record\.csv$", re.IGNORECASE),
}
_MONTH_PATTERN = (
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December"
)
_QUERY_DATE_RE = re.compile(
    rf"({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})",
    re.IGNORECASE,
)
_QUERY_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")


def build_openrca_problems(source: str | Path, dataset: str) -> list[dict[str, str]]:
    """Create one full-RCA problem for every row of an OpenRCA record.csv."""
    dataset_name = _validate_dataset(dataset)
    source_path = Path(source).expanduser().resolve()
    if source_path.suffix.lower() == ".zip":
        return _build_from_zip(source_path, dataset_name)

    records = discover_openrca_records(source_path, dataset_name)
    groups = []
    for path in records:
        query_path = path.with_name("query.csv")
        if not query_path.is_file():
            raise ValueError(f"Missing OpenRCA query.csv next to {path}.")
        groups.append(
            (
                _system_name(path, dataset_name),
                str(path.parent.resolve()),
                list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline=""))),
                list(
                    csv.DictReader(
                        query_path.open("r", encoding="utf-8-sig", newline="")
                    )
                ),
            )
        )
    return _problems_from_records(groups, dataset_name)


def discover_openrca_records(source: str | Path, dataset: str) -> list[Path]:
    """Resolve a dataset directory to its one or more ground-truth record files."""
    dataset_name = _validate_dataset(dataset)
    candidate = Path(source).expanduser().resolve()
    if candidate.is_file() and candidate.name.lower() == "record.csv":
        matches = [candidate]
    else:
        matches = sorted(
            path
            for path in candidate.rglob("record.csv")
            if _RECORD_PATHS[dataset_name].search(path.as_posix())
            or _record_parent_matches(path, dataset_name)
        )
    expected = 2 if dataset_name == "market" else 1
    if len(matches) != expected:
        raise ValueError(
            f"Expected {expected} OpenRCA {dataset_name} record.csv file(s) under "
            f"{candidate}, found {len(matches)}."
        )
    return matches


def openrca_problems_to_csv(problems: Iterable[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=NEZHA_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(problems)
    return output.getvalue()


def _build_from_zip(path: Path, dataset: str) -> list[dict[str, str]]:
    pattern = _RECORD_PATHS[dataset]
    with zipfile.ZipFile(path) as archive:
        names = sorted(name for name in archive.namelist() if pattern.search(name))
        expected = 2 if dataset == "market" else 1
        if len(names) != expected:
            raise ValueError(
                f"Expected {expected} OpenRCA {dataset} record.csv file(s) in {path}, "
                f"found {len(names)}."
            )
        groups = []
        for name in names:
            query_name = str(Path(name).with_name("query.csv")).replace("\\", "/")
            if query_name not in archive.namelist():
                raise ValueError(f"Missing OpenRCA query.csv next to {name} in {path}.")
            rows = list(
                csv.DictReader(io.StringIO(archive.read(name).decode("utf-8-sig")))
            )
            queries = list(
                csv.DictReader(
                    io.StringIO(archive.read(query_name).decode("utf-8-sig"))
                )
            )
            groups.append((_system_name(Path(name), dataset), "", rows, queries))
    # A zip contains enough metadata for the catalogue, but execution needs extraction.
    return _problems_from_records(groups, dataset)


def _problems_from_records(
    groups: Iterable[
        tuple[str, str, list[dict[str, str]], list[dict[str, str]]]
    ],
    dataset: str,
) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    sequence = 0
    for system, source_path, rows, queries in groups:
        rows.sort(key=lambda row: int(float(row.get("timestamp") or 0)))
        official_windows = sorted({_parse_query_window(row) for row in queries})
        rows_by_window: dict[tuple[datetime, datetime], list[dict[str, str]]] = {
            window: [] for window in official_windows
        }
        for row in rows:
            occurred = _record_datetime(row, dataset)
            matching = [
                window
                for window in official_windows
                if window[0] <= occurred < window[1]
            ]
            if len(matching) != 1:
                raise ValueError(
                    f"OpenRCA {dataset} record at {occurred} matches "
                    f"{len(matching)} distinct query.csv windows; expected exactly one."
                )
            rows_by_window[matching[0]].append(row)

        for row in rows:
            missing = {
                name
                for name in ("timestamp", "datetime", "component", "reason")
                if not str(row.get(name) or "").strip()
            }
            if missing:
                raise ValueError(
                    f"Invalid OpenRCA {dataset} record row; missing {sorted(missing)}."
                )
            sequence += 1
            occurred = _record_datetime(row, dataset)
            official_start, official_end_exclusive = next(
                window
                for window in official_windows
                if window[0] <= occurred < window[1]
            )
            window_rows = rows_by_window[(official_start, official_end_exclusive)]
            incident_index = window_rows.index(row)
            start = official_start
            end = official_end_exclusive - timedelta(seconds=1)
            if incident_index > 0:
                previous = _record_datetime(window_rows[incident_index - 1], dataset)
                start = _midpoint(previous, occurred) + timedelta(seconds=1)
            if incident_index + 1 < len(window_rows):
                following = _record_datetime(window_rows[incident_index + 1], dataset)
                end = _midpoint(occurred, following)
            timestamp = str(int(float(row["timestamp"])))
            safe_system = re.sub(r"[^a-z0-9]+", "-", system.lower()).strip("-")
            problems.append(
                {
                    "problem_id": (
                        f"openrca-{dataset}-{sequence:03d}-{safe_system}-"
                        f"{occurred:%Y-%m-%d-%H%M%S}"
                    ),
                    "system": system,
                    "date": occurred.strftime("%Y-%m-%d"),
                    "start_time": start.strftime("%H:%M:%S"),
                    "end_time": end.strftime("%H:%M:%S"),
                    "timezone_offset_minutes": "480",
                    "source_path": source_path,
                    "expected_component": row["component"].strip(),
                    "expected_time": timestamp,
                    "expected_reason": row["reason"].strip(),
                }
            )
    return problems


def _record_datetime(row: dict[str, str], dataset: str) -> datetime:
    value = str(row.get("datetime") or "").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(
            f"Invalid OpenRCA {dataset} record datetime {value!r}."
        ) from exc


def _parse_query_window(row: dict[str, str]) -> tuple[datetime, datetime]:
    instruction = str(row.get("instruction") or "").strip()
    dates = _QUERY_DATE_RE.findall(instruction)
    times = _QUERY_TIME_RE.findall(instruction)
    if not dates or len(times) < 2:
        raise ValueError(
            "Invalid OpenRCA query.csv instruction; could not determine its "
            f"official date/time range: {instruction!r}"
        )

    def parse_date(parts: tuple[str, str, str]) -> datetime:
        month, day, year = parts
        return datetime.strptime(f"{month} {day} {year}", "%B %d %Y")

    start_date = parse_date(dates[0])
    end_date = parse_date(dates[1]) if len(dates) > 1 else start_date
    start = start_date.replace(hour=int(times[0][0]), minute=int(times[0][1]))
    end = end_date.replace(hour=int(times[1][0]), minute=int(times[1][1]))
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _midpoint(left: datetime, right: datetime) -> datetime:
    return left + timedelta(seconds=int((right - left).total_seconds() // 2))


def _record_parent_matches(path: Path, dataset: str) -> bool:
    parent = path.parent.name.lower()
    if dataset == "market":
        return parent.startswith("cloudbed-") and path.parent.parent.name.lower() == "market"
    return parent == dataset


def _system_name(path: Path, dataset: str) -> str:
    return path.parent.name if dataset == "market" else dataset


def _validate_dataset(dataset: str) -> str:
    normalized = str(dataset or "").strip().lower()
    if normalized not in OPENRCA_DATASETS:
        raise ValueError(
            f"Unknown OpenRCA dataset {dataset!r}; expected one of {OPENRCA_DATASETS}."
        )
    return normalized
