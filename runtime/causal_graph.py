"""Build an inspectable causal graph without duplicating dependent evidence."""

from __future__ import annotations

import re
from collections import defaultdict

from aware_models.executor import (
    AssessFinding,
    CausalGraph,
    CausalGraphEdge,
    CausalGraphNode,
    CoordinatorDecision,
)

_COMPONENT_RE = re.compile(r"\bcomponent\s*=\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)
_REASON_RE = re.compile(
    r"candidate_reason\s*=\s*([^,;|\n]+?)(?=\s+[A-Za-z_]+=|$)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(?:peak_ts|onset_ts|first_failure_ts)\s*=\s*([0-9]+)", re.IGNORECASE)


def build_causal_graph(
    findings: list[AssessFinding],
    decision: CoordinatorDecision | None,
) -> CausalGraph:
    """Convert findings to evidence→component→hypothesis→outcome relations."""
    nodes: list[CausalGraphNode] = []
    edges: list[CausalGraphEdge] = []
    component_ids: dict[str, str] = {}
    hypothesis_ids: dict[tuple[str, str], str] = {}
    evidence_by_hypothesis: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)

    def component_node(component: str) -> str:
        key = component.lower()
        if key not in component_ids:
            node_id = f"component:{len(component_ids) + 1}"
            component_ids[key] = node_id
            nodes.append(
                CausalGraphNode(
                    id=node_id,
                    kind="component",
                    label=component,
                    component=component,
                    confidence=0.5,
                )
            )
        return component_ids[key]

    for index, finding in enumerate(findings, start=1):
        merged = " ".join([finding.summary, *finding.evidence])
        component_match = _COMPONENT_RE.search(merged)
        reason_match = _REASON_RE.search(merged)
        time_match = _TIME_RE.search(merged)
        component = component_match.group(1).strip() if component_match else ""
        reason = reason_match.group(1).strip() if reason_match else ""
        timestamp = int(time_match.group(1)) if time_match else 0
        confidence = {"low": 0.3, "medium": 0.6, "high": 0.85}[finding.severity]
        evidence_id = f"evidence:{index}"
        nodes.append(
            CausalGraphNode(
                id=evidence_id,
                kind="evidence",
                label=finding.summary,
                component=component,
                reason=reason,
                timestamp=timestamp,
                confidence=confidence,
                source_agent=finding.agent,
                source=finding.source,
            )
        )
        if not component:
            continue
        component_id = component_node(component)
        edges.append(
            CausalGraphEdge(
                source=evidence_id,
                target=component_id,
                relation="observed_on",
                confidence=confidence,
            )
        )
        if not reason:
            continue
        key = (component.lower(), reason.lower())
        if key not in hypothesis_ids:
            hypothesis_id = f"hypothesis:{len(hypothesis_ids) + 1}"
            hypothesis_ids[key] = hypothesis_id
            nodes.append(
                CausalGraphNode(
                    id=hypothesis_id,
                    kind="hypothesis",
                    label=f"{component} · {reason}",
                    component=component,
                    reason=reason,
                    timestamp=timestamp,
                    confidence=confidence,
                )
            )
            edges.append(
                CausalGraphEdge(
                    source=component_id,
                    target=hypothesis_id,
                    relation="causes",
                    confidence=confidence,
                )
            )
        hypothesis_id = hypothesis_ids[key]
        lineage_key = (finding.source, finding.agent.split("_")[0])
        # Duplicate statements from the same agent family and source remain
        # visible, but only the first contributes a support edge.
        if lineage_key not in evidence_by_hypothesis[key]:
            evidence_by_hypothesis[key].add(lineage_key)
            edges.append(
                CausalGraphEdge(
                    source=evidence_id,
                    target=hypothesis_id,
                    relation=(
                        "contradicts"
                        if "counter_evidence=true" in merged.lower()
                        else "supports"
                    ),
                    confidence=confidence,
                )
            )

    root_node_id = ""
    if decision and decision.root_cause_component and decision.root_cause_reason:
        key = (decision.root_cause_component.lower(), decision.root_cause_reason.lower())
        root_node_id = hypothesis_ids.get(key, "")
        if not root_node_id:
            component_id = component_node(decision.root_cause_component)
            root_node_id = f"hypothesis:{len(hypothesis_ids) + 1}"
            nodes.append(
                CausalGraphNode(
                    id=root_node_id,
                    kind="hypothesis",
                    label=f"{decision.root_cause_component} · {decision.root_cause_reason}",
                    component=decision.root_cause_component,
                    reason=decision.root_cause_reason,
                    timestamp=decision.root_cause_time,
                    confidence={"low": 0.35, "medium": 0.65, "high": 0.9}[decision.confidence],
                    source_agent="RCA Coordinator",
                )
            )
            edges.append(
                CausalGraphEdge(
                    source=component_id,
                    target=root_node_id,
                    relation="causes",
                    confidence=0.65,
                )
            )
        outcome_id = "outcome:root-cause"
        nodes.append(
            CausalGraphNode(
                id=outcome_id,
                kind="outcome",
                label="Selected root cause",
                component=decision.root_cause_component,
                reason=decision.root_cause_reason,
                timestamp=decision.root_cause_time,
                confidence={"low": 0.35, "medium": 0.65, "high": 0.9}[decision.confidence],
                source_agent="RCA Coordinator",
            )
        )
        edges.append(
            CausalGraphEdge(
                source=root_node_id,
                target=outcome_id,
                relation="supports",
                confidence={"low": 0.35, "medium": 0.65, "high": 0.9}[decision.confidence],
            )
        )
    return CausalGraph(nodes=nodes, edges=edges, root_node_id=root_node_id)
