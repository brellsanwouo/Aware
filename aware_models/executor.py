"""Executor models for Assess runtime execution output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aware_models.buildspec import BuildSpec


class AssessFinding(BaseModel):
    """One extracted observation/anomaly from a specialized agent."""

    agent: str
    kind: Literal["anomaly", "observation"] = "observation"
    source: str
    summary: str
    evidence: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "medium"


class AgentTaskResult(BaseModel):
    """Execution result for one specialized sub-agent."""

    agent_name: str
    target_path: str
    status: Literal["ok", "skipped", "error"] = "ok"
    findings: list[AssessFinding] = Field(default_factory=list)
    detail: str = ""


class CoordinatorDecision(BaseModel):
    """Evidence checkpoint used to decide follow-ups and the final RCA answer."""

    model_config = ConfigDict(extra="forbid")

    root_cause_component: str = ""
    root_cause_reason: str = ""
    root_cause_time: int = 0
    confidence: Literal["low", "medium", "high"] = "low"
    unresolved_fields: list[
        Literal["root_cause_component", "root_cause_reason", "root_cause_time"]
    ] = Field(default_factory=list)
    focus_component: str = ""
    followup_domains: list[Literal["logs", "trace", "metrics"]] = Field(
        default_factory=list
    )
    rationale: str = ""


class CausalGraphNode(BaseModel):
    """One provenance-preserving node in the RCA causal graph."""

    id: str
    kind: Literal["evidence", "hypothesis", "component", "outcome"]
    label: str
    component: str = ""
    reason: str = ""
    timestamp: int = 0
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    source_agent: str = ""
    source: str = ""


class CausalGraphEdge(BaseModel):
    """Directed causal/support relation between two graph nodes."""

    source: str
    target: str
    relation: Literal["observed_on", "supports", "contradicts", "causes", "propagates_to"]
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class CausalGraph(BaseModel):
    """Inspectable causal graph emitted by the V3 evidence pipeline."""

    nodes: list[CausalGraphNode] = Field(default_factory=list)
    edges: list[CausalGraphEdge] = Field(default_factory=list)
    root_node_id: str = ""


class ExecutorRunResult(BaseModel):
    """Structured output of ExecutorAgent (Assess execution stage)."""

    buildspec: BuildSpec
    agents_instantiated: list[str] = Field(default_factory=list)
    task_results: list[AgentTaskResult] = Field(default_factory=list)
    findings: list[AssessFinding] = Field(default_factory=list)
    preliminary_causes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    coordinator_decision: CoordinatorDecision | None = None
    execution_mode: Literal["v2", "v3"] = "v2"
    causal_graph: CausalGraph | None = None
    summary: str
