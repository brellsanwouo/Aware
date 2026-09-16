"""ExecutorAgent with template-driven ADK sub-agent instantiation for Assess."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent
from agents.v3_expert_agent import V3MetricExpertAgent
from llm.base import LLMClient, LLMError
from aware_models.buildspec import BuildSpec
from aware_models.executor import (
    AgentTaskResult,
    AssessFinding,
    CoordinatorDecision,
    ExecutorRunResult,
)
from runtime.knowledge_db import SQLiteKnowledgeStore
from runtime.causal_graph import build_causal_graph
from templates.assess_templates import AgentTemplate, load_assess_templates
from tools import telemetry_tools

_DECISION_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "assess_agent_decision",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "findings"],
            "properties": {
                "summary": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "summary", "severity", "evidence"],
                        "properties": {
                            "kind": {"type": "string", "enum": ["anomaly", "observation"]},
                            "summary": {"type": "string"},
                            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}

_COORDINATOR_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "rca_coordinator_checkpoint",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "root_cause_component",
                "root_cause_reason",
                "root_cause_time",
                "confidence",
                "unresolved_fields",
                "focus_component",
                "followup_domains",
                "rationale",
            ],
            "properties": {
                "root_cause_component": {"type": "string"},
                "root_cause_reason": {"type": "string"},
                "root_cause_time": {"type": "integer"},
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "unresolved_fields": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "root_cause_component",
                            "root_cause_reason",
                            "root_cause_time",
                        ],
                    },
                },
                "focus_component": {"type": "string"},
                "followup_domains": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["logs", "trace", "metrics"],
                    },
                },
                "rationale": {"type": "string"},
            },
        },
    },
}


class ExecutorAgentError(RuntimeError):
    """Raised when ExecutorAgent cannot complete assess execution."""


class ExecutorEvent(BaseModel):
    """One executor conversation event."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sender: str
    recipient: str
    phase: str
    content: str


class AnalyzerAgent(Agent):
    """Base class for specialized telemetry analyzers."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    def __init__(
        self,
        *,
        template: AgentTemplate,
        llm_client: LLMClient | None,
        instance_name: str | None = None,
        knowledge_text: str = "",
        known_components: list[str] | None = None,
        known_reasons: list[str] | None = None,
        enable_reasoning: bool = True,
        enable_memory: bool = True,
    ) -> None:
        super().__init__(name=instance_name or template.agent_name, description=template.objective)
        self.template = template
        self.llm_client = llm_client
        self.knowledge_text = knowledge_text
        self.known_components = list(known_components or [])
        self.known_reasons = list(known_reasons or [])
        self.enable_reasoning = bool(enable_reasoning)
        self.enable_memory = bool(enable_memory)

    def analyze(
        self,
        file_path: Path,
        buildspec: BuildSpec,
        shared_memory_context: list[str] | None = None,
        component_focus: str | None = None,
    ) -> AgentTaskResult:
        """Run strict-window tools, then let LLM decide findings (fallback on tool heuristics)."""
        if not file_path.exists() or not file_path.is_file():
            return AgentTaskResult(
                agent_name=self.name,
                target_path=str(file_path),
                status="skipped",
                detail="Target file is missing.",
            )

        view = telemetry_tools.load_csv_window(
            file_path=file_path,
            start_ts=buildspec.failure_time_range_ts.start,
            end_ts=buildspec.failure_time_range_ts.end,
        )
        focus_note = ""
        if component_focus:
            focused = telemetry_tools.apply_component_focus(
                view,
                domain=self.template.domain,
                component=component_focus,
            )
            view = focused.view
            focus_note = (
                f" component_focus={component_focus}; "
                f"focus_columns={focused.matched_columns}; focused_rows={view.window_rows}."
            )
        detail = telemetry_tools.window_detail(
            view,
            start_ts=buildspec.failure_time_range_ts.start,
            end_ts=buildspec.failure_time_range_ts.end,
        )
        tool_context = telemetry_tools.build_llm_observation_context(
            view,
            domain=self.template.domain,
            start_ts=buildspec.failure_time_range_ts.start,
            end_ts=buildspec.failure_time_range_ts.end,
        )
        semantic_findings = _semantic_context_findings(
            domain=self.template.domain,
            tool_context=tool_context,
            source_path=str(file_path),
            agent_name=self.name,
            known_components=self.known_components,
        )
        findings: list[AssessFinding]
        decision_source = "llm"
        llm_summary = ""
        memory_context = list(shared_memory_context or []) if self.enable_memory else []
        if self.llm_client is None:
            raise RuntimeError(f"{self.name} requires an LLM client.")
        llm_findings, llm_summary = _decide_findings_with_llm(
            llm_client=self.llm_client,
            template=self.template,
            target_path=str(file_path),
            buildspec=buildspec,
            tool_context=tool_context,
            agent_name=self.name,
            knowledge_text=self.knowledge_text,
            known_components=self.known_components,
            known_reasons=self.known_reasons,
            shared_memory_context=memory_context,
            reasoning_enabled=self.enable_reasoning,
        )
        if llm_findings:
            findings = llm_findings
        else:
            findings = _fallback_findings_from_tools(self.template.domain, view, str(file_path), self.name)
            decision_source = "tools_fallback_after_llm"

        if semantic_findings:
            existing = {item.summary for item in findings}
            for item in semantic_findings:
                if item.summary not in existing:
                    findings.append(item)

        if not findings:
            findings = [
                AssessFinding(
                    agent=self.name,
                    kind="observation",
                    source=str(file_path),
                    summary="No significant signal detected for this telemetry source.",
                    evidence=view.lines[:3],
                    severity="low",
                )
            ]
        summary_suffix = f" decision_source={decision_source}."
        if llm_summary:
            summary_suffix += f" llm_summary={llm_summary[:220]}"
        if memory_context:
            summary_suffix += f" memory_context_items={len(memory_context)}."
        if focus_note:
            summary_suffix += focus_note
        return AgentTaskResult(
            agent_name=self.name,
            target_path=str(file_path),
            status="ok",
            findings=findings,
            detail=f"{detail}{summary_suffix}",
        )

    def analyze_need(
        self,
        file_paths: tuple[Path, ...],
        buildspec: BuildSpec,
        shared_memory_context: list[str] | None = None,
        component_focus: str | None = None,
    ) -> AgentTaskResult:
        """Resolve one analytical need using one or more evidence fragments."""
        if len(file_paths) > 1:
            return self._analyze_combined_need(
                file_paths,
                buildspec,
                shared_memory_context=shared_memory_context,
                component_focus=component_focus,
            )
        partial = [
            self.analyze(
                file_path,
                buildspec,
                shared_memory_context=shared_memory_context,
                component_focus=component_focus,
            )
            for file_path in file_paths
        ]
        if len(partial) == 1:
            return partial[0]
        findings = [finding for item in partial for finding in item.findings]
        statuses = {item.status for item in partial}
        status = "ok" if "ok" in statuses else ("error" if "error" in statuses else "skipped")
        return AgentTaskResult(
            agent_name=self.name,
            target_path=json.dumps([str(path) for path in file_paths], ensure_ascii=False),
            status=status,
            findings=findings,
            detail=(
                f"need_sources={len(file_paths)}; component_focus={component_focus or 'none'}; "
                + " | ".join(item.detail for item in partial)
            ),
        )

    def _analyze_combined_need(
        self,
        file_paths: tuple[Path, ...],
        buildspec: BuildSpec,
        shared_memory_context: list[str] | None,
        component_focus: str | None,
    ) -> AgentTaskResult:
        """Analyze a domain as one task and make one consolidated LLM decision."""
        source_contexts: list[dict[str, Any]] = []
        semantic_findings: list[AssessFinding] = []
        fallback_findings: list[AssessFinding] = []
        details: list[str] = []
        statuses: set[str] = set()
        for file_path in file_paths:
            if not file_path.exists() or not file_path.is_file():
                statuses.add("skipped")
                details.append(f"{file_path}: missing")
                continue
            view = telemetry_tools.load_csv_window(
                file_path=file_path,
                start_ts=buildspec.failure_time_range_ts.start,
                end_ts=buildspec.failure_time_range_ts.end,
            )
            if component_focus:
                view = telemetry_tools.apply_component_focus(
                    view,
                    domain=self.template.domain,
                    component=component_focus,
                ).view
            context = telemetry_tools.build_llm_observation_context(
                view,
                domain=self.template.domain,
                start_ts=buildspec.failure_time_range_ts.start,
                end_ts=buildspec.failure_time_range_ts.end,
                sample_limit=4,
            )
            semantic_findings.extend(
                _semantic_context_findings(
                    domain=self.template.domain,
                    tool_context=context,
                    source_path=str(file_path),
                    agent_name=self.name,
                    known_components=self.known_components,
                )
            )
            fallback_findings.extend(
                _fallback_findings_from_tools(
                    self.template.domain,
                    view,
                    str(file_path),
                    self.name,
                )
            )
            source_contexts.append(
                {
                    "source": str(file_path),
                    "rows_in_window": view.window_rows,
                    "timestamp_field": view.timestamp_field,
                    "semantic_columns": context.get("semantic_columns", {}),
                    "semantic_summary": context.get("semantic_summary", {}),
                    "inter_service_network_latency": context.get(
                        "inter_service_network_latency", []
                    ),
                    "log_failure_signals": context.get("log_failure_signals", []),
                    "long_form_kpi_anomalies": context.get("long_form_kpi_anomalies", []),
                    "service_latency_anomalies": context.get("service_latency_anomalies", []),
                    "sample_lines": context.get("sample_lines", [])[:4],
                }
            )
            details.append(
                f"{file_path.name}: "
                + telemetry_tools.window_detail(
                    view,
                    start_ts=buildspec.failure_time_range_ts.start,
                    end_ts=buildspec.failure_time_range_ts.end,
                )
            )
            statuses.add("ok")

        combined_context = {
            "domain": self.template.domain,
            "window": {
                "start": buildspec.failure_time_range_ts.start,
                "end": buildspec.failure_time_range_ts.end,
            },
            "component_focus": component_focus,
            "source_count": len(source_contexts),
            "sources": source_contexts,
        }
        llm_findings, llm_summary = _decide_findings_with_llm(
            llm_client=self.llm_client,
            template=self.template,
            target_path=json.dumps([str(path) for path in file_paths], ensure_ascii=False),
            buildspec=buildspec,
            tool_context=combined_context,
            agent_name=self.name,
            knowledge_text=self.knowledge_text,
            known_components=self.known_components,
            known_reasons=self.known_reasons,
            shared_memory_context=(
                list(shared_memory_context or []) if self.enable_memory else []
            ),
            reasoning_enabled=self.enable_reasoning,
        )
        selected = llm_findings or fallback_findings
        findings = _compact_domain_findings([*selected, *semantic_findings])
        status = "ok" if "ok" in statuses else "skipped"
        focus_note = f"; component_focus={component_focus}" if component_focus else ""
        return AgentTaskResult(
            agent_name=self.name,
            target_path=json.dumps([str(path) for path in file_paths], ensure_ascii=False),
            status=status,
            findings=findings,
            detail=(
                f"need_sources={len(file_paths)}; consolidated_llm_decisions=1{focus_note}; "
                f"llm_summary={llm_summary[:220]}; "
                + " | ".join(details)
            ),
        )


class ExecutorAgent(Agent):
    """Orchestrate Assess execution using dynamic templates and ADK sub-agents."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    def __init__(
        self,
        *,
        llm_client: LLMClient | None = None,
        templates: list[AgentTemplate] | None = None,
        knowledge_file: str | None = None,
        enable_reasoning: bool = True,
        enable_memory: bool = True,
        execution_mode: str = "v3",
    ) -> None:
        super().__init__(
            name="ExecutorAgent",
            description="Execute Assess tasks from BuildSpec via dynamic ADK sub-agents.",
        )
        self.llm_client = llm_client
        self.templates = templates or load_assess_templates()
        self.enable_reasoning = bool(enable_reasoning)
        self.enable_memory = bool(enable_memory)
        self.execution_mode = "v3"
        self.knowledge_file = knowledge_file or os.getenv(
            "AWARE_EXECUTOR_V3_KB_FILE",
            "knowledge/executor_rca_v3_kb.md",
        )

    def execute_assess(
        self,
        *,
        buildspec: BuildSpec,
        repository_path: str,
        max_agents: int | None = None,
        run_id: str | None = None,
        knowledge_store: SQLiteKnowledgeStore | None = None,
        on_event: Callable[[ExecutorEvent], None] | None = None,
    ) -> ExecutorRunResult:
        repo = Path(repository_path)
        if not repo.is_absolute():
            repo = (Path.cwd() / repo).resolve()
        self._emit(
            on_event,
            phase="init",
            recipient="Runtime",
            content=(
                f"ExecutorAgent started. repository={repo}; "
                f"reasoning={'on' if self.enable_reasoning else 'off'}; "
                f"memory={'on' if self.enable_memory else 'off'}; "
                f"execution_mode={self.execution_mode}."
            ),
        )
        if not repo.exists():
            raise ExecutorAgentError(f"Repository path does not exist: {repo}")
        self._emit(
            on_event,
            phase="load_templates",
            recipient="Runtime",
            content=f"Loaded {len(self.templates)} templates from templates/assess_templates.py",
        )
        knowledge_source, knowledge_text, known_components, known_reasons = _load_executor_knowledge(
            self.knowledge_file
        )
        self._emit(
            on_event,
            phase="load_knowledge",
            recipient="Runtime",
            content=(
                f"Loaded executor knowledge from {knowledge_source} "
                f"(chars={len(knowledge_text)}; components={len(known_components)}; reasons={len(known_reasons)})."
            ),
        )

        cap = max(1, int(max_agents)) if max_agents is not None else None
        configured_expansions = os.getenv("EXECUTOR_MAX_EXPANSIONS", "2").strip()
        expansion_budget = (
            int(configured_expansions) if configured_expansions.isdigit() else 2
        )
        expansion_budget = max(0, min(expansion_budget, 4))
        if cap is not None:
            self._emit(
                on_event,
                phase="config",
                recipient="Runtime",
                content=f"Optional safety task budget set to {cap}.",
            )
        self._emit(
            on_event,
            phase="controlled_adaptive_config",
            recipient="Runtime",
            content=(
                "Controlled adaptive policy: at most one seed agent per available "
                f"telemetry domain and at most {expansion_budget} focused follow-up agent(s)."
            ),
        )

        task_results: list[AgentTaskResult] = []
        findings: list[AssessFinding] = []
        shared_memory_context: list[str] = []
        agents_instantiated: list[str] = []

        # Controlled adaptive scheduler:
        # 1) discover initial targets by template
        # 2) enqueue one grouped seed task per available telemetry domain
        # 3) instantiate-run-terminate one agent at a time
        # 4) after all seeds, optionally enqueue at most a few focused follow-ups
        discovered_by_template: dict[str, tuple[AgentTemplate, list[Path]]] = {}
        template_order: list[str] = []
        seed_domain_count = 0
        for template in self.templates:
            targets = _discover_targets_for_template(buildspec, template)
            discovered_by_template[template.template_id] = (template, targets)
            template_order.append(template.template_id)
            if targets:
                seed_domain_count += 1
            self._emit(
                on_event,
                phase="plan_targets",
                recipient="ExecutorAgent",
                content=f"Template {template.template_id} resolved {len(targets)} target(s).",
                sender=template.agent_name,
            )

        if cap is not None and seed_domain_count < cap:
            self._emit(
                on_event,
                phase="capacity_info",
                recipient="Runtime",
                content=(
                    f"max_agents={cap}, but only {seed_domain_count} initial domain task(s) "
                    "were planned from the BuildSpec."
                ),
            )

        queue: list[tuple[AgentTemplate, tuple[Path, ...], str | None, str]] = []
        queued_signatures: set[tuple[str, str, str]] = set()
        executed_signatures: set[tuple[str, str, str]] = set()
        cap_reached = False

        def _task_signature(
            template_id: str,
            target_paths: tuple[Path, ...],
            component_focus: str | None,
        ) -> tuple[str, str, str]:
            paths_key = "\n".join(str(path) for path in target_paths)
            return (template_id, paths_key, (component_focus or "").strip().lower())

        def _enqueue_task(
            *,
            template: AgentTemplate,
            target_paths: tuple[Path, ...],
            component_focus: str | None,
            origin: str,
            instantiated_count_now: int,
        ) -> bool:
            nonlocal cap_reached
            if cap is not None and (instantiated_count_now + len(queue)) >= cap:
                cap_reached = True
                return False
            if not target_paths:
                return False
            signature = _task_signature(template.template_id, target_paths, component_focus)
            if signature in queued_signatures or signature in executed_signatures:
                return False
            queue.append((template, target_paths, component_focus, origin))
            queued_signatures.add(signature)
            return True

        seed_order = sorted(
            template_order,
            key=lambda template_id: {
                "metrics": 0,
                "logs": 1,
                "trace": 2,
            }.get(discovered_by_template[template_id][0].domain, 3),
        )
        for template_id in seed_order:
            template, targets = discovered_by_template[template_id]
            if targets:
                # All files from one signal family jointly answer one domain question.
                _enqueue_task(
                    template=template,
                    target_paths=tuple(targets),
                    component_focus=None,
                    origin="seed",
                    instantiated_count_now=0,
                )

        instantiated_count = 0
        instance_counts: dict[str, int] = {}
        expansion_tasks_enqueued = 0
        component_display: dict[str, str] = {}
        component_domain_support: dict[str, set[str]] = {}
        component_evidence_score: dict[str, int] = {}
        expansion_planned = False
        v3_experts_planned = False
        seed_checkpoint: CoordinatorDecision | None = None
        while queue:
            if cap is not None and instantiated_count >= cap:
                cap_reached = True
                break
            template, target_paths, component_focus, origin = queue.pop(0)
            signature = _task_signature(template.template_id, target_paths, component_focus)
            queued_signatures.discard(signature)
            if signature in executed_signatures:
                continue
            current_count = instance_counts.get(template.agent_name, 0) + 1
            instance_counts[template.agent_name] = current_count
            instance_name = (
                template.agent_name if current_count == 1 else f"{template.agent_name}_{current_count}"
            )

            self._emit(
                on_event,
                phase="instantiate_agent",
                recipient="ExecutorAgent",
                content=(
                    f"Instantiated {instance_name} from template={template.template_id}; "
                    f"tools={', '.join(template.tools)}; sources={len(target_paths)}; "
                    f"component_focus={component_focus or 'none'}; origin={origin}"
                ),
                sender=instance_name,
            )
            agent = AnalyzerAgent(
                template=template,
                llm_client=self.llm_client,
                instance_name=instance_name,
                knowledge_text=knowledge_text,
                known_components=known_components,
                known_reasons=known_reasons,
                enable_reasoning=self.enable_reasoning,
                enable_memory=self.enable_memory,
            )
            agents_instantiated.append(instance_name)
            instantiated_count += 1

            self._emit(
                on_event,
                phase="dispatch",
                recipient="ExecutorAgent",
                content=(
                    f"I will resolve one {template.domain} need from {len(target_paths)} source(s) using tools "
                    f"[{', '.join(template.tools)}], then decide with LLM "
                    f"(reasoning_mode={'deep' if self.enable_reasoning else 'fast'}); "
                    f"component_focus={component_focus or 'none'}."
                ),
                sender=instance_name,
            )
            # V3 experts must form their first opinion from telemetry, not from
            # another expert's prose. Cross-agent opinions are reconciled later.
            analysis_memory_context: list[str] = []
            result = agent.analyze_need(
                target_paths,
                buildspec,
                shared_memory_context=analysis_memory_context,
                component_focus=component_focus,
            )
            executed_signatures.add(signature)
            task_results.append(result)
            findings.extend(result.findings)
            if self.enable_memory and knowledge_store and run_id:
                knowledge_store.append_task_result(
                    run_id=run_id,
                    agent_name=result.agent_name,
                    target_path=result.target_path,
                    status=result.status,
                    detail=result.detail,
                )
                for finding in result.findings:
                    knowledge_store.append_finding(
                        run_id=run_id,
                        agent_name=finding.agent,
                        kind=finding.kind,
                        source=finding.source,
                        summary=finding.summary,
                        evidence=finding.evidence,
                        severity=finding.severity,
                    )
                stored_component_items = _store_component_memory_from_findings(
                    knowledge_store=knowledge_store,
                    run_id=run_id,
                    findings=result.findings,
                    known_components=known_components,
                    max_components=4,
                    max_findings_per_component=3,
                )
                if stored_component_items > 0:
                    self._emit(
                        on_event,
                        phase="store_component_memory",
                        recipient="ExecutorAgent",
                        content=(
                            f"Stored {stored_component_items} component-memory item(s) "
                            f"from {instance_name} findings."
                        ),
                        sender=instance_name,
                    )
            if self.enable_memory:
                shared_memory_context.extend(
                    [
                        f"{finding.agent}|{finding.kind}|{finding.summary}|severity={finding.severity}"
                        for finding in result.findings[:5]
                    ]
                )
            self._emit(
                on_event,
                phase="agent_result",
                recipient="ExecutorAgent",
                content=f"{instance_name} completed with status={result.status}; findings={len(result.findings)}",
                sender=instance_name,
            )
            for finding in result.findings[:3]:
                self._emit(
                    on_event,
                    phase="finding",
                    recipient="ExecutorAgent",
                    content=f"{finding.kind.upper()}: {finding.summary}",
                    sender=instance_name,
                )

            # Agent lifecycle is explicit: one run then terminate.
            self._emit(
                on_event,
                phase="terminate_agent",
                recipient="ExecutorAgent",
                content=f"Terminated {instance_name} after task completion.",
                sender=instance_name,
            )
            del agent

            # Dynamic decomposition: findings create new component investigation needs.
            # The vocabulary is open; any telemetry-backed component may become a need.
            component_candidates = _extract_component_candidates_from_findings(
                result.findings,
                known_components=known_components,
            )
            if origin == "seed":
                for component in component_candidates:
                    key = component.strip().lower()
                    if not key:
                        continue
                    component_display.setdefault(key, component)
                    component_domain_support.setdefault(key, set()).add(template.domain)
                    matching_findings = [
                        finding
                        for finding in result.findings
                        if _finding_mentions_component(finding, component)
                    ]
                    component_evidence_score[key] = component_evidence_score.get(key, 0) + sum(
                        {"low": 1, "medium": 2, "high": 3}[finding.severity]
                        + (2 if finding.kind == "anomaly" else 0)
                        for finding in matching_findings
                    )
            if component_candidates:
                self._emit(
                    on_event,
                    phase="expand_detect",
                    recipient="ExecutorAgent",
                    content=(
                        f"Discovered component candidate(s): {', '.join(component_candidates[:8])}"
                    ),
                    sender=instance_name,
                )
            # Complete broad evidence coverage first. Then, at most once, focus the
            # best weakly-supported component in other domains. A candidate already
            # corroborated by two domains does not justify another agent.
            baseline_pending = any(queued_origin == "seed" for _, _, _, queued_origin in queue)
            enqueued_now = 0
            should_checkpoint = not baseline_pending and not expansion_planned
            if should_checkpoint:
                expansion_planned = True
                if not v3_experts_planned:
                    v3_experts_planned = True
                    metric_targets = [Path(item) for item in buildspec.absolute_metrics_file]
                    routed_specialties = ["jvm", "mysql", "redis"] if metric_targets else []
                    self._emit(
                        on_event,
                        phase="expert_router",
                        recipient="Runtime",
                        content=(
                            "V3 evidence router selected: "
                            + (", ".join(routed_specialties) if routed_specialties else "no technical specialist")
                            + ". Each specialist may explicitly abstain when its mechanism is unsupported."
                        ),
                    )
                    for specialty in routed_specialties:
                        if cap is not None and instantiated_count >= cap:
                            cap_reached = True
                            break
                        expert = V3MetricExpertAgent(specialty)  # type: ignore[arg-type]
                        self._emit(
                            on_event,
                            phase="instantiate_agent",
                            recipient="ExecutorAgent",
                            content=(
                                f"Instantiated {expert.name} for independent V3 "
                                f"specialty={specialty}; sources={len(metric_targets)}."
                            ),
                            sender=expert.name,
                        )
                        agents_instantiated.append(expert.name)
                        instantiated_count += 1
                        self._emit(
                            on_event,
                            phase="dispatch",
                            recipient="ExecutorAgent",
                            content=(
                                "Independent evidence pass: change-point, baseline, "
                                f"and mechanism analysis for {specialty}."
                            ),
                            sender=expert.name,
                        )
                        expert_result = expert.analyze(metric_targets, buildspec)
                        task_results.append(expert_result)
                        findings.extend(expert_result.findings)
                        if self.enable_memory and knowledge_store and run_id:
                            knowledge_store.append_task_result(
                                run_id=run_id,
                                agent_name=expert_result.agent_name,
                                target_path=expert_result.target_path,
                                status=expert_result.status,
                                detail=expert_result.detail,
                            )
                            for expert_finding in expert_result.findings:
                                knowledge_store.append_finding(
                                    run_id=run_id,
                                    agent_name=expert_finding.agent,
                                    kind=expert_finding.kind,
                                    source=expert_finding.source,
                                    summary=expert_finding.summary,
                                    evidence=expert_finding.evidence,
                                    severity=expert_finding.severity,
                                )
                        expert_components = _extract_component_candidates_from_findings(
                            expert_result.findings,
                            known_components=known_components,
                        )
                        for expert_component in expert_components:
                            key = expert_component.strip().lower()
                            component_display.setdefault(key, expert_component)
                            component_domain_support.setdefault(key, set()).add(
                                f"expert:{specialty}"
                            )
                            component_evidence_score[key] = (
                                component_evidence_score.get(key, 0)
                                + sum(
                                    5
                                    for finding in expert_result.findings
                                    if _finding_mentions_component(finding, expert_component)
                                )
                            )
                        self._emit(
                            on_event,
                            phase="agent_result",
                            recipient="ExecutorAgent",
                            content=(
                                f"{expert.name} completed with status={expert_result.status}; "
                                f"findings={len(expert_result.findings)}"
                            ),
                            sender=expert.name,
                        )
                        self._emit(
                            on_event,
                            phase="terminate_agent",
                            recipient="ExecutorAgent",
                            content=f"Terminated {expert.name} after independent expert pass.",
                            sender=expert.name,
                        )
                seed_checkpoint = _coordinate_findings(
                    llm_client=self.llm_client,
                    buildspec=buildspec,
                    findings=findings,
                )
                if seed_checkpoint is not None:
                    self._emit(
                        on_event,
                        phase="synthesis_checkpoint",
                        recipient="Runtime",
                        content=(
                            f"First-pass synthesis: confidence={seed_checkpoint.confidence}; "
                            f"component={seed_checkpoint.root_cause_component or 'unresolved'}; "
                            f"reason={seed_checkpoint.root_cause_reason or 'unresolved'}; "
                            f"time={seed_checkpoint.root_cause_time or 'unresolved'}; "
                            f"unresolved={seed_checkpoint.unresolved_fields}."
                        ),
                    )
                ranked_components = sorted(
                    component_domain_support,
                    key=lambda key: (
                        -len(component_domain_support[key]),
                        -component_evidence_score.get(key, 0),
                        key,
                    ),
                )
                selected_key = ranked_components[0] if ranked_components else None
                fallback_component = component_display.get(selected_key or "", "")
                component = (
                    (
                        seed_checkpoint.focus_component
                        or seed_checkpoint.root_cause_component
                        or fallback_component
                    )
                    if seed_checkpoint is not None
                    else fallback_component
                )
                component_key = component.strip().lower()
                support = component_domain_support.get(component_key, set())
                checkpoint_complete = bool(
                    seed_checkpoint is not None
                    and seed_checkpoint.confidence == "high"
                    and not seed_checkpoint.unresolved_fields
                )
                component_unresolved = bool(
                    seed_checkpoint is None
                    or not seed_checkpoint.root_cause_component
                    or "root_cause_component" in seed_checkpoint.unresolved_fields
                )
                explicit_followup_requested = bool(
                    seed_checkpoint is not None
                    and seed_checkpoint.followup_domains
                )
                weak_checkpoint = bool(
                    seed_checkpoint is None
                    or seed_checkpoint.confidence != "high"
                    or seed_checkpoint.unresolved_fields
                )
                requested_unknowns = any(
                    value == "unknown"
                    for value in buildspec.uncertainty.model_dump().values()
                )
                needs_focused_verification = bool(
                    explicit_followup_requested
                    or (component_unresolved and len(support) < 2)
                    or (weak_checkpoint and len(support) < 2)
                )
                may_expand = (
                    requested_unknowns
                    and expansion_budget > 0
                    and not checkpoint_complete
                    and needs_focused_verification
                )
                if component and may_expand:
                    remaining = expansion_budget
                    requested_domains = (
                        list(dict.fromkeys(seed_checkpoint.followup_domains))
                        if explicit_followup_requested and seed_checkpoint is not None
                        else [
                            discovered_by_template[item][0].domain
                            for item in template_order
                        ]
                    )
                    for requested_domain in requested_domains:
                        if remaining <= 0:
                            break
                        matching_template = next(
                            (
                                discovered_by_template[item]
                                for item in template_order
                                if discovered_by_template[item][0].domain == requested_domain
                            ),
                            None,
                        )
                        if matching_template is None:
                            continue
                        exp_template, exp_targets = matching_template
                        # A coordinator-requested domain is intentionally allowed
                        # to revisit the same files with a component focus. The
                        # task signature includes that focus, so this remains one
                        # bounded verification rather than recursive duplication.
                        if exp_template.domain in support and not explicit_followup_requested:
                            continue
                        relevant_targets = _targets_for_component_need(
                            template=exp_template,
                            targets=exp_targets,
                            component=component,
                        )
                        ok = _enqueue_task(
                            template=exp_template,
                            target_paths=tuple(relevant_targets),
                            component_focus=component,
                            origin=f"focused_followup:{instance_name}",
                            instantiated_count_now=instantiated_count,
                        )
                        if ok:
                            remaining -= 1
                            enqueued_now += 1
                            expansion_tasks_enqueued += 1
                elif component:
                    self._emit(
                        on_event,
                        phase="expansion_skipped",
                        recipient="Runtime",
                        content=(
                            f"No focused follow-up needed for {component}: "
                            + (
                                "the first-pass synthesis is complete with high confidence."
                                if checkpoint_complete
                                else "the checkpoint is sufficiently corroborated across domains."
                                if not needs_focused_verification
                                else f"corroborated by {len(support)} telemetry domains."
                            )
                        ),
                    )
            if enqueued_now > 0:
                self._emit(
                    on_event,
                    phase="expand_enqueue",
                    recipient="ExecutorAgent",
                    content=(
                        f"Enqueued {enqueued_now}/{expansion_budget} allowed focused "
                        f"follow-up task(s) from {instance_name}."
                    ),
                    sender=instance_name,
                )

        if not cap_reached:
            self._emit(
                on_event,
                phase="converged",
                recipient="Runtime",
                content=(
                    "Controlled adaptive queue converged: initial telemetry domains were "
                    "covered and no justified focused follow-up remained."
                ),
            )

        if cap_reached:
            self._emit(
                on_event,
                phase="cap_reached",
                recipient="Runtime",
                content=f"Reached max_agents={cap}; remaining discovered targets were skipped.",
            )

        coordinator_decision = seed_checkpoint
        if expansion_tasks_enqueued > 0:
            coordinator_decision = _coordinate_findings(
                llm_client=self.llm_client,
                buildspec=buildspec,
                findings=findings,
            ) or seed_checkpoint
            if coordinator_decision is not None:
                self._emit(
                    on_event,
                    phase="final_synthesis_checkpoint",
                    recipient="Runtime",
                    content=(
                        f"Final synthesis: confidence={coordinator_decision.confidence}; "
                        f"component={coordinator_decision.root_cause_component or 'unresolved'}; "
                        f"reason={coordinator_decision.root_cause_reason or 'unresolved'}; "
                        f"time={coordinator_decision.root_cause_time or 'unresolved'}."
                    ),
                )

        preliminary_causes = _derive_preliminary_causes(findings)
        confidence = _estimate_confidence(findings, preliminary_causes)
        summary = (
            f"Executor completed Assess: {len(findings)} findings, "
            f"{len([f for f in findings if f.kind == 'anomaly'])} anomalies, "
            f"{len(preliminary_causes)} preliminary causes, "
            f"{expansion_tasks_enqueued} expansion task(s) enqueued."
        )
        self._emit(
            on_event,
            phase="summary",
            recipient="Runtime",
            content=summary,
        )
        if self.enable_memory and knowledge_store and run_id:
            knowledge_store.finish_run(
                run_id=run_id,
                status="success",
                summary=summary,
                confidence=confidence,
                preliminary_causes=preliminary_causes,
            )

        causal_graph = build_causal_graph(findings, coordinator_decision)
        return ExecutorRunResult(
            buildspec=buildspec,
            agents_instantiated=agents_instantiated,
            task_results=task_results,
            findings=findings,
            preliminary_causes=preliminary_causes,
            confidence=confidence,
            coordinator_decision=coordinator_decision,
            execution_mode=self.execution_mode,
            causal_graph=causal_graph,
            summary=summary,
        )

    def _emit(
        self,
        callback: Callable[[ExecutorEvent], None] | None,
        *,
        phase: str,
        recipient: str,
        content: str,
        sender: str | None = None,
    ) -> None:
        if callback is None:
            return
        callback(
            ExecutorEvent(
                sender=sender or self.name,
                recipient=recipient,
                phase=phase,
                content=content,
            )
        )


def _decide_findings_with_llm(
    *,
    llm_client: LLMClient,
    template: AgentTemplate,
    target_path: str,
    buildspec: BuildSpec,
    tool_context: dict[str, Any],
    agent_name: str,
    knowledge_text: str,
    known_components: list[str],
    known_reasons: list[str],
    shared_memory_context: list[str],
    reasoning_enabled: bool,
) -> tuple[list[AssessFinding], str]:
    system_prompt = (
        f"You are {agent_name}.\n"
        f"Role: {template.role}\n"
        f"Objective: {template.objective}\n"
        "You are an RCA assessor. Decide findings strictly from provided tool outputs.\n"
        "Use semantic_columns and semantic_summary as primary evidence over raw sample_lines.\n"
        "When tool outputs contain a sources array, compare all sources and return one consolidated domain decision.\n"
        "Do not report CSV column names (for example PodName, cmdb_id, serviceName) as components.\n"
        "Infer component/reason cues from the detected column roles and in-window stats.\n"
        "Root cause components must come from telemetry values (cmdb_id/tc/kpi context), never file names.\n"
        "If component candidates exist in semantic_summary.top_component_values, cite them explicitly in findings.\n"
        "Use executor_knowledge for schema understanding and candidate component/reason examples.\n"
        "The reason examples are non-exhaustive: propose a novel cause when tool evidence supports it.\n"
        "Never describe components or reasons as 'allowed'; absence from prior knowledge is not rejection.\n"
        "Do not invent evidence outside tool_context.\n"
        "Use shared_memory_context as prior agent observations from the same run.\n"
        f"Reasoning mode: {'deep' if reasoning_enabled else 'fast'}.\n"
        "Return JSON only with keys: summary, findings.\n"
        "Each finding: kind (anomaly|observation), summary, severity (low|medium|high), evidence (list[str])."
        "\nexecutor_knowledge:\n"
        f"{knowledge_text}"
    )
    user_prompt = (
        "assess_task_context:\n"
        f"- template_id={template.template_id}\n"
        f"- target_path={target_path}\n"
        f"- failure_window_start={buildspec.failure_time_range_ts.start}\n"
        f"- failure_window_end={buildspec.failure_time_range_ts.end}\n"
        f"- known_components={json.dumps(known_components, ensure_ascii=False)}\n"
        f"- non_exhaustive_reason_examples={json.dumps(known_reasons, ensure_ascii=False)}\n"
        f"- shared_memory_context={json.dumps(shared_memory_context[:25], ensure_ascii=False)}\n"
        "tool_context_json:\n"
        f"{json.dumps(tool_context, ensure_ascii=False)}"
    )
    try:
        response = llm_client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format=_DECISION_RESPONSE_FORMAT,
        )
    except (LLMError, TypeError):
        return [], ""
    payload = _extract_json_payload(response)
    if payload is None:
        return [], ""
    findings_payload = payload.get("findings")
    if not isinstance(findings_payload, list):
        return [], str(payload.get("summary", ""))
    findings: list[AssessFinding] = []
    for item in findings_payload:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "observation")).strip().lower()
        severity = str(item.get("severity", "medium")).strip().lower()
        summary = str(item.get("summary", "")).strip()
        evidence = item.get("evidence", [])
        if kind not in {"anomaly", "observation"}:
            kind = "observation"
        if severity not in {"low", "medium", "high"}:
            severity = "medium"
        if not isinstance(evidence, list):
            evidence = []
        evidence_str = [str(entry)[:300] for entry in evidence if str(entry).strip()]
        if not summary:
            continue
        findings.append(
            AssessFinding(
                agent=agent_name,
                kind=kind,  # type: ignore[arg-type]
                source=target_path,
                summary=summary,
                evidence=evidence_str[:5],
                severity=severity,  # type: ignore[arg-type]
            )
        )
    return findings, str(payload.get("summary", ""))


def _fallback_findings_from_tools(
    domain: str,
    view: telemetry_tools.CsvWindowView,
    source_path: str,
    agent_name: str,
) -> list[AssessFinding]:
    if view.timestamp_field is not None and view.window_rows == 0:
        return [
            AssessFinding(
                agent=agent_name,
                kind="observation",
                source=source_path,
                summary=(
                    f"No {domain} rows found inside failure_time_range_ts; "
                    "strict window filtering yielded zero rows."
                ),
                evidence=[f"timestamp_field={view.timestamp_field}"],
                severity="low",
            )
        ]

    lines = view.lines
    text = "\n".join(lines).lower()
    if domain == "logs":
        timeout_count = telemetry_tools.count_matches(text, r"timeout|timed out|deadline exceeded")
        error_count = telemetry_tools.count_matches(text, r"\berror\b|exception|failed|failure")
        retry_count = telemetry_tools.count_matches(text, r"\bretry\b")
        findings: list[AssessFinding] = []
        if timeout_count > 0:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=f"Detected {timeout_count} timeout indicators in logs.",
                    evidence=telemetry_tools.sample_matching_lines(lines, r"timeout|timed out|deadline exceeded"),
                    severity="high" if timeout_count >= 3 else "medium",
                )
            )
        if error_count > 0:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=f"Detected {error_count} error/exception indicators in logs.",
                    evidence=telemetry_tools.sample_matching_lines(lines, r"\berror\b|exception|failed|failure"),
                    severity="high" if error_count >= 5 else "medium",
                )
            )
        if retry_count > 0:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary=f"Detected {retry_count} retry indicators in logs.",
                    evidence=telemetry_tools.sample_matching_lines(lines, r"\bretry\b"),
                    severity="low",
                )
            )
        if not findings:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary="No explicit timeout/error patterns found in logs.",
                    evidence=lines[:3],
                    severity="low",
                )
            )
        return findings

    if domain == "trace":
        timeout_count = telemetry_tools.count_matches(text, r"timeout|timed out|deadline exceeded")
        retry_count = telemetry_tools.count_matches(text, r"\bretry\b")
        durations_ms = telemetry_tools.extract_trace_durations(view.rows)
        if not durations_ms:
            durations_ms = [int(item) for item in re.findall(r"(\d+)\s*ms", text)]
        max_duration = max(durations_ms) if durations_ms else 0
        findings = []
        network_summary = telemetry_tools.summarize_inter_service_network_latency(view.rows)
        network_anomalies = [
            item for item in network_summary if float(item.get("p90_ms", 0.0)) >= 300.0
        ]
        for item in network_anomalies[:3]:
            component = str(item.get("component", "unknown"))
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=(
                        "Inter-service network delay detected for component "
                        f"{component} (p90={item.get('p90_ms')}ms)."
                    ),
                    evidence=[
                        f"component={component}",
                        "candidate_reason=network delay",
                        f"network_p90_ms={item.get('p90_ms')}",
                        f"network_max_ms={item.get('max_ms')}",
                        f"peak_ts={item.get('peak_ts')}",
                    ],
                    severity="high",
                )
            )
        if timeout_count > 0:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=f"Trace contains {timeout_count} timeout indicators.",
                    evidence=telemetry_tools.sample_matching_lines(lines, r"timeout|timed out|deadline exceeded"),
                    severity="high" if timeout_count >= 2 else "medium",
                )
            )
        if max_duration >= 500:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=f"Trace shows slow span(s), max observed duration={max_duration}ms.",
                    evidence=telemetry_tools.sample_matching_lines(lines, r"\d+\s*ms"),
                    severity="high" if max_duration >= 1200 else "medium",
                )
            )
        if retry_count > 0:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary=f"Trace contains {retry_count} retry indicators.",
                    evidence=telemetry_tools.sample_matching_lines(lines, r"\bretry\b"),
                    severity="low",
                )
            )
        if not findings:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary="No obvious timeout/latency trace anomalies detected.",
                    evidence=lines[:3],
                    severity="low",
                )
            )
        return findings

    max_cpu = telemetry_tools.max_numeric_column(view.rows, ("cpu", "cpu_usage", "cpu_percent", "cpu%"))
    max_latency = telemetry_tools.max_numeric_column(
        view.rows,
        ("latency", "p95", "duration_ms", "response_time", "mrt"),
    )
    max_error_rate = telemetry_tools.max_numeric_column(view.rows, ("error_rate", "errors_rate", "5xx_rate", "5xx"))
    if max_error_rate is None:
        min_sr = telemetry_tools.min_numeric_column(view.rows, ("sr", "success_rate"))
        if min_sr is not None:
            max_error_rate = max(0.0, 100.0 - min_sr)
    if max_cpu is None:
        max_cpu = telemetry_tools.max_value_after_keywords(text, ("cpu", "cpu_usage", "cpu%"))
    if max_latency is None:
        max_latency = telemetry_tools.max_value_after_keywords(text, ("latency", "p95", "duration_ms", "response_time"))
    if max_error_rate is None:
        max_error_rate = telemetry_tools.max_value_after_keywords(text, ("error_rate", "errors_rate", "5xx_rate", "5xx"))

    findings = []
    if max_cpu is not None and max_cpu >= 85:
        findings.append(
            AssessFinding(
                agent=agent_name,
                kind="anomaly",
                source=source_path,
                summary=f"CPU saturation signal detected (max={max_cpu:.1f}%).",
                evidence=telemetry_tools.sample_matching_lines(lines, r"cpu"),
                severity="high" if max_cpu >= 92 else "medium",
            )
        )
    if max_latency is not None and max_latency >= 300:
        findings.append(
            AssessFinding(
                agent=agent_name,
                kind="anomaly",
                source=source_path,
                summary=f"Latency spike detected (max={max_latency:.1f}ms).",
                evidence=telemetry_tools.sample_matching_lines(lines, r"latency|p95|duration|response_time|mrt"),
                severity="high" if max_latency >= 800 else "medium",
            )
        )
    if max_error_rate is not None and max_error_rate >= 5:
        findings.append(
            AssessFinding(
                agent=agent_name,
                kind="anomaly",
                source=source_path,
                summary=f"Error-rate spike detected (max={max_error_rate:.1f}).",
                evidence=telemetry_tools.sample_matching_lines(lines, r"error|5xx|sr"),
                severity="high" if max_error_rate >= 10 else "medium",
            )
        )
    if not findings:
        findings.append(
            AssessFinding(
                agent=agent_name,
                kind="observation",
                source=source_path,
                summary="No obvious CPU/latency/error-rate spikes detected in metrics.",
                evidence=lines[:3],
                severity="low",
            )
        )
    return findings


def _compact_domain_findings(
    findings: list[AssessFinding],
    *,
    max_anomalies: int = 10,
    max_observations: int = 6,
) -> list[AssessFinding]:
    """Keep a bounded, evidence-rich result from one consolidated domain task."""
    deduped: list[AssessFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        key = (finding.kind, finding.source, finding.summary)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)

    severity_score = {"low": 1, "medium": 2, "high": 3}

    def priority(item: AssessFinding) -> tuple[int, int, int]:
        merged = " ".join([item.summary, *item.evidence]).lower()
        explicit_component = int("component=" in merged or "candidate_reason=" in merged)
        numeric_signal = int(
            any(token in merged for token in ("cpu", "latency", "error", "timeout", "memory"))
        )
        return (severity_score[item.severity], explicit_component, numeric_signal)

    anomalies = sorted(
        (item for item in deduped if item.kind == "anomaly"),
        key=priority,
        reverse=True,
    )[:max_anomalies]
    observations = sorted(
        (item for item in deduped if item.kind == "observation"),
        key=lambda item: (
            int(item.summary.startswith("Top component identifiers")),
            int("timestamp bounds" in item.summary.lower()),
            priority(item),
        ),
        reverse=True,
    )[:max_observations]
    return [*anomalies, *observations]


def _has_decisive_structured_metric(findings: list[AssessFinding]) -> bool:
    """Return true only for one unambiguous long-form KPI hypothesis."""
    hypotheses: dict[tuple[str, str], int] = {}
    for finding in findings:
        if finding.kind != "anomaly" or not finding.agent.startswith("MetricsAgent"):
            continue
        merged = " ".join([finding.summary, *finding.evidence])
        if "kpi_range=" not in merged:
            continue
        component = re.search(r"\bcomponent\s*=\s*([A-Za-z0-9_.:-]+)", merged, re.IGNORECASE)
        reason = re.search(
            r"candidate_reason\s*=\s*([^,;|\n]+?)(?=\s+[A-Za-z_]+=|$)",
            merged,
            re.IGNORECASE,
        )
        spread = re.search(r"kpi_range\s*=\s*([0-9]+(?:\.[0-9]+)?)", merged, re.IGNORECASE)
        if not component or not reason or not spread or float(spread.group(1)) < 5.0:
            continue
        key = (component.group(1).lower(), reason.group(1).strip().lower())
        support = re.search(r"support_count\s*=\s*([0-9]+)", merged, re.IGNORECASE)
        hypotheses[key] = max(
            hypotheses.get(key, 0),
            int(support.group(1)) if support else 0,
        )
    if len(hypotheses) == 1:
        return True
    ranked = sorted(hypotheses.items(), key=lambda item: item[1], reverse=True)
    return bool(
        len(ranked) >= 2
        and ranked[0][1] >= 3
        and ranked[0][1] > ranked[1][1]
        and len({reason for (_, reason), _ in ranked}) == 1
    )


def _has_decisive_direct_log_failure(findings: list[AssessFinding]) -> bool:
    """Stop before traces when one repeated local return/exception is explicit."""
    hypotheses: set[tuple[str, str]] = set()
    for finding in findings:
        if finding.kind != "anomaly" or not finding.agent.startswith("LogsAgent"):
            continue
        merged = " ".join([finding.summary, *finding.evidence])
        if "propagated_failure=false" not in merged.lower():
            continue
        count = re.search(r"failure_signature_count\s*=\s*([0-9]+)", merged, re.IGNORECASE)
        component = re.search(r"\bcomponent\s*=\s*([A-Za-z0-9_.:-]+)", merged, re.IGNORECASE)
        reason = re.search(
            r"candidate_reason\s*=\s*([^,;|\n]+?)(?=\s+[A-Za-z_]+=|$)",
            merged,
            re.IGNORECASE,
        )
        if not count or int(count.group(1)) < 20 or not component or not reason:
            continue
        hypotheses.add((component.group(1).lower(), reason.group(1).strip().lower()))
    return len(hypotheses) == 1


def _extract_json_payload(text: str) -> dict[str, object] | None:
    content = text.strip()
    if not content:
        return None
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    snippet = content[start : end + 1]
    try:
        payload = json.loads(snippet)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _coordinate_findings(
    *,
    llm_client: LLMClient,
    buildspec: BuildSpec,
    findings: list[AssessFinding],
) -> CoordinatorDecision | None:
    """Synthesize a checkpoint from bounded cross-domain evidence."""
    severity_order = {"high": 3, "medium": 2, "low": 1}
    ordered = sorted(
        findings,
        key=lambda item: (item.kind == "anomaly", severity_order[item.severity]),
        reverse=True,
    )
    evidence = [
        {
            "agent": item.agent,
            "domain": (
                "metrics"
                if item.agent.startswith("MetricsAgent")
                else "trace"
                if item.agent.startswith("TraceAgent")
                else "logs"
            ),
            "kind": item.kind,
            "severity": item.severity,
            "summary": item.summary,
            "evidence": item.evidence[:4],
            "source": item.source,
        }
        for item in ordered[:36]
    ]
    system_prompt = (
        "You are the RCA coordinator. Reconcile bounded findings from logs, traces, and metrics.\n"
        "Return the best component, reason, and UNIX occurrence timestamp supported by evidence.\n"
        "Never use a CSV column name such as PodName, cmdb_id, serviceName, component, or node as the component value.\n"
        "Prefer explicit component=<value>, candidate_reason=<value>, anomaly peaks, and agreement across domains.\n"
        "The reason vocabulary is open and non-exhaustive; preserve the telemetry-supported wording.\n"
        "root_cause_reason must be one concise cause label only, never a sentence or combined explanation.\n"
        "When candidate_reason=<value> is supported, copy that value exactly into root_cause_reason and put details in rationale.\n"
        "Use empty strings and root_cause_time=0 for unresolved values.\n"
        "Request follow-up domains only for genuinely unresolved or weak fields; otherwise return an empty list.\n"
    )
    user_prompt = json.dumps(
        {
            "window": {
                "start": buildspec.failure_time_range_ts.start,
                "end": buildspec.failure_time_range_ts.end,
            },
            "requested_unknowns": buildspec.uncertainty.model_dump(),
            "findings": evidence,
        },
        ensure_ascii=False,
    )
    try:
        response = llm_client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format=_COORDINATOR_RESPONSE_FORMAT,
        )
    except LLMError:
        return None
    payload = _extract_json_payload(response)
    if payload is None:
        return None
    try:
        decision = CoordinatorDecision.model_validate(payload)
    except Exception:
        return None

    forbidden = {
        "podname",
        "pod_name",
        "cmdb_id",
        "servicename",
        "service_name",
        "component",
        "node",
        "unknown",
    }
    updates: dict[str, object] = {}
    if decision.root_cause_component.strip().lower() in forbidden:
        updates["root_cause_component"] = ""
        updates["unresolved_fields"] = list(
            dict.fromkeys([*decision.unresolved_fields, "root_cause_component"])
        )
    if not (
        buildspec.failure_time_range_ts.start
        <= decision.root_cause_time
        <= buildspec.failure_time_range_ts.end
    ):
        updates["root_cause_time"] = 0
        updates["unresolved_fields"] = list(
            dict.fromkeys(
                [
                    *list(updates.get("unresolved_fields", decision.unresolved_fields)),
                    "root_cause_time",
                ]
            )
        )
    explicit_reasons: list[str] = []
    explicit_hypotheses: list[tuple[int, str, str, int]] = []
    for finding in findings:
        finding_component = ""
        finding_reason = ""
        for text in [finding.summary, *finding.evidence]:
            component_match = re.search(
                r"\bcomponent\s*=\s*([A-Za-z0-9_.:-]+)",
                text,
                flags=re.IGNORECASE,
            )
            if component_match:
                finding_component = component_match.group(1).strip()
            match = re.search(
                r"candidate_reason\s*=\s*([^,;|\n]+)",
                text,
                flags=re.IGNORECASE,
            )
            if match:
                finding_reason = match.group(1).strip()
                explicit_reasons.append(finding_reason)
        if finding.kind == "anomaly" and finding_component and finding_reason:
            score = {"low": 1, "medium": 2, "high": 3}[finding.severity]
            if finding.agent.startswith("MetricsAgent"):
                score += 1
            numeric_strengths = [
                float(value)
                for value in re.findall(
                    r"(?:pod_cpu_usage_rate_max|max)\s*=\s*([0-9]+(?:\.[0-9]+)?)",
                    " ".join([finding.summary, *finding.evidence]),
                    flags=re.IGNORECASE,
                )
            ]
            numeric_strength = max(numeric_strengths, default=0.0)
            if numeric_strength >= 95.0:
                score += 4
            elif numeric_strength >= 80.0:
                score += 2
            failure_counts = [
                int(value)
                for value in re.findall(
                    r"failure_signature_count\s*=\s*([0-9]+)",
                    " ".join([finding.summary, *finding.evidence]),
                    flags=re.IGNORECASE,
                )
            ]
            failure_count = max(failure_counts, default=0)
            if failure_count >= 20:
                score += 4
            elif failure_count >= 3:
                score += 2
            merged_hypothesis = " ".join([finding.summary, *finding.evidence]).lower()
            if "propagated_failure=true" in merged_hypothesis:
                score -= 2
            support_counts = [
                int(value)
                for value in re.findall(r"support_count\s*=\s*([0-9]+)", merged_hypothesis)
            ]
            score += min(max(support_counts, default=0), 5)
            expert_scores = [
                float(value)
                for value in re.findall(
                    r"expert_score\s*=\s*([0-9]+(?:\.[0-9]+)?)",
                    merged_hypothesis,
                )
            ]
            if "independent_evidence=true" in merged_hypothesis:
                score += 3 + min(int(max(expert_scores, default=0.0)), 7)
            hypothesis_times = [
                int(value)
                for value in re.findall(
                    r"(?:peak_ts|first_failure_ts)\s*=\s*([0-9]+)",
                    " ".join([finding.summary, *finding.evidence]),
                    flags=re.IGNORECASE,
                )
            ]
            explicit_hypotheses.append(
                (score, finding_component, finding_reason, min(hypothesis_times, default=0))
            )
    decision_reason = decision.root_cause_reason.strip().lower()
    matching_explicit = next(
        (
            reason
            for reason in explicit_reasons
            if reason.lower() in decision_reason or decision_reason in reason.lower()
        ),
        None,
    )
    if matching_explicit:
        updates["root_cause_reason"] = matching_explicit
    if explicit_hypotheses:
        grouped_hypotheses: dict[tuple[str, str], list[tuple[int, str, str, int]]] = {}
        for hypothesis in explicit_hypotheses:
            _, component, reason, _ = hypothesis
            grouped_hypotheses.setdefault(
                (component.lower(), reason.lower()), []
            ).append(hypothesis)
        ranked_hypotheses: list[tuple[int, str, str, int]] = []
        for hypotheses in grouped_hypotheses.values():
            best = max(hypotheses, key=lambda item: item[0])
            # Repeated shards corroborate a signal, but boundedly: duplicate
            # evidence must not overpower a more specific failure signature.
            corroboration_bonus = min(len(hypotheses) - 1, 2)
            ranked_hypotheses.append(
                (
                    best[0] + corroboration_bonus,
                    best[1],
                    best[2],
                    min((item[3] for item in hypotheses if item[3]), default=0),
                )
            )
        explicit_score, explicit_component, explicit_reason, explicit_time = max(
            ranked_hypotheses,
            key=lambda item: item[0],
        )
        updates["root_cause_component"] = explicit_component
        updates["root_cause_reason"] = explicit_reason
        updates["focus_component"] = explicit_component
        if buildspec.failure_time_range_ts.start <= explicit_time <= buildspec.failure_time_range_ts.end:
            updates["root_cause_time"] = explicit_time
        unresolved = [
            field
            for field in list(
                updates.get("unresolved_fields", decision.unresolved_fields)
            )
            if field not in {"root_cause_component", "root_cause_reason"}
        ]
        updates["unresolved_fields"] = unresolved
        updates["confidence"] = "high" if explicit_score >= 3 else decision.confidence
        updates["rationale"] = (
            "Structured anomaly evidence takes precedence over indirect symptoms: "
            f"component={explicit_component}, candidate_reason={explicit_reason}."
        )
    return decision.model_copy(update=updates) if updates else decision


def _semantic_context_findings(
    *,
    domain: str,
    tool_context: dict[str, Any],
    source_path: str,
    agent_name: str,
    known_components: list[str],
) -> list[AssessFinding]:
    """Convert structured schema/window context into explicit findings."""
    semantic_columns = tool_context.get("semantic_columns", {})
    semantic_summary = tool_context.get("semantic_summary", {})
    if not isinstance(semantic_columns, dict) or not isinstance(semantic_summary, dict):
        return []

    findings: list[AssessFinding] = []

    component_columns = semantic_columns.get("component_columns", [])
    reason_columns = semantic_columns.get("reason_columns", [])
    duration_columns = semantic_columns.get("duration_columns", [])
    top_components = semantic_summary.get("top_component_values", [])
    top_reasons = semantic_summary.get("top_reason_values", [])
    numeric_ranges = semantic_summary.get("numeric_ranges", {})
    observed_bounds = semantic_summary.get("window_observed_time_bounds", {})
    network_latency = tool_context.get("inter_service_network_latency", [])
    log_failure_signals = tool_context.get("log_failure_signals", [])
    long_form_kpi_anomalies = tool_context.get("long_form_kpi_anomalies", [])
    service_latency_anomalies = tool_context.get("service_latency_anomalies", [])

    if domain == "metrics" and isinstance(numeric_ranges, dict):
        pod_cpu_max: float | None = None
        node_cpu_max: float | None = None
        for column, stats in numeric_ranges.items():
            normalized_column = telemetry_tools.normalize_header(str(column)).replace("_", "")
            if "cpuusagerate" not in normalized_column:
                continue
            if not isinstance(stats, dict):
                continue
            try:
                value = float(stats.get("max"))
            except (TypeError, ValueError):
                continue
            if "node" in normalized_column:
                node_cpu_max = value if node_cpu_max is None else max(node_cpu_max, value)
            else:
                pod_cpu_max = value if pod_cpu_max is None else max(pod_cpu_max, value)
        component = ""
        if isinstance(top_components, list):
            component = next(
                (
                    str(item.get("value", "")).strip()
                    for item in top_components
                    if isinstance(item, dict) and str(item.get("value", "")).strip()
                ),
                "",
            )
        if pod_cpu_max is not None and pod_cpu_max >= 70.0 and component:
            cpu_reason = (
                "cpu consumed"
                if pod_cpu_max >= 95.0 and node_cpu_max is not None and node_cpu_max < 10.0
                else "cpu contention"
            )
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=(
                        f"Elevated pod CPU indicates {cpu_reason} for component {component} "
                        f"(max={pod_cpu_max:.3f}%)."
                    ),
                    evidence=[
                        f"component={component}",
                        f"pod_cpu_usage_rate_max={pod_cpu_max:.6f}",
                        f"node_cpu_usage_rate_max={node_cpu_max:.6f}" if node_cpu_max is not None else "node_cpu_usage_rate_max=unknown",
                        f"candidate_reason={cpu_reason}",
                    ],
                    severity="high" if pod_cpu_max >= 80.0 else "medium",
                )
            )

    if domain == "logs" and isinstance(log_failure_signals, list):
        for signal in log_failure_signals:
            if not isinstance(signal, dict) or int(signal.get("count", 0)) < 3:
                continue
            category = str(signal.get("category", ""))
            component = str(signal.get("component", "")).strip()
            if not component or category not in {"early_return", "exception"}:
                continue
            reason = "return" if category == "early_return" else "exception"
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=(
                        f"Repeated {category.replace('_', ' ')} signature identifies "
                        f"component {component} (count={signal.get('count')})."
                    ),
                    evidence=[
                        f"component={component}",
                        f"candidate_reason={reason}",
                        f"failure_signature_count={signal.get('count')}",
                        f"propagated_failure={str(bool(signal.get('propagated'))).lower()}",
                        f"first_failure_ts={signal.get('first_ts')}",
                        f"sample={signal.get('sample', '')}",
                    ],
                    severity="high",
                )
            )

    if domain == "metrics" and isinstance(long_form_kpi_anomalies, list):
        for signal in long_form_kpi_anomalies:
            if not isinstance(signal, dict):
                continue
            component = str(signal.get("component", "")).strip()
            reason = str(signal.get("reason", "")).strip()
            if not component or not reason:
                continue
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=(
                        f"Explicit KPI threshold identifies {reason} on component {component}."
                    ),
                    evidence=[
                        f"component={component}",
                        f"candidate_reason={reason}",
                        f"kpi_name={signal.get('kpi_name')}",
                        f"kpi_value={signal.get('value')}",
                        f"kpi_min={signal.get('minimum')}",
                        f"kpi_range={signal.get('range')}",
                        f"support_count={signal.get('support_count')}",
                        f"peak_ts={signal.get('timestamp')}",
                    ],
                    severity="high",
                )
            )

    if domain == "metrics" and isinstance(service_latency_anomalies, list):
        for signal in service_latency_anomalies:
            if not isinstance(signal, dict):
                continue
            component = str(signal.get("component", "")).strip()
            if not component:
                continue
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=f"Earliest service latency surge identifies component {component}.",
                    evidence=[
                        f"component={component}",
                        "candidate_reason=container network latency",
                        f"latency_baseline={signal.get('baseline')}",
                        f"latency_peak={signal.get('peak')}",
                        f"kpi_range={float(signal.get('peak', 0.0)) - float(signal.get('baseline', 0.0))}",
                        f"support_count={signal.get('support_count')}",
                        f"peak_ts={signal.get('onset_ts')}",
                    ],
                    severity="high",
                )
            )

    if domain == "trace" and isinstance(network_latency, list):
        for item in network_latency:
            if not isinstance(item, dict) or float(item.get("p90_ms", 0.0)) < 200.0:
                continue
            component = str(item.get("component", "unknown"))
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="anomaly",
                    source=source_path,
                    summary=(
                        f"Inter-service network delay detected for component {component} "
                        f"(p90={item.get('p90_ms')}ms)."
                    ),
                    evidence=[
                        f"component={component}",
                        "candidate_reason=network delay",
                        f"network_p90_ms={item.get('p90_ms')}",
                        f"network_max_ms={item.get('max_ms')}",
                        f"peak_ts={item.get('peak_ts')}",
                    ],
                    severity="high",
                )
            )

    if component_columns or reason_columns or duration_columns:
        findings.append(
            AssessFinding(
                agent=agent_name,
                kind="observation",
                source=source_path,
                summary=(
                    "CSV header semantic mapping extracted "
                    f"(component={component_columns}, reason={reason_columns}, duration={duration_columns})."
                ),
                evidence=[
                    f"domain={domain}",
                    f"fieldnames={tool_context.get('fieldnames', [])}",
                ],
                severity="low",
            )
        )

    if isinstance(top_components, list) and top_components:
        values: list[str] = []
        for item in top_components[:8]:
            if not isinstance(item, dict):
                continue
            raw_value = str(item.get("value", "")).strip()
            raw_column = str(item.get("column", "")).strip()
            raw_count = item.get("count", "")
            if not raw_value:
                continue
            if known_components and raw_value not in known_components:
                # Keep unknown values too, but annotate whether it belongs to known component set.
                values.append(f"{raw_value} (column={raw_column}, count={raw_count}, known_component=no)")
            else:
                values.append(f"{raw_value} (column={raw_column}, count={raw_count}, known_component=yes)")
        if values:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary="Top component identifiers observed in-window.",
                    evidence=values[:6],
                    severity="low",
                )
            )

    if isinstance(top_reasons, list) and top_reasons:
        reason_values: list[str] = []
        for item in top_reasons[:6]:
            if not isinstance(item, dict):
                continue
            reason_values.append(
                f"{item.get('value')} (column={item.get('column')}, count={item.get('count')})"
            )
        if reason_values:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary="Top reason/message-like values observed in-window.",
                    evidence=reason_values[:6],
                    severity="low",
                )
            )

    if isinstance(numeric_ranges, dict) and numeric_ranges:
        compact_ranges = []
        for key, stats in list(numeric_ranges.items())[:6]:
            if not isinstance(stats, dict):
                continue
            compact_ranges.append(
                f"{key}: min={stats.get('min')}, max={stats.get('max')}, count={stats.get('count')}"
            )
        if compact_ranges:
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary="Numeric column ranges computed over in-window rows.",
                    evidence=compact_ranges,
                    severity="low",
                )
            )

    if isinstance(observed_bounds, dict):
        min_ts = observed_bounds.get("min_ts")
        max_ts = observed_bounds.get("max_ts")
        if isinstance(min_ts, int) and isinstance(max_ts, int):
            findings.append(
                AssessFinding(
                    agent=agent_name,
                    kind="observation",
                    source=source_path,
                    summary=f"Observed in-window timestamp bounds: min_ts={min_ts}, max_ts={max_ts}.",
                    evidence=[
                        f"window_observed_time_bounds.min_ts={min_ts}",
                        f"window_observed_time_bounds.max_ts={max_ts}",
                    ],
                    severity="low",
                )
            )

    return findings


def _derive_preliminary_causes(findings: list[AssessFinding]) -> list[str]:
    causes: list[str] = []
    merged = " ".join(item.summary.lower() for item in findings)
    if "timeout" in merged:
        causes.append("Downstream timeout or dependency latency likely contributed to the incident.")
    if "cpu saturation" in merged or "cpu" in merged:
        causes.append("Resource saturation (CPU pressure) likely contributed to degraded performance.")
    if "error/exception" in merged or "exception" in merged:
        causes.append("Application-level errors/exceptions likely contributed to failures.")
    if "latency spike" in merged:
        causes.append("System latency spike observed around the failure window.")
    # The catalogue is deliberately open. Preserve telemetry-backed anomaly
    # descriptions as emerging candidates instead of forcing them into known buckets.
    for finding in findings:
        if finding.kind != "anomaly" or finding.severity not in {"medium", "high"}:
            continue
        candidate = finding.summary.strip()
        if candidate:
            causes.append(candidate)
    if not causes:
        causes.append("Insufficient evidence for strong preliminary causes; gather more telemetry.")
    return _dedupe_preserve_order(causes)


def _load_executor_knowledge(knowledge_file: str) -> tuple[str, str, list[str], list[str]]:
    """Load Executor knowledge text plus parsed known components/reasons."""
    raw = (knowledge_file or "").strip() or "knowledge/executor_rca_v3_kb.md"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    text = _EXECUTOR_KB_FALLBACK
    if candidate.exists() and candidate.is_file():
        try:
            loaded = candidate.read_text(encoding="utf-8").strip()
            if loaded:
                text = loaded
        except Exception:
            pass
    components = _parse_markdown_list_section(text, "POSSIBLE ROOT CAUSE COMPONENTS")
    reasons = _parse_markdown_list_section(text, "POSSIBLE ROOT CAUSE REASONS")
    return str(candidate), text, components, reasons


def _parse_markdown_list_section(text: str, section_name: str) -> list[str]:
    marker = section_name.strip().lower().rstrip(":")
    lines = text.splitlines()
    in_section = False
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        lower = line.lower()
        if lower.startswith("## "):
            current = lower.removeprefix("## ").strip().rstrip(":")
            in_section = current == marker
            continue
        if not in_section:
            continue
        if lower.startswith("## "):
            break
        if line.startswith("- "):
            value = line[2:].strip()
            if value:
                out.append(value)
    return out


def _estimate_confidence(findings: list[AssessFinding], causes: list[str]) -> float:
    anomalies = [item for item in findings if item.kind == "anomaly"]
    high = [item for item in anomalies if item.severity == "high"]
    distinct_agents = len({item.agent for item in findings})
    score = 0.25
    score += min(0.35, 0.1 * len(anomalies))
    score += min(0.2, 0.1 * len(high))
    score += min(0.15, 0.05 * distinct_agents)
    if len(causes) > 1:
        score += 0.05
    return round(min(score, 0.95), 2)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _extract_component_candidates_from_findings(
    findings: list[AssessFinding],
    *,
    known_components: list[str],
    max_items: int = 6,
) -> list[str]:
    """Extract component candidates from findings for dynamic expansion."""
    candidates: list[str] = []
    known = [item.strip() for item in known_components if item.strip()]
    known_lut = {item.lower(): item for item in known}
    stopwords = {"unknown", "none", "n/a", "na", "null", "component", "service"}
    component_column_tokens = {"cmdb_id", "component", "service", "host", "node", "instance", "tc"}

    def add(value: str) -> None:
        token = value.strip().strip(",;:()[]{}")
        if not token:
            return
        low = token.lower()
        if low in stopwords:
            return
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", low):
            return
        canonical = known_lut.get(low, token)
        candidates.append(canonical)

    # Prioritize exact known-component matches found in text.
    if known:
        for finding in findings:
            all_text = "\n".join([finding.summary, *finding.evidence[:10]])
            for comp in known:
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(comp)}(?![A-Za-z0-9_])",
                    all_text,
                    flags=re.IGNORECASE,
                ):
                    add(comp)

    for finding in findings:
        texts = [finding.summary, *finding.evidence[:10]]
        for text in texts:
            if not text:
                continue
            # Semantic evidence format: "Tomcat02 (column=cmdb_id, count=...)"
            m_sem = re.match(r"\s*([A-Za-z0-9_.:-]+)\s*\(column=([A-Za-z0-9_.:-]+)", text)
            if m_sem:
                value = m_sem.group(1)
                column = m_sem.group(2).strip().lower()
                if any(token == column or token in column for token in component_column_tokens):
                    add(value)

            # Key/value patterns: "cmdb_id=Tomcat02", "tc:Tomcat02", "component=..."
            # Ignore "known_component=yes" style metadata keys.
            for m in re.finditer(
                r"(?:cmdb_id|(?<!known_)component|service|host|node|instance|tc)\s*[:=]\s*([A-Za-z0-9_.:-]+)",
                text,
                flags=re.IGNORECASE,
            ):
                add(m.group(1))

    return _dedupe_preserve_order(candidates)[:max_items]


def _finding_mentions_component(finding: AssessFinding, component: str) -> bool:
    """Check whether finding summary/evidence explicitly mentions the component token."""
    token = component.strip()
    if not token:
        return False
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])"
    merged = "\n".join([finding.summary, *finding.evidence[:10]])
    return re.search(pattern, merged, flags=re.IGNORECASE) is not None


def _store_component_memory_from_findings(
    *,
    knowledge_store: SQLiteKnowledgeStore,
    run_id: str,
    findings: list[AssessFinding],
    known_components: list[str],
    max_components: int,
    max_findings_per_component: int,
) -> int:
    """Persist component-scoped memory entries derived from findings."""
    candidates = _extract_component_candidates_from_findings(
        findings,
        known_components=known_components,
        max_items=max_components,
    )
    stored = 0
    seen_keys: set[tuple[str, str, str]] = set()
    for component in candidates:
        matching = [item for item in findings if _finding_mentions_component(item, component)]
        if not matching:
            # Keep at least one weak memory entry for the detected component.
            matching = findings[:1]
        for finding in matching[: max(1, int(max_findings_per_component))]:
            dedupe_key = (component.lower(), finding.source, finding.summary)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            knowledge_store.append_component_memory(
                component=component,
                run_id=run_id,
                agent_name=finding.agent,
                source=finding.source,
                summary=finding.summary,
                evidence=finding.evidence,
                severity=finding.severity,
            )
            stored += 1
    return stored


def _format_component_memory_context(
    entries: list[dict[str, object]],
    *,
    max_items: int,
) -> list[str]:
    """Convert DB component-memory rows into compact shared-memory lines for LLM prompts."""
    out: list[str] = []
    for item in entries[: max(1, int(max_items))]:
        component = str(item.get("component", "")).strip()
        agent = str(item.get("agent_name", "")).strip()
        summary = str(item.get("summary", "")).strip()
        severity = str(item.get("severity", "")).strip()
        source = str(item.get("source", "")).strip()
        evidence = item.get("evidence", [])
        evidence_preview = ""
        if isinstance(evidence, list) and evidence:
            evidence_preview = f" evidence={str(evidence[0])[:120]}"
        line = (
            f"component_memory|component={component}|agent={agent}|severity={severity}|"
            f"source={source}|summary={summary[:180]}{evidence_preview}"
        )
        out.append(line)
    return out


def _discover_targets_for_template(buildspec: BuildSpec, template: AgentTemplate) -> list[Path]:
    """Resolve evidence explicitly selected for this incident.

    A validated BuildSpec is authoritative. An empty list means that the signal family is
    unavailable. Scanning the date directory would both override that meaning and risk
    mixing evidence unrelated to the selected incident.
    """
    raw_primary = getattr(buildspec, template.target_field)
    primary_values: list[str]
    if isinstance(raw_primary, list):
        primary_values = [str(item) for item in raw_primary if str(item).strip()]
    else:
        primary_values = [str(raw_primary)]
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_path(path: Path) -> None:
        key = str(path.resolve() if path.exists() else path)
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    for primary in primary_values:
        add_path(Path(primary))

    return candidates


def _targets_for_component_need(
    *,
    template: AgentTemplate,
    targets: list[Path],
    component: str,
) -> list[Path]:
    """Route a component investigation need only to relevant evidence sources."""
    if template.domain != "metrics":
        # Logs and traces are time-sharded; each shard may contain the component.
        return list(targets)

    component_key = component.strip().lower()
    service_key = re.sub(r"-[a-z0-9]{8,12}-[a-z0-9]{5}$", "", component_key)
    tokens = [token for token in {component_key, service_key} if len(token) >= 3]
    matching = [
        target
        for target in targets
        if any(token in target.name.lower() for token in tokens)
    ]
    # OpenRCA aggregates many components in generic metric files such as
    # metric_container.csv. In that layout the analyzer filters cached rows by
    # semantic component columns rather than by filename.
    return matching or list(targets)


_EXECUTOR_KB_FALLBACK = """
## POSSIBLE ROOT CAUSE REASONS:
- high CPU usage
- high memory usage
- network latency
- network packet loss
- high disk I/O read usage
- high disk space usage
- high JVM CPU load
- JVM Out of Memory (OOM) Heap

## POSSIBLE ROOT CAUSE COMPONENTS:
- apache01
- apache02
- Tomcat01
- Tomcat02
- Tomcat03
- Tomcat04
- MG01
- MG02
- IG01
- IG02
- Mysql01
- Mysql02
- Redis01
- Redis02

## DATA SCHEMA
- metric_app.csv: timestamp,rr,sr,cnt,mrt,tc
- metric_container.csv: timestamp,cmdb_id,kpi_name,value
- trace_span.csv: timestamp,cmdb_id,parent_id,span_id,trace_id,duration
- log_service.csv: log_id,timestamp,cmdb_id,log_name,value

## CLARIFICATION
- Metric timestamps are seconds.
- Trace timestamps are milliseconds.
- Log timestamps are seconds.
- Service/component identity is usually found in cmdb_id or tc columns.
""".strip()
