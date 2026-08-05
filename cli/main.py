"""CLI for ParserAgent BuildSpec generation."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer

from agents.executor_agent import ExecutorAgentError, ExecutorEvent
from agents.parser_agent import ParserAgentError, ParserEvent
from llm.base import LLMError
from llm.factory import create_llm_client
from aware_models.buildspec import BuildSpec
from runtime.agent_factory import get_executor_agent, get_parser_agent
from runtime.env import env_bool, load_env
from runtime.knowledge_db import maybe_create_knowledge_store, resolve_db_url
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
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON only.")] = False,
) -> None:
    """Execute Assess stage from an existing BuildSpec."""
    if not buildspec_json.exists():
        typer.secho(f"BuildSpec file does not exist: {buildspec_json}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if not repo.exists():
        typer.secho(f"Repository path does not exist: {repo}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    run_id = f"run-{uuid.uuid4().hex[:10]}"
    event_history: list[dict[str, object]] = []
    resolved_provider = "openai-compatible"
    resolved_model = os.getenv("OPENAI_MODEL")
    default_max_agents = (
        int(os.getenv("EXECUTOR_MAX_AGENTS", "5"))
        if os.getenv("EXECUTOR_MAX_AGENTS", "5").strip().isdigit()
        else 5
    )
    resolved_max_agents = int(max_agents) if max_agents is not None else default_max_agents
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
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON only.")] = False,
) -> None:
    """Run ParserAgent then ExecutorAgent in one command."""
    if not repo.exists():
        typer.secho(f"Repository path does not exist: {repo}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    run_id = f"run-{uuid.uuid4().hex[:10]}"
    event_history: list[dict[str, object]] = []
    resolved_provider = "openai-compatible"
    default_max_agents = (
        int(os.getenv("EXECUTOR_MAX_AGENTS", "5"))
        if os.getenv("EXECUTOR_MAX_AGENTS", "5").strip().isdigit()
        else 5
    )
    resolved_max_agents = int(max_agents) if max_agents is not None else default_max_agents
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


def _read_json_file(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON file `{path}`: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object in `{path}`.")
    return data


def run() -> None:
    """Script entrypoint."""
    app()


if __name__ == "__main__":
    run()
