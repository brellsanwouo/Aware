"""CLI for ParserAgent BuildSpec generation."""

from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from agents.executor_agent import ExecutorAgentError, ExecutorEvent
from agents.parser_agent import ParserAgentError, ParserEvent
from llm.base import LLMError
from llm.factory import create_llm_client
from aware_models.buildspec import BuildSpec
from runtime.agent_factory import get_executor_agent, get_parser_agent
from runtime.batch_store import record_batch_incident, validate_batch_identifier
from runtime.env import env_bool, load_env
from runtime.graph_export import export_causal_graph_png
from runtime.knowledge_db import maybe_create_knowledge_store, resolve_db_url
from runtime.nezha_dataset import NEZHA_CSV_FIELDS, score_diagnosis
from runtime.output_store import persist_run_artifacts
from runtime.reporting import build_assessment_output

app = typer.Typer(help="AWARE Assess CLI (Google ADK).")
_LOADED_ENV_FILES: list[str] = []


def _runtime_flags() -> tuple[bool, bool]:
    """Return global (reasoning, memory) toggles."""
    return (
        env_bool("AWARE_ENABLE_REASONING", True),
        env_bool("AWARE_ENABLE_MEMORY", True),
    )


def _set_rca_version(value: str | None) -> str:
    resolved = (value or os.getenv("AWARE_RCA_VERSION", "v2")).strip().lower()
    if resolved not in {"v2", "v3"}:
        raise typer.BadParameter("RCA version must be v2 or v3.")
    os.environ["AWARE_RCA_VERSION"] = resolved
    return resolved


def _print_explicit_assess_sections(assessment_output: dict[str, object]) -> None:
    """Print explicit findings/anomalies/preliminary causes/final reporting."""
    findings = assessment_output.get("findings", [])
    anomalies = assessment_output.get("anomalies", [])
    causes = assessment_output.get("preliminary_causes", [])
    final_reporting = assessment_output.get("final_reporting", {})
    scope = assessment_output.get("buildspec_resolution_scope", {})
    synthesis = assessment_output.get("root_cause_synthesis", {})

    typer.echo("")
    typer.echo("--- BuildSpec Scope ---")
    if isinstance(scope, dict):
        task_type = scope.get("task_type", "n/a")
        requested_fields = scope.get("requested_fields", [])
        unknown_fields = scope.get("uncertainty_unknown_fields", [])
        typer.echo(f"task_type: {task_type}")
        typer.echo(f"requested_fields: {requested_fields}")
        typer.echo(f"uncertainty_unknown_fields: {unknown_fields}")
        mismatch = scope.get("scope_mismatch", {})
        if isinstance(mismatch, dict):
            typer.echo(
                "scope_mismatch: "
                f"unknown_not_in_task_scope={mismatch.get('unknown_not_in_task_scope', [])}, "
                f"task_scope_not_unknown={mismatch.get('task_scope_not_unknown', [])}"
            )

    typer.echo("")
    typer.echo("--- Root Cause Synthesis ---")
    if isinstance(synthesis, dict):
        typer.echo(f"task_type: {synthesis.get('task_type', 'n/a')}")
        typer.echo(f"metrics_summary: {synthesis.get('metrics_summary', 'n/a')}")
        typer.echo(f"trace_summary: {synthesis.get('trace_summary', 'n/a')}")
        typer.echo(f"log_summary: {synthesis.get('log_summary', 'n/a')}")
        uncertainty = synthesis.get("uncertainty", {})
        if isinstance(uncertainty, dict):
            typer.echo(f"uncertainty: {uncertainty}")

    typer.echo("")
    typer.echo("--- Findings ---")
    if isinstance(findings, list) and findings:
        for idx, item in enumerate(findings, start=1):
            if not isinstance(item, dict):
                continue
            typer.echo(
                f"{idx}. [{item.get('kind')}/{item.get('severity')}] "
                f"{item.get('agent')}: {item.get('summary')}"
            )
    else:
        typer.echo("1. No findings.")

    typer.echo("")
    typer.echo("--- Anomalies ---")
    if isinstance(anomalies, list) and anomalies:
        for idx, item in enumerate(anomalies, start=1):
            if not isinstance(item, dict):
                continue
            typer.echo(f"{idx}. {item.get('agent')}: {item.get('summary')}")
    else:
        typer.echo("1. No anomalies.")

    typer.echo("")
    typer.echo("--- Preliminary Causes ---")
    if isinstance(causes, list) and causes:
        for idx, item in enumerate(causes, start=1):
            typer.echo(f"{idx}. {item}")
    else:
        typer.echo("1. No preliminary causes.")

    typer.echo("")
    typer.echo("--- Final Reporting ---")
    if isinstance(final_reporting, dict):
        if final_reporting:
            for key, value in final_reporting.items():
                typer.echo(f"{key}: {value}")
        else:
            typer.echo("No final reporting fields requested by BuildSpec.")


@app.callback()
def main() -> None:
    """CLI group callback."""
    global _LOADED_ENV_FILES
    _LOADED_ENV_FILES = [str(path) for path in load_env()]


@app.command("parse")
def parse_buildspec(
    query: Annotated[str, typer.Option("--query", help="User RCA request.")],
    repo: Annotated[Path, typer.Option("--repo", help="Repository path to analyze.")],
    max_attempts: Annotated[
        int | None, typer.Option("--max-attempts", help="Maximum retry attempts.")
    ] = None,
    llm_model: Annotated[
        str | None, typer.Option("--llm-model", help="OpenAI model override.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON only.")] = False,
) -> None:
    """Generate and validate BuildSpec using ParserAgent."""
    if not repo.exists():
        typer.secho(f"Repository path does not exist: {repo}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    event_history: list[dict[str, object]] = []

    resolved_provider = "openai-compatible"
    resolved_attempts = int(max_attempts if max_attempts is not None else os.getenv("PARSER_MAX_ATTEMPTS", "5"))
    resolved_model = llm_model or os.getenv("OPENAI_MODEL")
    enable_reasoning, enable_memory = _runtime_flags()

    parser_agent = get_parser_agent(llm_client=None, max_attempts=resolved_attempts)

    try:
        def on_event(event: ParserEvent) -> None:
            event_history.append(
                {
                    "type": "event",
                    "sender": event.sender,
                    "recipient": event.recipient,
                    "phase": event.phase,
                    "content": event.content,
                    "timestamp": event.timestamp.isoformat(),
                }
            )

        result = parser_agent.generate_buildspec(
            user_query=query,
            repository_path=str(repo),
            on_event=on_event,
        )
    except ParserAgentError as exc:
        artifacts = persist_run_artifacts(
            run_id=run_id,
            query=query,
            source_path=str(repo),
            db_url=None,
            llm_provider=resolved_provider,
            llm_model=resolved_model,
            status="error",
            events=event_history,
            result_payload=None,
            error_message=str(exc),
        )
        typer.secho(
            f"Artifacts saved: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
            fg=typer.colors.YELLOW,
        )
        typer.secho(f"ParserAgent failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    result_payload = result.model_dump(mode="json")
    artifacts = persist_run_artifacts(
        run_id=run_id,
        query=query,
        source_path=str(repo),
        db_url=None,
        llm_provider=resolved_provider,
        llm_model=resolved_model,
        status="success",
        events=event_history,
        result_payload=result_payload,
        error_message=None,
    )
    result_payload["artifacts"] = artifacts

    if json_output:
        typer.echo(json.dumps(result_payload, ensure_ascii=False, indent=2))
        return

    typer.echo("=== ParserAgent Result ===")
    typer.echo(f"Run ID: {run_id}")
    typer.echo(f"Agent: {parser_agent.name} (Google ADK)")
    typer.echo(f"LLM Provider: {resolved_provider}")
    typer.echo(f"Reasoning: {'on' if enable_reasoning else 'off'} | Memory: {'on' if enable_memory else 'off'}")
    typer.echo(f"Attempts: {result.attempts}")
    typer.echo("BuildSpec valid: yes")
    typer.echo(f"Artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}")
    typer.echo("")
    typer.echo(result.buildspec.model_dump_json(indent=2))
    if result.errors_by_attempt:
        typer.echo("")
        typer.echo("Previous validation errors:")
        for idx, errors in enumerate(result.errors_by_attempt, start=1):
            typer.echo(f"- attempt {idx}: {'; '.join(errors)}")


@app.command("execute")
def execute_assess(
    buildspec_json: Annotated[Path, typer.Option("--buildspec-json", help="Path to BuildSpec JSON file.")],
    repo: Annotated[Path, typer.Option("--repo", help="Repository path to analyze.")],
    db_url: Annotated[str | None, typer.Option("--db-url", help="SQLite DB URL for knowledge storage.")] = None,
    max_agents: Annotated[
        int | None, typer.Option("--max-agents", help="Maximum number of sub-agents to instantiate.")
    ] = None,
    rca_version: Annotated[
        str | None, typer.Option("--rca-version", help="RCA engine version: v2 or v3.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON only.")] = False,
) -> None:
    """Execute Assess stage from an existing BuildSpec."""
    if not buildspec_json.exists():
        typer.secho(f"BuildSpec file does not exist: {buildspec_json}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if not repo.exists():
        typer.secho(f"Repository path does not exist: {repo}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    resolved_version = _set_rca_version(rca_version)

    run_id = f"run-{uuid.uuid4().hex[:10]}"
    event_history: list[dict[str, object]] = []
    resolved_provider = "openai-compatible"
    resolved_model = os.getenv("OPENAI_MODEL")
    default_max_agents = (
        int(os.getenv("EXECUTOR_MAX_AGENTS", "5"))
        if os.getenv("EXECUTOR_MAX_AGENTS", "5").strip().isdigit()
        else 5
    )
    resolved_max_agents = int(max_agents) if max_agents is not None else max(default_max_agents, 8) if resolved_version == "v3" else default_max_agents
    enable_reasoning, enable_memory = _runtime_flags()
    resolved_db_url = resolve_db_url(db_url)
    knowledge_store = maybe_create_knowledge_store(resolved_db_url) if enable_memory else None
    if knowledge_store is not None:
        knowledge_store.start_run(
            run_id=run_id,
            source_path=str(repo),
            query="execute_assess",
            db_url=resolved_db_url,
        )
    payload = _read_json_file(buildspec_json)
    buildspec_payload = payload.get("buildspec", payload) if isinstance(payload, dict) else payload
    try:
        buildspec = BuildSpec.model_validate(buildspec_payload)
    except Exception as exc:
        typer.secho(f"Invalid BuildSpec JSON: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    try:
        llm_client = create_llm_client(
            provider=resolved_provider,
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv("OPENAI_BASE_URL"),
            openai_model=resolved_model,
        )
        executor_agent = get_executor_agent(llm_client=llm_client)
    except (LLMError, ValueError) as exc:
        persist_run_artifacts(
            run_id=run_id,
            query="execute_assess",
            source_path=str(repo),
            db_url=None,
            llm_provider=resolved_provider,
            llm_model=resolved_model,
            status="error",
            events=event_history,
            result_payload=None,
            error_message=f"LLM configuration error: {exc}",
        )
        typer.secho(f"LLM configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    def on_event(event: ExecutorEvent) -> None:
        item = {
            "type": "event",
            "sender": event.sender,
            "recipient": event.recipient,
            "phase": event.phase,
            "content": event.content,
            "timestamp": event.timestamp.isoformat(),
        }
        event_history.append(item)
        if knowledge_store is not None:
            knowledge_store.append_event(
                run_id=run_id,
                ts=str(item["timestamp"]),
                sender=str(item["sender"]),
                recipient=str(item["recipient"]),
                phase=str(item["phase"]),
                content=str(item["content"]),
            )

    try:
        result = executor_agent.execute_assess(
            buildspec=buildspec,
            repository_path=str(repo),
            max_agents=resolved_max_agents,
            run_id=run_id,
            knowledge_store=knowledge_store,
            on_event=on_event,
        )
    except ExecutorAgentError as exc:
        if knowledge_store is not None:
            knowledge_store.finish_run(
                run_id=run_id,
                status="error",
                summary=str(exc),
                confidence=None,
                preliminary_causes=[],
            )
        artifacts = persist_run_artifacts(
            run_id=run_id,
            query="execute_assess",
            source_path=str(repo),
            db_url=None,
            llm_provider=resolved_provider,
            llm_model=resolved_model,
            status="error",
            events=event_history,
            result_payload=None,
            error_message=str(exc),
        )
        typer.secho(
            f"Artifacts saved: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
            fg=typer.colors.YELLOW,
        )
        typer.secho(f"ExecutorAgent failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    result_payload = result.model_dump(mode="json")
    assessment_output = build_assessment_output(buildspec, result)
    result_payload["assessment_output"] = assessment_output
    artifacts = persist_run_artifacts(
        run_id=run_id,
        query="execute_assess",
        source_path=str(repo),
        db_url=None,
        llm_provider=resolved_provider,
        llm_model=resolved_model,
        status="success",
        events=event_history,
        result_payload=result_payload,
        error_message=None,
    )
    result_payload["artifacts"] = artifacts

    if json_output:
        typer.echo(json.dumps(result_payload, ensure_ascii=False, indent=2))
        return

    typer.echo("=== ExecutorAgent Result ===")
    typer.echo(f"Run ID: {run_id}")
    typer.echo(f"Agent: {executor_agent.name} (Google ADK)")
    typer.echo(f"RCA version: {resolved_version.upper()}")
    typer.echo(f"Reasoning: {'on' if enable_reasoning else 'off'} | Memory: {'on' if enable_memory else 'off'}")
    typer.echo(f"Max agents: {resolved_max_agents}")
    typer.echo(f"Agents instantiated: {', '.join(result.agents_instantiated)}")
    typer.echo(f"Findings: {len(result.findings)}")
    typer.echo(f"Confidence: {result.confidence}")
    typer.echo(f"Artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}")
    _print_explicit_assess_sections(assessment_output)
    typer.echo("")
    typer.echo("Use --json to print the full structured payload.")


@app.command("assess")
def assess_end_to_end(
    query: Annotated[str, typer.Option("--query", help="User RCA request.")],
    repo: Annotated[Path, typer.Option("--repo", help="Repository path to analyze.")],
    db_url: Annotated[str | None, typer.Option("--db-url", help="SQLite DB URL for knowledge storage.")] = None,
    max_agents: Annotated[
        int | None, typer.Option("--max-agents", help="Maximum number of sub-agents to instantiate.")
    ] = None,
    max_attempts: Annotated[
        int | None, typer.Option("--max-attempts", help="Maximum parser retry attempts.")
    ] = None,
    llm_model: Annotated[
        str | None, typer.Option("--llm-model", help="OpenAI model override.")
    ] = None,
    rca_version: Annotated[
        str | None, typer.Option("--rca-version", help="RCA engine version: v2 or v3.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON only.")] = False,
) -> None:
    """Run ParserAgent then ExecutorAgent in one command."""
    if not repo.exists():
        typer.secho(f"Repository path does not exist: {repo}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    resolved_version = _set_rca_version(rca_version)

    run_id = f"run-{uuid.uuid4().hex[:10]}"
    event_history: list[dict[str, object]] = []
    resolved_provider = "openai-compatible"
    default_max_agents = (
        int(os.getenv("EXECUTOR_MAX_AGENTS", "5"))
        if os.getenv("EXECUTOR_MAX_AGENTS", "5").strip().isdigit()
        else 5
    )
    resolved_max_agents = int(max_agents) if max_agents is not None else max(default_max_agents, 8) if resolved_version == "v3" else default_max_agents
    enable_reasoning, enable_memory = _runtime_flags()
    resolved_db_url = resolve_db_url(db_url)
    knowledge_store = maybe_create_knowledge_store(resolved_db_url) if enable_memory else None
    if knowledge_store is not None:
        knowledge_store.start_run(
            run_id=run_id,
            source_path=str(repo),
            query=query,
            db_url=resolved_db_url,
        )
    resolved_attempts = int(max_attempts if max_attempts is not None else os.getenv("PARSER_MAX_ATTEMPTS", "5"))
    resolved_model = llm_model or os.getenv("OPENAI_MODEL")

    parser_agent = get_parser_agent(llm_client=None, max_attempts=resolved_attempts)

    def on_parser_event(event: ParserEvent) -> None:
        item = {
            "type": "event",
            "sender": event.sender,
            "recipient": event.recipient,
            "phase": event.phase,
            "content": event.content,
            "timestamp": event.timestamp.isoformat(),
        }
        event_history.append(item)
        if knowledge_store is not None:
            knowledge_store.append_event(
                run_id=run_id,
                ts=str(item["timestamp"]),
                sender=str(item["sender"]),
                recipient=str(item["recipient"]),
                phase=str(item["phase"]),
                content=str(item["content"]),
            )

    def on_executor_event(event: ExecutorEvent) -> None:
        item = {
            "type": "event",
            "sender": event.sender,
            "recipient": event.recipient,
            "phase": event.phase,
            "content": event.content,
            "timestamp": event.timestamp.isoformat(),
        }
        event_history.append(item)
        if knowledge_store is not None:
            knowledge_store.append_event(
                run_id=run_id,
                ts=str(item["timestamp"]),
                sender=str(item["sender"]),
                recipient=str(item["recipient"]),
                phase=str(item["phase"]),
                content=str(item["content"]),
            )

    try:
        parser_result = parser_agent.generate_buildspec(
            user_query=query,
            repository_path=str(repo),
            on_event=on_parser_event,
        )
        if parser_agent.llm_client is None:
            raise ParserAgentError("ParserAgent did not expose a configured LLM client.")
        executor_agent = get_executor_agent(llm_client=parser_agent.llm_client)
        executor_result = executor_agent.execute_assess(
            buildspec=parser_result.buildspec,
            repository_path=str(repo),
            max_agents=resolved_max_agents,
            run_id=run_id,
            knowledge_store=knowledge_store,
            on_event=on_executor_event,
        )
    except (ParserAgentError, ExecutorAgentError) as exc:
        if knowledge_store is not None:
            knowledge_store.finish_run(
                run_id=run_id,
                status="error",
                summary=str(exc),
                confidence=None,
                preliminary_causes=[],
            )
        artifacts = persist_run_artifacts(
            run_id=run_id,
            query=query,
            source_path=str(repo),
            db_url=None,
            llm_provider=resolved_provider,
            llm_model=resolved_model,
            status="error",
            events=event_history,
            result_payload=None,
            error_message=str(exc),
        )
        typer.secho(
            f"Artifacts saved: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
            fg=typer.colors.YELLOW,
        )
        typer.secho(f"Assess failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    combined_payload = {
        "run_id": run_id,
        "parser": parser_result.model_dump(mode="json"),
        "executor": executor_result.model_dump(mode="json"),
    }
    assessment_output = build_assessment_output(parser_result.buildspec, executor_result)
    combined_payload["assessment_output"] = assessment_output
    artifacts = persist_run_artifacts(
        run_id=run_id,
        query=query,
        source_path=str(repo),
        db_url=None,
        llm_provider=resolved_provider,
        llm_model=resolved_model,
        status="success",
        events=event_history,
        result_payload=combined_payload,
        error_message=None,
    )
    combined_payload["artifacts"] = artifacts

    if json_output:
        typer.echo(json.dumps(combined_payload, ensure_ascii=False, indent=2))
        return

    typer.echo("=== Assess Result (Parser + Executor) ===")
    typer.echo(f"Run ID: {run_id}")
    typer.echo(f"RCA version: {resolved_version.upper()}")
    typer.echo(f"Parser attempts: {parser_result.attempts}")
    typer.echo(f"Reasoning: {'on' if enable_reasoning else 'off'} | Memory: {'on' if enable_memory else 'off'}")
    typer.echo(f"Max agents: {resolved_max_agents}")
    typer.echo(f"Executor agents: {', '.join(executor_result.agents_instantiated)}")
    typer.echo(f"Findings: {len(executor_result.findings)}")
    typer.echo(f"Confidence: {executor_result.confidence}")
    typer.echo(f"Artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}")
    _print_explicit_assess_sections(assessment_output)
    typer.echo("")
    typer.echo("Use --json to print the full structured payload.")


@app.command("batch")
def assess_batch(
    problems_csv: Annotated[
        Path, typer.Option("--problems-csv", help="AWARE problems CSV to execute.")
    ],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Fallback telemetry repository when source_path is blank."),
    ] = None,
    rca_version: Annotated[
        str | None, typer.Option("--rca-version", help="RCA engine version: v2 or v3.")
    ] = None,
    results_csv: Annotated[
        Path | None, typer.Option("--results-csv", help="Batch score CSV destination.")
    ] = None,
    graph_dir: Annotated[
        Path | None, typer.Option("--graph-dir", help="Directory for V3 causal graph PNG files.")
    ] = None,
    batch_id: Annotated[
        str | None, typer.Option("--batch-id", help="Stable identifier used for backend journals.")
    ] = None,
    db_url: Annotated[
        str | None, typer.Option("--db-url", help="SQLite DB URL for knowledge storage.")
    ] = None,
    max_agents: Annotated[
        int | None, typer.Option("--max-agents", min=1, max=200, help="Agent ceiling per incident.")
    ] = None,
    max_attempts: Annotated[
        int | None, typer.Option("--max-attempts", min=1, help="Parser retry ceiling.")
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Run only the first N incidents.")
    ] = None,
    llm_model: Annotated[
        str | None, typer.Option("--llm-model", help="OpenAI model override.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the final batch summary as JSON.")
    ] = False,
) -> None:
    """Run an AWARE CSV batch with per-incident logs, scores, and V3 graph PNGs."""
    if not problems_csv.is_file():
        typer.secho(f"Problems CSV does not exist: {problems_csv}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if repo is not None and not repo.exists():
        typer.secho(f"Fallback repository does not exist: {repo}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    version = _set_rca_version(rca_version)
    resolved_batch_id = validate_batch_identifier(
        batch_id or f"batch-cli-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        "batch_id",
    )
    problems = _read_problems_csv(problems_csv)
    if limit is not None:
        problems = problems[:limit]
    if not problems:
        typer.secho("The problems CSV contains no executable incidents.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    batch_root = (Path.cwd() / "output" / "batches" / resolved_batch_id).resolve()
    resolved_results_csv = (results_csv or (batch_root / "results.csv")).expanduser().resolve()
    resolved_graph_dir = (graph_dir or (batch_root / "graphs")).expanduser().resolve()
    default_agents = int(os.getenv("EXECUTOR_MAX_AGENTS", "5")) if os.getenv("EXECUTOR_MAX_AGENTS", "5").isdigit() else 5
    resolved_max_agents = int(max_agents) if max_agents is not None else max(default_agents, 8) if version == "v3" else default_agents
    resolved_attempts = int(max_attempts if max_attempts is not None else os.getenv("PARSER_MAX_ATTEMPTS", "5"))
    resolved_model = llm_model or os.getenv("OPENAI_MODEL")
    resolved_db_url = resolve_db_url(db_url)
    enable_reasoning, enable_memory = _runtime_flags()
    knowledge_store = maybe_create_knowledge_store(resolved_db_url) if enable_memory else None
    try:
        llm_client = create_llm_client(
            provider="openai-compatible",
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv("OPENAI_BASE_URL"),
            openai_model=resolved_model,
        )
    except (LLMError, ValueError) as exc:
        typer.secho(f"LLM configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    parser_agent = get_parser_agent(llm_client=llm_client, max_attempts=resolved_attempts)
    executor_agent = get_executor_agent(llm_client=llm_client)
    results: list[dict[str, object]] = []

    if not json_output:
        typer.echo(
            f"Starting {version.upper()} batch {resolved_batch_id}: "
            f"incidents={len(problems)}, max_agents={resolved_max_agents}"
        )
    for index, problem in enumerate(problems, start=1):
        problem_id = validate_batch_identifier(str(problem["problem_id"]), "problem_id")
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        source_path = _resolve_problem_source(problem, problems_csv, repo)
        query = _problem_query(problem)
        profile = _problem_profile(problem_id)
        offset = int(str(problem.get("timezone_offset_minutes") or 0))
        events: list[dict[str, object]] = []
        if knowledge_store is not None:
            knowledge_store.start_run(
                run_id=run_id,
                source_path=str(source_path),
                query=query,
                db_url=resolved_db_url,
            )

        def store_event(event: ParserEvent | ExecutorEvent) -> None:
            item = {
                "type": "event",
                "sender": event.sender,
                "recipient": event.recipient,
                "phase": event.phase,
                "content": event.content,
                "timestamp": event.timestamp.isoformat(),
            }
            events.append(item)
            if knowledge_store is not None:
                knowledge_store.append_event(
                    run_id=run_id,
                    ts=str(item["timestamp"]),
                    sender=str(item["sender"]),
                    recipient=str(item["recipient"]),
                    phase=str(item["phase"]),
                    content=str(item["content"]),
                )

        status = "success"
        error_message = ""
        agents_created = 0
        graph_path = ""
        combined_payload: dict[str, object] | None = None
        artifacts: dict[str, str] = {}
        diagnosis: dict[str, object] = {}
        try:
            parser_result = parser_agent.generate_buildspec(
                user_query=query,
                repository_path=str(source_path),
                on_event=store_event,
                timezone_offset_minutes=offset,
                dataset_profile=profile,
            )
            executor_result = executor_agent.execute_assess(
                buildspec=parser_result.buildspec,
                repository_path=str(source_path),
                max_agents=resolved_max_agents,
                run_id=run_id,
                knowledge_store=knowledge_store,
                on_event=store_event,
            )
            agents_created = len(executor_result.agents_instantiated)
            assessment_output = build_assessment_output(parser_result.buildspec, executor_result)
            synthesis = assessment_output.get("root_cause_synthesis", {})
            if isinstance(synthesis, dict) and isinstance(synthesis.get("final_diagnosis"), dict):
                diagnosis = synthesis["final_diagnosis"]
            combined_payload = {
                "run_id": run_id,
                "batch_id": resolved_batch_id,
                "problem_id": problem_id,
                "parser": parser_result.model_dump(mode="json"),
                "executor": executor_result.model_dump(mode="json"),
                "assessment_output": assessment_output,
            }
            artifacts = persist_run_artifacts(
                run_id=run_id,
                query=query,
                source_path=str(source_path),
                db_url=resolved_db_url,
                llm_provider="openai-compatible",
                llm_model=resolved_model,
                status="success",
                events=events,
                result_payload=combined_payload,
                error_message=None,
            )
            combined_payload["artifacts"] = artifacts
            if (
                version == "v3"
                and executor_result.causal_graph is not None
                and executor_result.causal_graph.nodes
            ):
                graph_path = str(
                    export_causal_graph_png(
                        executor_result.causal_graph,
                        resolved_graph_dir / f"{problem_id}-causal-graph.png",
                    )
                )
                artifacts["graph_png_path"] = graph_path
                combined_payload["graph_png_path"] = graph_path
        except (ParserAgentError, ExecutorAgentError, LLMError, ValueError, OSError) as exc:
            status = "error"
            error_message = str(exc)
            if knowledge_store is not None:
                knowledge_store.finish_run(
                    run_id=run_id,
                    status="error",
                    summary=error_message,
                    confidence=None,
                    preliminary_causes=[],
                )
            artifacts = persist_run_artifacts(
                run_id=run_id,
                query=query,
                source_path=str(source_path),
                db_url=resolved_db_url,
                llm_provider="openai-compatible",
                llm_model=resolved_model,
                status="error",
                events=events,
                result_payload=None,
                error_message=error_message,
            )

        scores = score_diagnosis(diagnosis, problem)
        result_row: dict[str, object] = {
            **problem,
            **scores,
            "status": status,
            "run_id": run_id,
            "agents_created": agents_created,
            "predicted_component": diagnosis.get("root_cause_component", ""),
            "predicted_time": diagnosis.get("root_cause_time", ""),
            "predicted_reason": diagnosis.get("root_cause_reason", ""),
            "graph_png_path": graph_path,
            "artifact_json_path": artifacts.get("json_path", ""),
            "artifact_text_path": artifacts.get("txt_path", ""),
            "error": error_message,
        }
        if combined_payload is not None:
            combined_payload["score"] = scores
        journal = record_batch_incident(
            batch_id=resolved_batch_id,
            problem_id=problem_id,
            run_id=run_id,
            status=status,
            agents_created=agents_created,
            events=events,
            artifacts=artifacts,
            result_payload=combined_payload,
            error_message=error_message or None,
        )
        result_row.update(journal)
        results.append(result_row)
        if not json_output:
            outcome = "PASS" if scores.get("success") else "ERROR" if status == "error" else "FAIL"
            typer.echo(
                f"[{index}/{len(problems)}] {problem_id}: {outcome} · "
                f"agents={agents_created} · graph={graph_path or 'none'}"
            )

    _write_batch_results_csv(resolved_results_csv, results)
    passed = sum(1 for result in results if result.get("success"))
    summary = {
        "batch_id": resolved_batch_id,
        "rca_version": version,
        "incidents": len(results),
        "passed": passed,
        "success_rate": round(passed / len(results), 4) if results else 0.0,
        "results_csv": str(resolved_results_csv),
        "graph_dir": str(resolved_graph_dir) if version == "v3" else "",
        "batch_manifest": str(batch_root / "manifest.json"),
    }
    if json_output:
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        typer.echo(
            f"Batch complete: {passed}/{len(results)} passed "
            f"({summary['success_rate']:.1%}). Results: {resolved_results_csv}"
        )


@app.command("ui")
def launch_ui(
    host: Annotated[str | None, typer.Option("--host", help="UI host.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="UI port.")] = None,
    rca_version: Annotated[
        str | None,
        typer.Option("--rca-version", help="RCA engine version: v2 or v3."),
    ] = None,
) -> None:
    """Launch web UI for live parser conversation."""
    try:
        import uvicorn
    except ImportError as exc:
        typer.secho("uvicorn is required. Install dependencies with `pip install -e .`.", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    from ui.server import create_app

    resolved_host = (host or os.getenv("UI_HOST", "127.0.0.1")).strip()
    resolved_port = int(port if port is not None else os.getenv("UI_PORT", "8787"))
    resolved_version = (rca_version or os.getenv("AWARE_RCA_VERSION", "v2")).strip().lower()
    if resolved_version not in {"v2", "v3"}:
        typer.secho("--rca-version must be v2 or v3.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    os.environ["AWARE_RCA_VERSION"] = resolved_version

    typer.secho(
        f"Starting AWARE {resolved_version.upper()} UI on http://{resolved_host}:{resolved_port}",
        fg=typer.colors.GREEN,
    )
    uvicorn.run(create_app(), host=resolved_host, port=resolved_port, reload=False)


def _read_json_file(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON file `{path}`: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object in `{path}`.")
    return data


def _read_problems_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = set(reader.fieldnames or [])
            missing = [field for field in NEZHA_CSV_FIELDS if field not in fieldnames]
            if missing:
                raise ValueError(f"Problems CSV is missing required columns: {', '.join(missing)}")
            rows = [
                {key: str(value or "").strip() for key, value in row.items()}
                for row in reader
                if str(row.get("problem_id") or "").strip()
            ]
    except OSError as exc:
        raise ValueError(f"Unable to read problems CSV `{path}`: {exc}") from exc
    for index, row in enumerate(rows, start=2):
        for field in ("problem_id", "date", "start_time", "end_time", "expected_component", "expected_time", "expected_reason"):
            if not row.get(field):
                raise ValueError(f"Problems CSV row {index} has an empty `{field}` value.")
        try:
            int(row.get("timezone_offset_minutes") or 0)
            int(float(row["expected_time"]))
        except ValueError as exc:
            raise ValueError(f"Problems CSV row {index} has an invalid timezone or expected_time.") from exc
    return rows


def _resolve_problem_source(
    problem: dict[str, str],
    problems_csv: Path,
    fallback_repo: Path | None,
) -> Path:
    raw = str(problem.get("source_path") or "").strip()
    if raw:
        source = Path(raw).expanduser()
        if not source.is_absolute():
            source = problems_csv.resolve().parent / source
    elif fallback_repo is not None:
        source = fallback_repo.expanduser()
    else:
        raise ValueError(
            f"Incident {problem.get('problem_id')} has no source_path and --repo was not provided."
        )
    source = source.resolve()
    if not source.exists():
        raise ValueError(f"Telemetry source does not exist for {problem.get('problem_id')}: {source}")
    return source


def _problem_profile(problem_id: str) -> str:
    lowered = problem_id.lower()
    if lowered.startswith("nezha-"):
        return "nezha"
    for dataset in ("bank", "market", "telecom"):
        if lowered.startswith(f"openrca-{dataset}-"):
            return f"openrca_{dataset}"
    return "generic"


def _offset_label(minutes: int) -> str:
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    absolute = abs(minutes)
    return f"UTC{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _problem_query(problem: dict[str, str]) -> str:
    offset = int(problem.get("timezone_offset_minutes") or 0)
    return (
        f"On {problem['date']}, between {problem['start_time']} and {problem['end_time']} "
        f"{_offset_label(offset)}, one failure occurred. Identify the root cause component, "
        "occurrence time, and reason."
    )


_BATCH_RESULT_FIELDS = (
    "problem_id",
    "system",
    "status",
    "run_id",
    "agents_created",
    "success",
    "component_ok",
    "reason_ok",
    "time_ok",
    "time_error_seconds",
    "predicted_component",
    "predicted_time",
    "predicted_reason",
    "expected_component",
    "expected_time",
    "expected_reason",
    "graph_png_path",
    "artifact_json_path",
    "artifact_text_path",
    "batch_incident_json_path",
    "batch_incident_log_path",
    "error",
)


def _write_batch_results_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_BATCH_RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run() -> None:
    """Script entrypoint."""
    app()


if __name__ == "__main__":
    run()
