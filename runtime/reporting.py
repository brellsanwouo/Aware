"""Assess reporting helpers (explicit findings + synthesis + final reporting)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Iterable

from aware_models.buildspec import BuildSpec
from aware_models.executor import AgentTaskResult, AssessFinding, ExecutorRunResult
from tools import telemetry_tools

_UNIX_MIN = 946684800  # 2000-01-01
_UNIX_MAX = 4102444800  # 2100-01-01
_COMPONENT_TOKEN_RE = re.compile(
    r"\b(?:MG\d{2}|IG\d{2}|Tomcat\d+|Mysql\d+|ServiceTest\d+|docker[A-Za-z0-9_-]+|[A-Za-z]{2,}\d{1,4})\b"
)
_TS_RE = re.compile(r"\b\d{10,13}\b")

_TASK_SCOPE_MAP: dict[str, tuple[str, ...]] = {
    "task_1": ("root_cause_time",),
    "task_2": ("root_cause_reason",),
    "task_3": ("root_cause_component",),
    "task_4": ("root_cause_time", "root_cause_reason"),
    "task_5": ("root_cause_time", "root_cause_component"),
    "task_6": ("root_cause_component", "root_cause_reason"),
    "task_7": ("root_cause_time", "root_cause_component", "root_cause_reason"),
}
_FINAL_KEY_BY_FIELD: dict[str, str] = {
    "root_cause_time": "root cause timestamp",
    "root_cause_component": "root cause component",
    "root_cause_reason": "root cause reason",
}


def _confidence_label(value: float | int | None) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "low"
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _iter_texts(finding: AssessFinding) -> Iterable[str]:
    yield finding.summary
    for item in finding.evidence:
        yield item


def _extract_unix_ts(text: str) -> list[int]:
    out: list[int] = []
    for match in _TS_RE.findall(text or ""):
        try:
            raw = int(match)
        except ValueError:
            continue
        ts = raw // 1000 if raw >= 1_000_000_000_000 else raw
        if _UNIX_MIN <= ts <= _UNIX_MAX:
            out.append(ts)
    return out


def _unknown_fields_from_buildspec(buildspec: BuildSpec) -> tuple[str, ...]:
    fields: list[str] = []
    if buildspec.uncertainty.root_cause_time == "unknown":
        fields.append("root_cause_time")
    if buildspec.uncertainty.root_cause_component == "unknown":
        fields.append("root_cause_component")
    if buildspec.uncertainty.root_cause_reason == "unknown":
        fields.append("root_cause_reason")
    return tuple(fields)


def _requested_fields(buildspec: BuildSpec) -> tuple[str, ...]:
    unknown = _unknown_fields_from_buildspec(buildspec)
    if unknown:
        return unknown
    return _TASK_SCOPE_MAP.get(buildspec.task_type, ())


def _as_view(finding: AssessFinding) -> dict[str, object]:
    return {
        "agent": finding.agent,
        "kind": finding.kind,
        "severity": finding.severity,
        "summary": finding.summary,
        "source": finding.source,
        "evidence": list(finding.evidence),
    }


def _build_candidates(preliminary_causes: list[str], anomalies: list[AssessFinding]) -> list[dict[str, object]]:
    if not preliminary_causes and not anomalies:
        return []

    candidates: list[dict[str, object]] = []
    anomaly_summaries = [item.summary for item in anomalies]

    if preliminary_causes:
        for idx, cause in enumerate(preliminary_causes, start=1):
            support = anomaly_summaries[:3] if anomaly_summaries else []
            candidates.append(
                {
                    "rank": idx,
                    "candidate": cause,
                    "supporting_findings": support,
                }
            )
        return candidates

    for idx, item in enumerate(anomalies[:5], start=1):
        candidates.append(
            {
                "rank": idx,
                "candidate": item.summary,
                "supporting_findings": list(item.evidence[:3]),
            }
        )
    return candidates


def _summarize_domain(findings: list[AssessFinding], prefix: str, fallback: str) -> str:
    subset = [item for item in findings if item.agent.startswith(prefix)]
    if not subset:
        return fallback
    anomalies = [item.summary for item in subset if item.kind == "anomaly"]
    observations = [item.summary for item in subset if item.kind == "observation"]
    if anomalies:
        text = " | ".join(anomalies[:2])
        if observations:
            text += " || " + " | ".join(observations[:1])
        return text
    return " | ".join(observations[:2]) if observations else fallback


def _supporting_evidence(findings: list[AssessFinding]) -> list[str]:
    result: list[str] = []
    by_prefix = ["MetricsAgent", "TraceAgent", "LogsAgent"]
    for prefix in by_prefix:
        subset = [item for item in findings if item.agent.startswith(prefix)]
        if not subset:
            continue
        preferred = next((item for item in subset if item.kind == "anomaly"), subset[0])
        result.append(f"{prefix}: {preferred.summary}")

    for item in [x for x in findings if x.kind == "anomaly"]:
        line = f"{item.agent}: {item.summary}"
        if line not in result:
            result.append(line)
        if len(result) >= 6:
            break

    return result[:6] if result else ["Insufficient consolidated evidence from metrics/trace/log outputs."]


def _iter_buildspec_targets(buildspec: BuildSpec) -> Iterable[tuple[str, str]]:
    for path in buildspec.absolute_log_file:
        yield ("logs", path)
    for path in buildspec.absolute_trace_file:
        yield ("trace", path)
    for path in buildspec.absolute_metrics_file:
        yield ("metrics", path)


def _collect_window_semantic_fallback(
    buildspec: BuildSpec,
    *,
    max_files_per_domain: int = 6,
) -> tuple[Counter[str], Counter[str], list[int]]:
    """Read BuildSpec files in-window to extract fallback component/reason/time signals."""
    start_ts = int(buildspec.failure_time_range_ts.start)
    end_ts = int(buildspec.failure_time_range_ts.end)
    by_domain_count: dict[str, int] = {"logs": 0, "trace": 0, "metrics": 0}
    component_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    ts_candidates: list[int] = []

    for domain, path_value in _iter_buildspec_targets(buildspec):
        if by_domain_count.get(domain, 0) >= max_files_per_domain:
            continue
        by_domain_count[domain] = by_domain_count.get(domain, 0) + 1

        path = Path(path_value)
        if not path.exists() or not path.is_file():
            continue
        try:
            view = telemetry_tools.load_csv_window(path, start_ts=start_ts, end_ts=end_ts)
        except Exception:
            continue
        if view.window_rows <= 0:
            continue
        try:
            context = telemetry_tools.build_llm_observation_context(
                view=view,
                domain=domain,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception:
            continue

        summary = context.get("semantic_summary", {})
        if not isinstance(summary, dict):
            continue

        components = summary.get("top_component_values", [])
        if isinstance(components, list):
            for item in components:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value", "")).strip()
                if not value:
                    continue
                count = item.get("count", 1)
                try:
                    weight = int(count)
                except (TypeError, ValueError):
                    weight = 1
                component_counter[value] += max(1, weight)

        reasons = summary.get("top_reason_values", [])
        if isinstance(reasons, list):
            for item in reasons:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value", "")).strip()
                if not value:
                    continue
                count = item.get("count", 1)
                try:
                    weight = int(count)
                except (TypeError, ValueError):
                    weight = 1
                reason_counter[value] += max(1, weight)

        bounds = summary.get("window_observed_time_bounds", {})
        if isinstance(bounds, dict):
            for key in ("min_ts", "max_ts"):
                raw = bounds.get(key)
                if isinstance(raw, int) and start_ts <= raw <= end_ts:
                    ts_candidates.append(raw)

    return component_counter, reason_counter, ts_candidates


def _find_time_candidates(
    buildspec: BuildSpec,
    findings: list[AssessFinding],
    *,
    preferred_prefixes: tuple[str, ...],
) -> list[int]:
    start_ts = int(buildspec.failure_time_range_ts.start)
    end_ts = int(buildspec.failure_time_range_ts.end)
    candidates: list[int] = []

    ordered = list(findings)
    if preferred_prefixes:
        preferred: list[AssessFinding] = []
        others: list[AssessFinding] = []
        for item in ordered:
            if any(item.agent.startswith(prefix) for prefix in preferred_prefixes):
                preferred.append(item)
            else:
                others.append(item)
        ordered = preferred + others

    for finding in ordered:
        for text in _iter_texts(finding):
            for ts in _extract_unix_ts(text):
                if start_ts <= ts <= end_ts:
                    candidates.append(ts)
    return candidates


def _extract_rows_in_window(task_result: AgentTaskResult) -> int | None:
    match = re.search(r"rows_in_window=(\d+)", task_result.detail or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _task_results_time_candidates(
    buildspec: BuildSpec,
    task_results: list[AgentTaskResult],
) -> list[int]:
    start_ts = int(buildspec.failure_time_range_ts.start)
    end_ts = int(buildspec.failure_time_range_ts.end)
    out: list[int] = []
    for item in task_results:
        for ts in _extract_unix_ts(item.detail or ""):
            if start_ts <= ts <= end_ts:
                out.append(ts)
    return out


def _infer_root_cause_time(
    buildspec: BuildSpec,
    findings: list[AssessFinding],
    task_results: list[AgentTaskResult],
    semantic_ts_candidates: list[int],
) -> tuple[int | str, list[str], str]:
    start_ts = int(buildspec.failure_time_range_ts.start)
    end_ts = int(buildspec.failure_time_range_ts.end)
    network_peaks: list[int] = []
    for finding in findings:
        if finding.kind != "anomaly" or "network delay" not in finding.summary.lower():
            continue
        for evidence in finding.evidence:
            match = re.fullmatch(r"peak_ts=(\d{10})", evidence.strip())
            if match:
                value = int(match.group(1))
                if start_ts <= value <= end_ts:
                    network_peaks.append(value)
    if network_peaks:
        resolved = min(network_peaks)
        return resolved, [f"timestamp_from_network_latency_peak={resolved}"], "known"

    candidates = _find_time_candidates(buildspec, findings, preferred_prefixes=("LogsAgent", "TraceAgent", "MetricsAgent"))
    if candidates:
        resolved = min(candidates)
        return resolved, [f"timestamp_in_window_detected={resolved}"], "known"

    detail_candidates = _task_results_time_candidates(buildspec, task_results)
    if detail_candidates:
        resolved = min(detail_candidates)
        return (
            resolved,
            [f"timestamp_from_task_result_detail={resolved}"],
            "known",
        )

    in_window_semantic = [
        item
        for item in semantic_ts_candidates
        if int(buildspec.failure_time_range_ts.start) <= item <= int(buildspec.failure_time_range_ts.end)
    ]
    if in_window_semantic:
        resolved = min(in_window_semantic)
        return (
            resolved,
            [f"timestamp_from_semantic_window_bounds={resolved}"],
            "known",
        )

    # Deterministic fallback: when no explicit timestamp is found, use failure window start.
    fallback_ts = int(buildspec.failure_time_range_ts.start)
    return (
        fallback_ts,
        [
            (
                "fallback_to_failure_window_start_due_missing_explicit_timestamp="
                f"{fallback_ts}"
            )
        ],
        "known",
    )


def _component_tokens(findings: list[AssessFinding], prefixes: tuple[str, ...]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for finding in findings:
        if prefixes and not any(finding.agent.startswith(prefix) for prefix in prefixes):
            continue
        for text in _iter_texts(finding):
            for candidate in re.findall(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}\b", text or ""):
                if any(char.isdigit() for char in candidate):
                    counter[candidate] += 1
            for token in _COMPONENT_TOKEN_RE.findall(text or ""):
                if token.lower() in {"podclientlatencyp90", "podserverlatencyp90"}:
                    continue
                counter[token] += 1
    return counter


def _component_from_finding(finding: AssessFinding) -> str | None:
    merged = "\n".join([finding.summary, *finding.evidence])
    forbidden = {
        "podname",
        "pod_name",
        "cmdb_id",
        "servicename",
        "service_name",
        "component",
        "instance",
        "host",
        "node",
        "tc",
        "unknown",
        "unresolved",
    }
    patterns = (
        r"\bcomponent\s*=\s*([A-Za-z0-9_.:-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, merged, flags=re.IGNORECASE)
        if not match:
            continue
        component = match.group(1).rstrip(".,;:")
        if component.lower() not in forbidden:
            return component
    return None


def _infer_root_cause_component(
    findings: list[AssessFinding],
    semantic_component_counter: Counter[str],
) -> tuple[str, list[str], str]:
    cpu_candidates: list[tuple[float, AssessFinding, str]] = []
    for finding in findings:
        merged = " ".join(_iter_texts(finding)).lower()
        if (
            finding.kind != "anomaly"
            or not finding.agent.startswith("MetricsAgent")
            or "cpu" not in merged
            or "candidate_reason=cpu " not in merged
        ):
            continue
        component = _component_from_finding(finding)
        if not component:
            continue
        values = re.findall(r"(?:max|pod_cpu_usage_rate_max)=([0-9]+(?:\.[0-9]+)?)", merged)
        strength = max((float(value) for value in values), default=0.0)
        cpu_candidates.append((strength, finding, component))
    if cpu_candidates:
        strength, finding, component = max(cpu_candidates, key=lambda item: item[0])
        return (
            component,
            [f"derived_from={finding.agent}:explicit_cpu_evidence", f"cpu_signal={strength}"],
            "known",
        )

    for finding in findings:
        if finding.kind != "anomaly" or "network delay" not in finding.summary.lower():
            continue
        component = _component_from_finding(finding)
        if component:
            return component, [f"derived_from={finding.agent}:network_latency_evidence"], "known"

    # Prioritize trace evidence to approximate downstream-component selection.
    for prefixes, label in (
        (("TraceAgent",), "trace_priority"),
        (("LogsAgent",), "logs_priority"),
        (("MetricsAgent",), "metrics_priority"),
        (tuple(), "global"),
    ):
        counter = _component_tokens(findings, prefixes)
        if counter:
            component, count = counter.most_common(1)[0]
            return component, [f"component_token_frequency={component}:{count}", f"selection_mode={label}"], "known"

    if semantic_component_counter:
        component, count = semantic_component_counter.most_common(1)[0]
        return (
            component,
            [f"component_from_semantic_window_frequency={component}:{count}"],
            "known",
        )

    return "component_not_resolved", ["no component token found in findings/evidence"], "unknown"


def _infer_root_cause_reason(
    findings: list[AssessFinding],
    preliminary_causes: list[str],
    semantic_reason_counter: Counter[str],
) -> tuple[str, list[str], str]:
    anomalies = [item for item in findings if item.kind == "anomaly"]
    for finding in anomalies:
        merged = " ".join(_iter_texts(finding))
        match = re.search(r"candidate_reason\s*=\s*([^,;|\n]+)", merged, re.IGNORECASE)
        if match:
            return match.group(1).strip(), [f"derived_from={finding.agent}:explicit_reason"], "known"
    for finding in anomalies:
        lowered = " ".join(_iter_texts(finding)).lower()
        if finding.agent.startswith("MetricsAgent") and (
            "cpu saturation" in lowered or "cpu contention" in lowered
        ):
            return "cpu contention", [f"derived_from={finding.agent}:cpu_evidence"], "known"
    for finding in anomalies:
        lowered = " ".join(_iter_texts(finding)).lower()
        if "network" in lowered and ("delay" in lowered or "latency" in lowered):
            return "network delay", [f"derived_from={finding.agent}:network_latency_evidence"], "known"
        if "cpu saturation" in lowered or "cpu contention" in lowered:
            return "cpu contention", [f"derived_from={finding.agent}:cpu_evidence"], "known"

    log_anomalies = [item for item in anomalies if item.agent.startswith("LogsAgent")]
    if log_anomalies:
        return log_anomalies[0].summary, ["derived_from_logs_anomaly"], "known"

    if preliminary_causes:
        return preliminary_causes[0], ["derived_from_preliminary_causes_rank_1"], "known"

    if anomalies:
        return anomalies[0].summary, ["derived_from_top_anomaly_summary"], "unknown"

    if findings:
        return findings[0].summary, ["derived_from_top_finding_summary"], "unknown"

    if semantic_reason_counter:
        reason, count = semantic_reason_counter.most_common(1)[0]
        return (
            reason,
            [f"reason_from_semantic_window_frequency={reason}:{count}"],
            "known",
        )

    return (
        "No explicit root cause reason found in selected telemetry window.",
        ["no findings available to derive reason"],
        "unknown",
    )


def build_assessment_output(buildspec: BuildSpec, executor_result: ExecutorRunResult) -> dict[str, object]:
    """Build user-facing Assess output including synthesis + final reporting."""
    findings = list(executor_result.findings)
    anomalies = [f for f in findings if f.kind == "anomaly"]
    preliminary_causes = list(executor_result.preliminary_causes)

    requested_fields = _requested_fields(buildspec)
    task_scope = _TASK_SCOPE_MAP.get(buildspec.task_type, ())
    unknown_fields = _unknown_fields_from_buildspec(buildspec)
    coordinator = executor_result.coordinator_decision
    coordinator_complete = bool(
        coordinator is not None
        and coordinator.root_cause_component
        and coordinator.root_cause_reason
        and coordinator.root_cause_time
    )
    if coordinator_complete:
        semantic_component_counter = Counter()
        semantic_reason_counter = Counter()
        semantic_ts_candidates: list[int] = []
    else:
        (
            semantic_component_counter,
            semantic_reason_counter,
            semantic_ts_candidates,
        ) = _collect_window_semantic_fallback(buildspec)

    root_cause_time, time_evidence, time_uncertainty = _infer_root_cause_time(
        buildspec,
        findings,
        list(executor_result.task_results),
        semantic_ts_candidates,
    )
    root_cause_component, component_evidence, component_uncertainty = _infer_root_cause_component(
        findings,
        semantic_component_counter,
    )
    root_cause_reason, reason_evidence, reason_uncertainty = _infer_root_cause_reason(
        findings,
        preliminary_causes,
        semantic_reason_counter,
    )
    if coordinator is not None:
        if coordinator.root_cause_component:
            root_cause_component = coordinator.root_cause_component
            component_evidence = [
                "derived_from=coordinator_cross_domain_synthesis",
                coordinator.rationale,
            ]
            component_uncertainty = "known"
        if coordinator.root_cause_reason:
            root_cause_reason = coordinator.root_cause_reason
            reason_evidence = [
                "derived_from=coordinator_cross_domain_synthesis",
                coordinator.rationale,
            ]
            reason_uncertainty = "known"
        if coordinator.root_cause_time:
            window_duration = (
                buildspec.failure_time_range_ts.end - buildspec.failure_time_range_ts.start
            )
            # Short Nezha windows score anomaly onset. Long OpenRCA windows need
            # the explicit anomaly timestamp rather than the broad window start.
            if window_duration <= 180:
                existing_time = (
                    int(root_cause_time)
                    if isinstance(root_cause_time, int)
                    else coordinator.root_cause_time
                )
                root_cause_time = min(existing_time, coordinator.root_cause_time)
            else:
                root_cause_time = coordinator.root_cause_time
            time_evidence = [
                "derived_from=earliest_supported_onset_and_coordinator_synthesis",
                coordinator.rationale,
            ]
            time_uncertainty = "known"

    supporting_evidence = _supporting_evidence(findings)
    if coordinator is not None and coordinator.rationale:
        supporting_evidence = [
            f"Coordinator: {coordinator.rationale}",
            *supporting_evidence,
        ][:6]

    root_cause_synthesis = {
        "task_type": "root_cause_synthesis",
        "date": buildspec.date,
        "filename_date": buildspec.filename_date,
        "failure_time_range": {
            "start": buildspec.failure_time_range.start,
            "end": buildspec.failure_time_range.end,
        },
        "metrics_summary": _summarize_domain(
            findings,
            "MetricsAgent",
            "No metrics findings available.",
        ),
        "trace_summary": _summarize_domain(
            findings,
            "TraceAgent",
            "No trace findings available.",
        ),
        "log_summary": _summarize_domain(
            findings,
            "LogsAgent",
            "No log findings available.",
        ),
        "final_diagnosis": {
            "root_cause_component": root_cause_component,
            "root_cause_reason": root_cause_reason,
            "root_cause_time": root_cause_time,
            "confidence": (
                coordinator.confidence
                if coordinator is not None
                else _confidence_label(executor_result.confidence)
            ),
            "supporting_evidence": supporting_evidence,
        },
        "uncertainty": {
            "root_cause_time": time_uncertainty,
            "root_cause_component": component_uncertainty,
            "root_cause_reason": reason_uncertainty,
        },
    }

    final_reporting: dict[str, object] = {}
    for field in requested_fields:
        key = _FINAL_KEY_BY_FIELD.get(field)
        if key is None:
            continue
        if field == "root_cause_time":
            final_reporting[key] = root_cause_synthesis["final_diagnosis"]["root_cause_time"]
        elif field == "root_cause_component":
            final_reporting[key] = root_cause_synthesis["final_diagnosis"]["root_cause_component"]
        elif field == "root_cause_reason":
            final_reporting[key] = root_cause_synthesis["final_diagnosis"]["root_cause_reason"]

    return {
        "summary": executor_result.summary,
        "execution_mode": executor_result.execution_mode,
        "causal_graph": (
            executor_result.causal_graph.model_dump(mode="json")
            if executor_result.causal_graph is not None
            else {"nodes": [], "edges": [], "root_node_id": ""}
        ),
        "buildspec_resolution_scope": {
            "task_type": buildspec.task_type,
            "task_scope_fields": list(task_scope),
            "requested_fields": list(requested_fields),
            "uncertainty_unknown_fields": list(unknown_fields),
        },
        "findings": [_as_view(item) for item in findings],
        "anomalies": [_as_view(item) for item in anomalies],
        "preliminary_causes": preliminary_causes,
        "root_cause_candidates": _build_candidates(preliminary_causes, anomalies),
        "confidence": {
            "numeric": executor_result.confidence,
            "label": _confidence_label(executor_result.confidence),
        },
        "root_cause_synthesis": root_cause_synthesis,
        "final_reporting": final_reporting,
        "resolution_evidence": {
            "root_cause_time": time_evidence,
            "root_cause_component": component_evidence,
            "root_cause_reason": reason_evidence,
        },
    }
