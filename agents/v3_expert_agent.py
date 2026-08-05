"""Independent deterministic metric experts used by the V3 RCA strategy."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Literal

from agents.base import Agent
from aware_models.buildspec import BuildSpec
from aware_models.executor import AgentTaskResult, AssessFinding
from tools.telemetry_tools import load_csv_window, parse_timestamp_seconds

ExpertKind = Literal["jvm", "mysql", "redis"]


class V3MetricExpertAgent(Agent):
    """Analyze one technical KPI family without consuming another agent's opinion."""

    specialty: ExpertKind

    def __init__(self, specialty: ExpertKind) -> None:
        names = {"jvm": "JVMExpert", "mysql": "MySQLExpert", "redis": "RedisExpert"}
        super().__init__(
            name=names[specialty],
            description=f"Independent {specialty.upper()} change-point and baseline expert.",
            specialty=specialty,
        )

    def analyze(self, paths: list[Path], buildspec: BuildSpec) -> AgentTaskResult:
        series: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
        analyzed: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            view = load_csv_window(
                path,
                start_ts=buildspec.failure_time_range_ts.start,
                end_ts=buildspec.failure_time_range_ts.end,
            )
            if not {"cmdb_id", "kpi_name", "value"}.issubset(set(view.fieldnames)):
                continue
            analyzed.append(str(path))
            for row in view.rows:
                component = str(row.get("cmdb_id") or "").strip()
                kpi = str(row.get("kpi_name") or "").strip()
                if not self._accept(component, kpi):
                    continue
                ts = parse_timestamp_seconds(row.get(view.timestamp_field or "", ""))
                try:
                    value = float(row.get("value", ""))
                except (TypeError, ValueError):
                    continue
                if component and kpi and ts is not None:
                    series[(component, kpi)].append((ts, value))

        candidates = [
            candidate
            for key, samples in series.items()
            if (candidate := self._score_series(key[0], key[1], samples)) is not None
        ]
        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        findings = [self._finding(item, analyzed) for item in candidates[:2]]
        detail = (
            f"v3_specialty={self.specialty}; independent_evidence=true; "
            f"series_analyzed={len(series)}; candidates={len(candidates)}; "
            f"sources={len(analyzed)}"
        )
        return AgentTaskResult(
            agent_name=self.name,
            target_path=json.dumps(analyzed, ensure_ascii=False),
            status="ok" if findings else "skipped",
            findings=findings,
            detail=detail,
        )

    def _accept(self, component: str, kpi: str) -> bool:
        comp = component.lower()
        metric = kpi.lower()
        if self.specialty == "jvm":
            return "jvm" in metric and comp.startswith(("mg", "ig", "tomcat"))
        if self.specialty == "mysql":
            return comp.startswith("mysql") and any(
                token in metric for token in ("memory", "buffer", "memusedmemperc", "nocachememperc")
            )
        return comp.startswith("redis") and any(
            token in metric for token in ("memory", "used_memory", "memusedmemperc", "nocachememperc")
        )

    def _score_series(
        self,
        component: str,
        kpi: str,
        raw_samples: list[tuple[int, float]],
    ) -> dict[str, object] | None:
        samples = sorted(raw_samples)
        if len(samples) < 4:
            return None
        values = [value for _, value in samples]
        metric = kpi.lower()
        median = statistics.median(values)
        transitions = [
            (abs(samples[index][1] - samples[index - 1][1]), index)
            for index in range(1, len(samples))
        ]
        delta, transition_index = max(transitions)
        before_ts, before = samples[transition_index - 1]
        after_ts, after = samples[transition_index]
        scale = max(abs(median), 1.0)
        score = delta / scale
        reason = ""
        onset_ts = before_ts

        if self.specialty == "jvm" and "jvm_cpuload" in metric:
            if max(values) < 45.0 or delta < 10.0:
                return None
            reason = "high JVM CPU load"
            score += min(max(values) / 20.0, 5.0)
            onset_ts = after_ts
        elif self.specialty == "jvm" and "noheapmemoryused" in metric:
            # Ordinary collections produce large heap saw-teeth. They are not
            # OOM evidence. A sustained non-heap expansion is much rarer and is
            # used here as the conservative JVM memory-pressure indicator.
            growth = after - before
            if growth < 10_000_000 or growth / max(abs(before), 1.0) < 0.015:
                return None
            reason = "JVM Out of Memory (OOM) Heap"
            score = 6.0 + min(growth / 20_000_000, 6.0)
            onset_ts = after_ts
        elif self.specialty in {"mysql", "redis"} and (
            "memusedmemperc" in metric or "nocachememperc" in metric
        ):
            maximum = max(values)
            threshold = 70.0 if self.specialty == "redis" and "nocachememperc" in metric else 85.0
            if maximum < threshold:
                return None
            reason = "high memory usage"
            jump = after - before
            # A static high percentage is not enough to identify one database
            # among several hosts with chronically high page-cache usage.
            if self.specialty == "mysql" and abs(jump) < 10.0:
                return None
            if self.specialty == "redis" and abs(jump) < 20.0:
                return None
            score = max(score * 4.0, maximum / 12.0)
            high_samples = [ts for ts, value in samples if value >= threshold]
            onset_ts = min(high_samples, default=after_ts)
        elif self.specialty == "redis" and "used_memory" in metric:
            if score < 0.5:
                return None
            reason = "high memory usage"
        else:
            return None

        return {
            "component": component,
            "reason": reason,
            "kpi": kpi,
            "score": round(float(score), 4),
            "minimum": min(values),
            "maximum": max(values),
            "before": before,
            "after": after,
            "onset_ts": onset_ts,
            "support_count": len(values),
        }

    def _finding(self, candidate: dict[str, object], sources: list[str]) -> AssessFinding:
        component = str(candidate["component"])
        reason = str(candidate["reason"])
        return AssessFinding(
            agent=self.name,
            kind="anomaly",
            source=" | ".join(sources),
            summary=(
                f"Independent {self.specialty.upper()} expert identifies {reason} "
                f"on component {component}."
            ),
            evidence=[
                f"component={component}",
                f"candidate_reason={reason}",
                f"kpi_name={candidate['kpi']}",
                f"expert_score={candidate['score']}",
                f"baseline_before={candidate['before']}",
                f"transition_after={candidate['after']}",
                f"kpi_min={candidate['minimum']}",
                f"kpi_max={candidate['maximum']}",
                f"support_count={candidate['support_count']}",
                f"peak_ts={candidate['onset_ts']}",
                "independent_evidence=true",
            ],
            severity="high",
        )
