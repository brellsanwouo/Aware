"""Shared file/data manipulation tools for Assess telemetry analysis."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass
class CsvWindowView:
    """CSV rows constrained to the failure time window."""

    fieldnames: list[str]
    rows: list[dict[str, str]]
    lines: list[str]
    total_rows: int
    window_rows: int
    timestamp_field: str | None


@dataclass
class ComponentFocusResult:
    """Result of applying component focus over a window view."""

    view: CsvWindowView
    component: str
    matched_columns: list[str]


def load_csv_window(file_path: Path, start_ts: int, end_ts: int) -> CsvWindowView:
    """Read CSV and keep only rows inside [start_ts, end_ts], strictly by timestamp."""
    resolved = file_path.resolve()
    stat = resolved.stat()
    return _load_csv_window_cached(
        str(resolved),
        int(start_ts),
        int(end_ts),
        stat.st_mtime_ns,
        stat.st_size,
    )


@lru_cache(maxsize=32)
def _load_csv_window_cached(
    file_path_value: str,
    start_ts: int,
    end_ts: int,
    _mtime_ns: int,
    _size: int,
) -> CsvWindowView:
    """Cache strict slices so focused agents do not rescan multi-gigabyte files."""
    file_path = Path(file_path_value)
    fieldnames: list[str] = []
    rows: list[dict[str, str]] = []
    total_rows = 0
    window_rows = 0

    with file_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        timestamp_field = find_timestamp_field(fieldnames)
        last_ts: int | None = None
        ordered_prefix = True
        sampled_timestamps = 0
        for row in reader:
            total_rows += 1
            if timestamp_field is None:
                # Strict policy: if timestamp column does not exist, no row is considered in-window.
                continue
            row_ts = parse_timestamp_seconds(row.get(timestamp_field, ""))
            if row_ts is None:
                continue
            sampled_timestamps += 1
            if last_ts is not None and row_ts < last_ts:
                ordered_prefix = False
            last_ts = row_ts
            # Large OpenRCA files are timestamp ordered. Once a sufficiently
            # long prefix confirms that ordering, stop after the requested
            # window instead of rescanning gigabytes of later telemetry.
            if row_ts > end_ts and ordered_prefix and sampled_timestamps >= 256:
                break
            if row_ts < start_ts or row_ts > end_ts:
                continue
            window_rows += 1
            rows.append({k: (v or "") for k, v in row.items()})

    if not fieldnames:
        return CsvWindowView(
            fieldnames=[],
            rows=[],
            lines=[],
            total_rows=0,
            window_rows=0,
            timestamp_field=None,
        )

    lines = [",".join(fieldnames)]
    for row in rows:
        lines.append(",".join((row.get(col, "") or "").replace("\n", " ").strip() for col in fieldnames))

    return CsvWindowView(
        fieldnames=fieldnames,
        rows=rows,
        lines=lines,
        total_rows=total_rows,
        window_rows=window_rows,
        timestamp_field=find_timestamp_field(fieldnames),
    )


def apply_component_focus(
    view: CsvWindowView,
    *,
    domain: str,
    component: str,
) -> ComponentFocusResult:
    """Filter in-window rows to a component value using semantic component columns."""
    focus = str(component or "").strip()
    if not focus:
        return ComponentFocusResult(
            view=view,
            component=focus,
            matched_columns=[],
        )

    semantic = detect_semantic_columns(view.fieldnames, domain=domain)
    component_columns = semantic.get("component_columns", [])
    if not component_columns:
        return ComponentFocusResult(
            view=CsvWindowView(
                fieldnames=view.fieldnames,
                rows=[],
                lines=[",".join(view.fieldnames)] if view.fieldnames else [],
                total_rows=view.total_rows,
                window_rows=0,
                timestamp_field=view.timestamp_field,
            ),
            component=focus,
            matched_columns=[],
        )

    lowered_focus = focus.lower()
    filtered_rows: list[dict[str, str]] = []
    for row in view.rows:
        matched = False
        for column in component_columns:
            value = (row.get(column, "") or "").strip()
            if not value:
                continue
            lowered = value.lower()
            if lowered == lowered_focus or lowered_focus in lowered or lowered in lowered_focus:
                matched = True
                break
        if matched:
            filtered_rows.append(row)

    lines = [",".join(view.fieldnames)] if view.fieldnames else []
    for row in filtered_rows:
        lines.append(",".join((row.get(col, "") or "").replace("\n", " ").strip() for col in view.fieldnames))

    filtered = CsvWindowView(
        fieldnames=view.fieldnames,
        rows=filtered_rows,
        lines=lines,
        total_rows=view.total_rows,
        window_rows=len(filtered_rows),
        timestamp_field=view.timestamp_field,
    )
    return ComponentFocusResult(
        view=filtered,
        component=focus,
        matched_columns=component_columns,
    )


def find_timestamp_field(fieldnames: list[str]) -> str | None:
    """Locate best timestamp-like column in headers."""
    # Prefer an explicit epoch/ISO timestamp over generic display columns such as
    # Nezha's Go-formatted `Time` value (which also contains a monotonic suffix).
    priority = (
        "timestamp",
        "time_stamp",
        "ts",
        "event_time",
        "start_time",
        "starttime",
        "start_time_unix_nano",
        "starttimeunixnano",
        "time_unix_nano",
        "timeunixnano",
        "datetime",
        "time",
        "end_time",
        "end_time_unix_nano",
        "endtimeunixnano",
    )
    normalized = {normalize_header(field): field for field in fieldnames}
    for target in priority:
        if target in normalized:
            return normalized[target]
    return None


def normalize_header(value: str) -> str:
    """Normalize header for robust matching."""
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def parse_timestamp_seconds(raw: str) -> int | None:
    """Parse timestamp values in seconds, milliseconds, microseconds, nanoseconds, or ISO datetime."""
    value = str(raw or "").strip()
    if not value:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        try:
            numeric = float(value)
        except ValueError:
            return None
        magnitude = abs(numeric)
        if magnitude >= 100_000_000_000_000_000:
            return int(numeric // 1_000_000_000)
        if magnitude >= 100_000_000_000_000:
            return int(numeric // 1_000_000)
        if magnitude >= 100_000_000_000:
            return int(numeric // 1000)
        return int(numeric)
    iso = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def window_detail(view: CsvWindowView, *, start_ts: int, end_ts: int) -> str:
    """Human-readable detail for diagnostics and logs."""
    return (
        "Analyzed strict time-window slice: "
        f"window=[{start_ts},{end_ts}], "
        f"rows_in_window={view.window_rows}, total_rows={view.total_rows}, "
        f"timestamp_field={view.timestamp_field or 'not_found'}."
    )


def build_llm_observation_context(
    view: CsvWindowView,
    *,
    domain: str,
    start_ts: int,
    end_ts: int,
    sample_limit: int = 12,
) -> dict[str, Any]:
    """Build compact tool output context for LLM-based decision making."""
    sample_lines = view.lines[:sample_limit] if view.lines else []
    semantic_columns = detect_semantic_columns(view.fieldnames, domain=domain)
    semantic_summary = summarize_semantic_window(
        rows=view.rows,
        semantic_columns=semantic_columns,
        timestamp_field=view.timestamp_field,
    )
    context = {
        "domain": domain,
        "window": {"start": start_ts, "end": end_ts},
        "timestamp_field": view.timestamp_field,
        "total_rows": view.total_rows,
        "rows_in_window": view.window_rows,
        "header_line": view.lines[0] if view.lines else "",
        "fieldnames": view.fieldnames,
        "semantic_columns": semantic_columns,
        "semantic_summary": semantic_summary,
        "sample_lines": sample_lines,
    }
    if domain == "trace":
        context["inter_service_network_latency"] = summarize_inter_service_network_latency(
            view.rows
        )
    elif domain == "logs":
        context["log_failure_signals"] = summarize_log_failure_signals(
            view.rows,
            semantic_columns=semantic_columns,
            timestamp_field=view.timestamp_field,
        )
    elif domain == "metrics":
        context["long_form_kpi_anomalies"] = summarize_long_form_kpi_anomalies(
            view.rows,
            semantic_columns=semantic_columns,
            timestamp_field=view.timestamp_field,
        )
        context["service_latency_anomalies"] = summarize_service_latency_anomalies(
            view.rows,
            semantic_columns=semantic_columns,
            timestamp_field=view.timestamp_field,
        )
    return context


def summarize_service_latency_anomalies(
    rows: list[dict[str, str]],
    *,
    semantic_columns: dict[str, list[str]],
    timestamp_field: str | None,
) -> list[dict[str, Any]]:
    """Find the earliest service MRT surge before propagated caller latency."""
    component_columns = semantic_columns.get("component_columns", [])
    duration_columns = semantic_columns.get("duration_columns", [])
    component_column = next(
        (column for column in component_columns if normalize_header(column) == "service"),
        None,
    )
    duration_column = next(
        (column for column in duration_columns if normalize_header(column) in {"mrt", "latency", "duration"}),
        None,
    )
    if not component_column or not duration_column or not timestamp_field:
        return []
    grouped: dict[str, list[tuple[int, float]]] = {}
    for row in rows:
        component = (row.get(component_column, "") or "").strip()
        ts = parse_timestamp_seconds(row.get(timestamp_field, ""))
        try:
            value = float(row.get(duration_column, ""))
        except (TypeError, ValueError):
            continue
        if component and ts is not None:
            grouped.setdefault(component, []).append((ts, value))
    anomalies: list[dict[str, Any]] = []
    for component, samples in grouped.items():
        ordered = sorted(samples)
        values = sorted(value for _, value in ordered)
        if len(values) < 5:
            continue
        baseline = values[(len(values) - 1) // 2]
        threshold = max(baseline * 5.0, baseline + 50.0)
        high = [(ts, value) for ts, value in ordered if value >= threshold]
        if not high:
            continue
        detected_ts, _ = high[0]
        preceding_times = [ts for ts, _ in ordered if ts < detected_ts]
        onset_ts = max(preceding_times) if preceding_times else detected_ts
        peak_ts, peak = max(high, key=lambda item: item[1])
        canonical = re.sub(r"-(?:grpc|http)$", "", component, flags=re.IGNORECASE)
        anomalies.append(
            {
                "component": canonical,
                "reason": "container network latency",
                "baseline": baseline,
                "peak": peak,
                "onset_ts": onset_ts,
                "peak_ts": peak_ts,
                "detected_ts": detected_ts,
                "support_count": len(high),
                "amplification": peak / max(abs(baseline), 0.001),
            }
        )
    if not anomalies:
        return []
    earliest = min(int(item["detected_ts"]) for item in anomalies)
    earliest_candidates = [
        item for item in anomalies if int(item["detected_ts"]) == earliest
    ]
    strongest = max(float(item["amplification"]) for item in earliest_candidates)
    return [
        item
        for item in earliest_candidates
        if float(item["amplification"]) == strongest
    ]


def summarize_long_form_kpi_anomalies(
    rows: list[dict[str, str]],
    *,
    semantic_columns: dict[str, list[str]],
    timestamp_field: str | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Extract explicit component/KPI/value anomalies from long-form metrics."""
    component_columns = semantic_columns.get("component_columns", [])
    reason_columns = semantic_columns.get("reason_columns", [])
    numeric_columns = semantic_columns.get("numeric_candidate_columns", [])
    kpi_column = next(
        (
            column
            for column in reason_columns
            if normalize_header(column) in {"kpi_name", "metric_name", "name"}
        ),
        None,
    )
    value_column = next(
        (column for column in numeric_columns if normalize_header(column) == "value"),
        None,
    )
    if not component_columns or not kpi_column or not value_column:
        return []
    grouped: dict[tuple[str, str, str], list[tuple[float, int | None]]] = {}
    for row in rows:
        component = next(
            ((row.get(column, "") or "").strip() for column in component_columns if (row.get(column, "") or "").strip()),
            "",
        )
        kpi = (row.get(kpi_column, "") or "").strip()
        try:
            value = float(row.get(value_column, ""))
        except (TypeError, ValueError):
            continue
        lowered = kpi.lower()
        percent_like = any(token in lowered for token in ("perc", "percent", "util", "rate", "capacity"))
        reason = ""
        if percent_like and any(token in lowered for token in ("memory", "mem")):
            reason = "high memory usage"
        elif component.lower().startswith("docker_") and "container_cpu_used" in lowered:
            reason = "CPU fault"
        elif (
            percent_like
            and "cpu" in lowered
            and "idle" not in lowered
            and "singlecpu" not in lowered
        ):
            reason = "high CPU usage"
        elif percent_like and any(token in lowered for token in ("disk", "filesystem", "fscapacity")):
            reason = "high disk space usage"
        if not component or not reason:
            continue
        grouped.setdefault((component, reason, kpi), []).append(
            (
                value,
                parse_timestamp_seconds(row.get(timestamp_field, "")) if timestamp_field else None,
            )
        )
    candidates: list[dict[str, Any]] = []
    for (component, reason, kpi), samples in grouped.items():
        values = [item[0] for item in samples]
        minimum = min(values)
        maximum = max(values)
        spread = maximum - minimum
        threshold = 95.0 if reason == "high CPU usage" else 90.0
        minimum_spread = 10.0 if reason == "high CPU usage" else 5.0
        high_count = sum(value >= threshold for value in values)
        if maximum < threshold or spread < minimum_spread:
            continue
        if reason == "CPU fault" and high_count < 3:
            continue
        peak_times = sorted(ts for value, ts in samples if value == maximum and ts is not None)
        # Flat anomaly plateaus have no unique mathematical peak. Their lower
        # median is a more useful occurrence estimate than the window boundary.
        peak_ts = peak_times[(len(peak_times) - 1) // 2] if peak_times else None
        if reason == "CPU fault":
            ordered_samples = sorted(
                ((ts, value) for value, ts in samples if ts is not None),
                key=lambda item: item[0],
            )
            first_high = next((ts for ts, value in ordered_samples if value >= threshold), None)
            preceding = [ts for ts, value in ordered_samples if first_high is not None and ts < first_high and value < threshold]
            if preceding:
                timestamps = [ts for ts, _ in ordered_samples]
                cadences = sorted(
                    right - left
                    for left, right in zip(timestamps, timestamps[1:])
                    if 0 < right - left <= 300
                )
                cadence = cadences[(len(cadences) - 1) // 2] if cadences else 0
                peak_ts = max(preceding) - cadence
        candidates.append(
            {
                "component": component,
                "reason": reason,
                "kpi_name": kpi,
                "value": maximum,
                "minimum": minimum,
                "range": spread,
                "support_count": high_count,
                "timestamp": peak_ts,
            }
        )
    candidates.sort(
        key=lambda item: (int(item["support_count"]), float(item["range"])),
        reverse=True,
    )
    balanced: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for item in candidates:
        reason = str(item["reason"])
        if reason_counts[reason] >= 3:
            continue
        balanced.append(item)
        reason_counts[reason] += 1
    balanced.sort(
        key=lambda item: (
            int(item["support_count"]),
            float(item["range"]),
            float(item["value"]),
        ),
        reverse=True,
    )
    return balanced[:limit]


def summarize_log_failure_signals(
    rows: list[dict[str, str]],
    *,
    semantic_columns: dict[str, list[str]],
    timestamp_field: str | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Aggregate repeated failure mechanics instead of sampling a few log rows.

    The categories deliberately describe observable mechanics. They are useful
    for Nezha's ``return``/``exception`` injections, but do not depend on an
    incident label or ground-truth file.
    """
    component_columns = semantic_columns.get("component_columns", [])
    text_columns = [
        name
        for name in (rows[0].keys() if rows else [])
        if any(
            token in normalize_header(name)
            for token in ("log", "message", "value", "error", "exception", "reason")
        )
    ]
    patterns = (
        (
            "early_return",
            re.compile(
                r"not specified|missing required|required .{0,40} missing|"
                r"empty response|return(?:ed)? (?:nil|null|empty)|invalid argument|"
                r"could not .{0,100} error",
                flags=re.IGNORECASE,
            ),
        ),
        (
            "exception",
            re.compile(
                r"\bexception\b|\bpanic\b|\bfatal\b|\bfailed to\b|"
                r"\bfailure\b|stack ?trace",
                flags=re.IGNORECASE,
            ),
        ),
    )
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        text = " ".join((row.get(column, "") or "") for column in text_columns)
        if not text:
            continue
        component = next(
            (
                (row.get(column, "") or "").strip()
                for column in component_columns
                if (row.get(column, "") or "").strip()
            ),
            "",
        )
        if not component:
            continue
        for category, pattern in patterns:
            if category == "exception" and re.search(
                r"allocation failure|failed to retrieve", text, re.IGNORECASE
            ):
                continue
            if not pattern.search(text):
                continue
            key = (category, component)
            item = aggregated.setdefault(
                key,
                {
                    "category": category,
                    "component": component,
                    "count": 0,
                    "first_ts": None,
                    "sample": "",
                    "propagated": False,
                },
            )
            item["count"] += 1
            row_ts = parse_timestamp_seconds(row.get(timestamp_field, "")) if timestamp_field else None
            if row_ts is not None and (item["first_ts"] is None or row_ts < item["first_ts"]):
                item["first_ts"] = row_ts
            if not item["sample"]:
                match = pattern.search(text)
                start = max(0, (match.start() if match else 0) - 80)
                item["sample"] = text[start : start + 240].replace("\n", " ")
            if re.search(
                r"failed to complete (?:the )?(?:order|request)|upstream|downstream",
                text,
                flags=re.IGNORECASE,
            ):
                item["propagated"] = True
            break
    return sorted(
        aggregated.values(),
        key=lambda item: int(item["count"]),
        reverse=True,
    )[:limit]


def summarize_inter_service_network_latency(
    rows: list[dict[str, str]],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Estimate cross-service latency from parent/child span completion times.

    Nezha represents network delay as the gap between a child span completion and
    its parent completion when both spans belong to different pods. The result is
    useful beyond Nezha for OTLP-like flattened trace exports.
    """
    spans: dict[str, dict[str, str]] = {}
    for row in rows:
        span_id = (row.get("SpanID", "") or row.get("span_id", "")).strip()
        if span_id:
            spans[span_id] = row

    by_component: dict[str, list[tuple[float, int]]] = {}
    for row in rows:
        parent_id = (row.get("ParentID", "") or row.get("parent_id", "")).strip()
        parent = spans.get(parent_id)
        if parent is None:
            continue
        component = (row.get("PodName", "") or row.get("pod_name", "")).strip()
        parent_component = (
            parent.get("PodName", "") or parent.get("pod_name", "")
        ).strip()
        if not component or not parent_component or component == parent_component:
            continue
        try:
            child_end = int(float(row.get("EndTimeUnixNano", "")))
            parent_end = int(float(parent.get("EndTimeUnixNano", "")))
        except (TypeError, ValueError):
            continue
        latency_ms = (parent_end - child_end) / 1_000_000
        if latency_ms < 0:
            continue
        ts = parse_timestamp_seconds(str(child_end)) or 0
        by_component.setdefault(component, []).append((latency_ms, ts))

    summaries: list[dict[str, Any]] = []
    for component, values in by_component.items():
        ordered = sorted(value for value, _ in values)
        index = max(0, int(0.9 * len(ordered)) - 1)
        p90 = ordered[index]
        peak, peak_ts = max(values, key=lambda item: item[0])
        summaries.append(
            {
                "component": component,
                "p90_ms": round(p90, 3),
                "max_ms": round(peak, 3),
                "peak_ts": peak_ts,
                "samples": len(values),
            }
        )
    summaries.sort(key=lambda item: float(item["p90_ms"]), reverse=True)
    return summaries[:limit]


def detect_semantic_columns(fieldnames: list[str], *, domain: str) -> dict[str, list[str]]:
    """Infer semantic column roles from CSV header names."""
    by_norm = {name: normalize_header(name) for name in fieldnames}

    def match(tokens: tuple[str, ...]) -> list[str]:
        selected: list[str] = []
        for original, norm in by_norm.items():
            if any(norm == token or token in norm for token in tokens):
                selected.append(original)
        return selected

    component = match(
        ("cmdb_id", "component", "service", "host", "node", "instance", "tc", "pod", "pod_name", "podname")
    )
    # Substring matching must not turn metric names such as
    # NodeCpuUsageRate or PodClientLatency into component identifiers.
    component = [
        name for name in component if not _looks_numeric_column(by_norm[name])
    ]
    reason = match(("value", "message", "msg", "error", "exception", "reason", "log_name", "kpi_name", "status"))
    duration = match(("duration", "duration_ms", "latency", "mrt", "response_time", "p95", "p99"))
    error_rate = match(("error_rate", "errors_rate", "5xx_rate", "5xx", "sr", "rr"))

    # Domain-specific light bias.
    if domain == "trace":
        component = _prepend_unique(component, match(("cmdb_id", "service", "component")))
        duration = _prepend_unique(duration, match(("duration", "latency")))
    elif domain == "logs":
        preferred_components = match(
            ("pod_name", "podname", "cmdb_id", "service_name", "servicename", "service", "component")
        )
        preferred_components = [
            name for name in preferred_components if not _looks_numeric_column(by_norm[name])
        ]
        component = _prepend_unique(component, preferred_components)
        reason = _prepend_unique(reason, match(("log_name", "value", "message", "error", "exception")))
    elif domain == "metrics":
        preferred_components = match(("tc", "cmdb_id", "service", "host", "pod", "pod_name", "podname"))
        preferred_components = [
            name for name in preferred_components if not _looks_numeric_column(by_norm[name])
        ]
        component = _prepend_unique(component, preferred_components)
        metric_name_columns = [
            original
            for original, norm in by_norm.items()
            if norm in {"name", "kpi_name", "metric_name"}
        ]
        reason = _prepend_unique(reason, metric_name_columns)
        duration = _prepend_unique(duration, match(("mrt", "latency", "response_time", "duration")))

    numeric_candidates = [
        name for name in fieldnames if _looks_numeric_column(normalize_header(name))
    ]
    numeric_candidates.sort(
        key=lambda name: (
            0 if "cpu" in normalize_header(name) else 1,
            1 if "node" in normalize_header(name) else 0,
            fieldnames.index(name),
        )
    )
    return {
        "component_columns": component[:6],
        "reason_columns": reason[:8],
        "duration_columns": duration[:6],
        "error_rate_columns": error_rate[:6],
        "numeric_candidate_columns": numeric_candidates[:10],
    }


def summarize_semantic_window(
    *,
    rows: list[dict[str, str]],
    semantic_columns: dict[str, list[str]],
    timestamp_field: str | None,
) -> dict[str, Any]:
    """Summarize semantic signals from all in-window rows."""
    components = _top_values(rows, semantic_columns.get("component_columns", []), limit=6)
    reasons = _top_values(rows, semantic_columns.get("reason_columns", []), limit=6)
    numeric_ranges = _numeric_ranges(rows, semantic_columns.get("numeric_candidate_columns", []), limit=8)
    min_ts, max_ts = _window_observed_bounds(rows, timestamp_field)
    return {
        "window_observed_time_bounds": {
            "min_ts": min_ts,
            "max_ts": max_ts,
        },
        "top_component_values": components,
        "top_reason_values": reasons,
        "numeric_ranges": numeric_ranges,
    }


def _prepend_unique(base: list[str], preferred: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in preferred + base:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _looks_numeric_column(norm: str) -> bool:
    numeric_tokens = (
        "latency",
        "duration",
        "mrt",
        "p95",
        "p99",
        "cpu",
        "memory",
        "network",
        "bytes",
        "workload",
        "syscall",
        "error",
        "rate",
        "rr",
        "sr",
        "cnt",
        "qps",
        "rps",
        "value",
    )
    return any(token == norm or token in norm for token in numeric_tokens)


def _top_values(rows: list[dict[str, str]], columns: list[str], *, limit: int) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    for row in rows:
        for column in columns:
            raw = (row.get(column, "") or "").strip()
            if not raw:
                continue
            counter[(column, raw[:140])] += 1
    results: list[dict[str, Any]] = []
    for (column, value), count in counter.most_common(limit):
        results.append({"column": column, "value": value, "count": count})
    return results


def _numeric_ranges(rows: list[dict[str, str]], columns: list[str], *, limit: int) -> dict[str, dict[str, float | int]]:
    out: dict[str, dict[str, float | int]] = {}
    for column in columns:
        values: list[float] = []
        for row in rows:
            raw = (row.get(column, "") or "").strip()
            if not raw:
                continue
            try:
                values.append(float(raw))
            except ValueError:
                continue
        if not values:
            continue
        out[column] = {
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "count": len(values),
        }
        if len(out) >= limit:
            break
    return out


def _window_observed_bounds(rows: list[dict[str, str]], timestamp_field: str | None) -> tuple[int | None, int | None]:
    if not timestamp_field:
        return None, None
    values: list[int] = []
    for row in rows:
        ts = parse_timestamp_seconds(row.get(timestamp_field, ""))
        if ts is not None:
            values.append(ts)
    if not values:
        return None, None
    return min(values), max(values)


def count_matches(text: str, pattern: str) -> int:
    """Count regex matches case-insensitively."""
    return len(re.findall(pattern, text, flags=re.IGNORECASE))


def sample_matching_lines(lines: list[str], pattern: str, limit: int = 3) -> list[str]:
    """Get short evidence snippets matching a pattern."""
    regex = re.compile(pattern, flags=re.IGNORECASE)
    matches: list[str] = []
    for line in lines:
        if regex.search(line):
            matches.append(line.strip()[:300])
            if len(matches) >= limit:
                break
    return matches


def extract_trace_durations(rows: list[dict[str, str]]) -> list[int]:
    """Extract trace durations normalized to milliseconds."""
    durations: list[int] = []
    for row in rows:
        try:
            start_ns = int(float(row.get("StartTimeUnixNano", "")))
            end_ns = int(float(row.get("EndTimeUnixNano", "")))
        except (TypeError, ValueError):
            start_ns = end_ns = 0
        if start_ns and end_ns >= start_ns:
            durations.append(int((end_ns - start_ns) / 1_000_000))
            continue
        for key, value in row.items():
            if normalize_header(key) not in {"duration", "latency", "duration_ms", "mrt"}:
                continue
            try:
                durations.append(int(float(str(value).strip())))
            except ValueError:
                continue
    return durations


def max_numeric_column(rows: list[dict[str, str]], column_names: tuple[str, ...]) -> float | None:
    """Return max numeric value among candidate columns."""
    targets = {item.lower() for item in column_names}
    values: list[float] = []
    for row in rows:
        for key, value in row.items():
            if normalize_header(key) not in targets:
                continue
            try:
                values.append(float(str(value).strip()))
            except ValueError:
                continue
    return max(values) if values else None


def min_numeric_column(rows: list[dict[str, str]], column_names: tuple[str, ...]) -> float | None:
    """Return min numeric value among candidate columns."""
    targets = {item.lower() for item in column_names}
    values: list[float] = []
    for row in rows:
        for key, value in row.items():
            if normalize_header(key) not in targets:
                continue
            try:
                values.append(float(str(value).strip()))
            except ValueError:
                continue
    return min(values) if values else None


def max_value_after_keywords(text: str, keywords: tuple[str, ...]) -> float | None:
    """Fallback extraction for non-structured lines (keyword then number)."""
    values: list[float] = []
    for keyword in keywords:
        pattern = rf"{re.escape(keyword)}[^\d-]{{0,16}}(-?\d+(?:\.\d+)?)"
        for item in re.findall(pattern, text, flags=re.IGNORECASE):
            try:
                values.append(float(item))
            except ValueError:
                continue
    return max(values) if values else None
