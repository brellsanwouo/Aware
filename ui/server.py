"""FastAPI UI server for live ParserAgent conversation."""

from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from agents.executor_agent import ExecutorAgentError, ExecutorEvent
from agents.parser_agent import ParserAgentError, ParserEvent
from llm.factory import create_llm_client
from runtime.agent_factory import get_executor_agent, get_parser_agent
from runtime.batch_store import (
    batch_directory,
    incident_log_path,
    record_batch_incident,
)
from runtime.env import env_bool, load_env
from runtime.knowledge_db import maybe_create_knowledge_store, resolve_db_url
from runtime.output_store import persist_run_artifacts
from runtime.nezha_dataset import build_nezha_problems, discover_nezha_root, problems_to_csv
from runtime.openrca_dataset import build_openrca_problems, openrca_problems_to_csv
from runtime.reporting import build_assessment_output


def create_app() -> FastAPI:
    """Create UI app."""
    loaded_env_files = [str(path) for path in load_env()]
    rca_version = os.getenv("AWARE_RCA_VERSION", "v2").strip().lower()
    rca_version = "v3" if rca_version == "v3" else "v2"
    app = FastAPI(title=f"AWARE Assess UI ({rca_version.upper()})")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        index_path = Path(__file__).with_name("index.html")
        return index_path.read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def config() -> dict[str, str]:
        project_root = Path(__file__).resolve().parent.parent
        nezha_extracted = project_root / "data" / "Nezha" / "Nezha-v0.1"
        nezha_case = project_root / "data" / "Nezha" / "cases" / "train-ticket-2023-01-30-1151"
        openrca_root = project_root / "data" / "Openrca"
        openrca_bank = openrca_root / "Bank-003" / "Bank"
        openrca_market = openrca_root / "Market-002" / "Market" / "cloudbed-1"
        openrca_telecom = openrca_root / "Telecom-001" / "Telecom"
        try:
            nezha_root = discover_nezha_root(nezha_extracted) if nezha_extracted.is_dir() else None
        except ValueError:
            nezha_root = None
        return {
            "ui_default_source_path": os.getenv("UI_DEFAULT_SOURCE_PATH", ""),
            "nezha_case_path": str(nezha_root or (nezha_case if nezha_case.is_dir() else "")),
            "openrca_bank_path": str(openrca_bank if openrca_bank.is_dir() else ""),
            "openrca_market_path": str(openrca_market if openrca_market.is_dir() else ""),
            "openrca_telecom_path": str(openrca_telecom if openrca_telecom.is_dir() else ""),
            "openai_model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            "executor_max_agents": os.getenv("EXECUTOR_MAX_AGENTS", ""),
            "parser_llm_provider": "openai-compatible",
            "aware_enable_reasoning": "true" if env_bool("AWARE_ENABLE_REASONING", True) else "false",
            "aware_enable_memory": "true" if env_bool("AWARE_ENABLE_MEMORY", True) else "false",
            "rca_version": rca_version,
            "env_loaded_files": ", ".join(loaded_env_files),
        }

    @app.get("/api/nezha/problems.csv")
    def nezha_problems_csv(source_path: str | None = Query(None)) -> Response:
        project_root = Path(__file__).resolve().parent.parent
        selected_source = Path(source_path).expanduser() if source_path else project_root / "data" / "Nezha" / "Nezha-v0.1"
        try:
            csv_text = problems_to_csv(build_nezha_problems(selected_source))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="nezha_incidents.csv"'},
        )

    @app.get("/api/openrca/{dataset}/problems.csv")
    def openrca_problems_csv(
        dataset: str,
        source_path: str | None = Query(None),
    ) -> Response:
        project_root = Path(__file__).resolve().parent.parent
        defaults = {
            "bank": project_root / "data" / "Openrca" / "Bank-003",
            "market": project_root / "data" / "Openrca" / "Market-002",
            "telecom": project_root / "data" / "Openrca" / "Telecom-001",
        }
        dataset_name = dataset.strip().lower()
        if dataset_name not in defaults:
            raise HTTPException(status_code=404, detail=f"Unknown OpenRCA dataset: {dataset}")
        selected_source = Path(source_path).expanduser() if source_path else defaults[dataset_name]
        try:
            csv_text = openrca_problems_to_csv(
                build_openrca_problems(selected_source, dataset_name)
            )
        except (OSError, ValueError, csv.Error, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="openrca_{dataset_name}_incidents.csv"'
                )
            },
        )

    @app.get("/api/batches/{batch_id}")
    def batch_manifest(batch_id: str) -> FileResponse:
        try:
            path = batch_directory(batch_id) / "manifest.json"
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Batch manifest not found.")
        return FileResponse(path, media_type="application/json", filename=f"{batch_id}-manifest.json")

    @app.get("/api/batches/{batch_id}/incidents/{problem_id}")
    def batch_incident_log(batch_id: str, problem_id: str, text: bool = Query(False)) -> FileResponse:
        try:
            json_path = incident_log_path(batch_id, problem_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path = json_path.with_suffix(".log") if text else json_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Batch incident log not found.")
        media_type = "text/plain" if text else "application/json"
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.get("/api/parse-stream")
    def parse_stream(
        query: str = Query(..., min_length=3),
        source_path: str | None = Query(None),
        repo: str | None = Query(None),
        db_url: str | None = Query(None),
        llm_model: str | None = Query(None),
        max_attempts: int | None = Query(None, ge=1, le=20),
        max_agents: int | None = Query(None, ge=1, le=200),
        buildspec_only: bool = Query(False),
        timezone_offset_minutes: int = Query(480, ge=-720, le=840),
        dataset_profile: str = Query(
            "generic",
            pattern="^(generic|nezha|openrca_bank|openrca_market|openrca_telecom)$",
        ),
        batch_id: str | None = Query(None, pattern=r"^[A-Za-z0-9_.-]{1,120}$"),
        problem_id: str | None = Query(None, pattern=r"^[A-Za-z0-9_.-]{1,120}$"),
    ) -> StreamingResponse:
        selected_source = (source_path or repo or "").strip()
        if not selected_source:
            raise HTTPException(status_code=400, detail="source_path (or repo) is required.")

        repo_path = Path(selected_source)
        if not repo_path.exists():
            raise HTTPException(status_code=400, detail=f"Repository path does not exist: {selected_source}")

        run_id = f"run-{uuid.uuid4().hex[:10]}"
        event_queue: queue.Queue[dict[str, object]] = queue.Queue()
        done = threading.Event()
        result_holder: dict[str, object] = {}
        event_history: list[dict[str, object]] = []
        event_lock = threading.Lock()
        enable_reasoning = env_bool("AWARE_ENABLE_REASONING", True)
        enable_memory = env_bool("AWARE_ENABLE_MEMORY", True)
        resolved_db_url = resolve_db_url(db_url)
        knowledge_store = maybe_create_knowledge_store(resolved_db_url) if enable_memory else None
        if knowledge_store is not None:
            knowledge_store.start_run(
                run_id=run_id,
                source_path=str(repo_path),
                query=query,
                db_url=resolved_db_url,
            )

        def push_event(
            sender: str,
            recipient: str,
            phase: str,
            content: str,
        ) -> None:
            item = {
                "type": "event",
                "sender": sender,
                "recipient": recipient,
                "phase": phase,
                "content": content,
                "timestamp": _iso_now(),
            }
            with event_lock:
                event_history.append(item)
            event_queue.put(item)
            if knowledge_store is not None:
                knowledge_store.append_event(
                    run_id=run_id,
                    ts=str(item["timestamp"]),
                    sender=str(item["sender"]),
                    recipient=str(item["recipient"]),
                    phase=str(item["phase"]),
                    content=str(item["content"]),
                )

        def on_event(event: ParserEvent) -> None:
            item = {
                "type": "event",
                "sender": event.sender,
                "recipient": event.recipient,
                "phase": event.phase,
                "content": event.content,
                "timestamp": event.timestamp.isoformat(),
            }
            with event_lock:
                event_history.append(item)
            event_queue.put(item)
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
            with event_lock:
                event_history.append(item)
            event_queue.put(item)
            if knowledge_store is not None:
                knowledge_store.append_event(
                    run_id=run_id,
                    ts=str(item["timestamp"]),
                    sender=str(item["sender"]),
                    recipient=str(item["recipient"]),
                    phase=str(item["phase"]),
                    content=str(item["content"]),
                )

        def persist_batch_log(
            *,
            status: str,
            artifacts: dict[str, str],
            result_payload: dict[str, object] | None,
            error_message: str | None,
        ) -> dict[str, str]:
            if not batch_id or not problem_id:
                return {}
            with event_lock:
                frozen_events = list(event_history)
            agents_created = sum(
                1 for item in frozen_events if item.get("phase") == "instantiate_agent"
            )
            stored = record_batch_incident(
                batch_id=batch_id,
                problem_id=problem_id,
                run_id=run_id,
                status=status,
                agents_created=agents_created,
                events=frozen_events,
                artifacts=artifacts,
                result_payload=result_payload,
                error_message=error_message,
            )
            return {
                **stored,
                "manifest_url": f"/api/batches/{batch_id}",
                "incident_log_url": f"/api/batches/{batch_id}/incidents/{problem_id}",
                "incident_text_log_url": (
                    f"/api/batches/{batch_id}/incidents/{problem_id}?text=true"
                ),
            }

        def worker() -> None:
            resolved_provider = "openai-compatible"
            resolved_model = llm_model or os.getenv("OPENAI_MODEL")
            resolved_attempts = int(max_attempts if max_attempts is not None else os.getenv("PARSER_MAX_ATTEMPTS", "5"))
            configured_agent_budget = os.getenv("EXECUTOR_MAX_AGENTS", "").strip()
            default_max_agents = (
                int(configured_agent_budget) if configured_agent_budget.isdigit() else None
            )
            if rca_version == "v3" and max_agents is None:
                default_max_agents = max(default_max_agents or 0, 8)
            resolved_max_agents = int(max_agents) if max_agents is not None else default_max_agents
            try:
                push_event("System", "UI", "status", f"Starting Assess run... ({run_id})")
                if batch_id and problem_id:
                    push_event(
                        "System",
                        "UI",
                        "batch",
                        f"Batch context: batch_id={batch_id}; problem_id={problem_id}.",
                    )
                push_event("System", "UI", "status", f"Resolved LLM provider: {resolved_provider}")
                push_event(
                    "System",
                    "UI",
                    "status",
                    f"Runtime flags: reasoning={'on' if enable_reasoning else 'off'}, memory={'on' if enable_memory else 'off'}",
                )
                push_event("System", "UI", "status", f"Resolved DB URL: {resolved_db_url}")
                push_event(
                    "System",
                    "UI",
                    "status",
                    f"Dataset profile: {dataset_profile}; UTC offset: {timezone_offset_minutes:+d} minutes.",
                )
                push_event(
                    "System",
                    "UI",
                    "status",
                    (
                        "Resolved max agents: "
                        f"{resolved_max_agents if resolved_max_agents is not None else 'unlimited'}"
                    ),
                )
                push_event(
                    "System",
                    "UI",
                    "status",
                    (
                        "Env check: OPENAI_API_KEY="
                        f"{'present' if bool(os.getenv('OPENAI_API_KEY')) else 'missing'}; "
                        f"loaded_env_files={', '.join(loaded_env_files) or 'none'}"
                    ),
                )

                parser_agent = get_parser_agent(
                    llm_client=None,
                    max_attempts=resolved_attempts,
                )
                push_event("System", "UI", "status", f"{parser_agent.name} ready (persistent runtime agent).")

                parser_result = parser_agent.generate_buildspec(
                    user_query=query,
                    repository_path=str(repo_path),
                    on_event=on_event,
                    timezone_offset_minutes=timezone_offset_minutes,
                    dataset_profile=dataset_profile,
                )
                if parser_agent.llm_client is None:
                    raise ParserAgentError("ParserAgent did not expose a configured LLM client.")
                executor_agent = get_executor_agent(llm_client=parser_agent.llm_client)
                push_event("System", "UI", "status", f"{executor_agent.name} ready (persistent runtime agent).")
                event_queue.put(
                    {
                        "type": "parser_result",
                        "payload": {
                            "buildspec": parser_result.buildspec.model_dump(mode="json"),
                            "attempts": parser_result.attempts,
                            "timezone_offset_minutes": timezone_offset_minutes,
                            "dataset_profile": dataset_profile,
                        },
                    }
                )
                push_event(
                    "ParserAgent",
                    "Runtime",
                    "buildspec",
                    f"BuildSpec created and validated (attempts={parser_result.attempts}).",
                )
                if buildspec_only:
                    result_payload = {
                        "parser": parser_result.model_dump(mode="json"),
                        "execution": {
                            "run_id": run_id,
                            "batch_id": batch_id,
                            "problem_id": problem_id,
                            "agents_created": 0,
                        },
                    }
                    with event_lock:
                        frozen_events = list(event_history)
                    artifacts = persist_run_artifacts(
                        run_id=run_id,
                        query=query,
                        source_path=str(repo_path),
                        db_url=db_url,
                        llm_provider=resolved_provider,
                        llm_model=resolved_model,
                        status="success",
                        events=frozen_events,
                        result_payload=result_payload,
                        error_message=None,
                    )
                    result_payload["artifacts"] = artifacts
                    push_event(
                        "System",
                        "UI",
                        "status",
                        "BuildSpec-only mode: stopping before Executor.",
                    )
                    push_event(
                        "System",
                        "UI",
                        "artifact",
                        f"Saved run artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
                    )
                    batch_log = persist_batch_log(
                        status="success",
                        artifacts=artifacts,
                        result_payload=result_payload,
                        error_message=None,
                    )
                    if batch_log:
                        result_payload["batch_log"] = batch_log
                    result_holder["result"] = result_payload
                    return

                executor_result = executor_agent.execute_assess(
                    buildspec=parser_result.buildspec,
                    repository_path=str(repo_path),
                    max_agents=resolved_max_agents,
                    run_id=run_id,
                    knowledge_store=knowledge_store,
                    on_event=on_executor_event,
                )
                assessment_output = build_assessment_output(parser_result.buildspec, executor_result)
                result_payload = {
                    "parser": parser_result.model_dump(mode="json"),
                    "executor": executor_result.model_dump(mode="json"),
                    "assessment_output": assessment_output,
                    "execution": {
                        "run_id": run_id,
                        "batch_id": batch_id,
                        "problem_id": problem_id,
                        "agents_created": len(executor_result.agents_instantiated),
                    },
                }
                with event_lock:
                    frozen_events = list(event_history)
                artifacts = persist_run_artifacts(
                    run_id=run_id,
                    query=query,
                    source_path=str(repo_path),
                    db_url=db_url,
                    llm_provider=resolved_provider,
                    llm_model=resolved_model,
                    status="success",
                    events=frozen_events,
                    result_payload=result_payload,
                    error_message=None,
                )
                result_payload["artifacts"] = artifacts
                push_event(
                    "System",
                    "UI",
                    "artifact",
                    f"Saved run artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
                )
                batch_log = persist_batch_log(
                    status="success",
                    artifacts=artifacts,
                    result_payload=result_payload,
                    error_message=None,
                )
                if batch_log:
                    result_payload["batch_log"] = batch_log
                result_holder["result"] = result_payload
            except (ParserAgentError, ExecutorAgentError) as exc:
                result_holder["error"] = str(exc)
                if knowledge_store is not None:
                    knowledge_store.finish_run(
                        run_id=run_id,
                        status="error",
                        summary=str(exc),
                        confidence=None,
                        preliminary_causes=[],
                    )
                with event_lock:
                    frozen_events = list(event_history)
                artifacts = persist_run_artifacts(
                    run_id=run_id,
                    query=query,
                    source_path=str(repo_path),
                    db_url=db_url,
                    llm_provider=resolved_provider,
                    llm_model=resolved_model,
                    status="error",
                    events=frozen_events,
                    result_payload=None,
                    error_message=str(exc),
                )
                push_event(
                    "System",
                    "UI",
                    "artifact",
                    f"Saved run artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
                )
                persist_batch_log(
                    status="error",
                    artifacts=artifacts,
                    result_payload=None,
                    error_message=str(exc),
                )
            except Exception as exc:  # pragma: no cover
                result_holder["error"] = f"Unexpected error: {exc}"
                if knowledge_store is not None:
                    knowledge_store.finish_run(
                        run_id=run_id,
                        status="error",
                        summary=f"Unexpected error: {exc}",
                        confidence=None,
                        preliminary_causes=[],
                    )
                with event_lock:
                    frozen_events = list(event_history)
                artifacts = persist_run_artifacts(
                    run_id=run_id,
                    query=query,
                    source_path=str(repo_path),
                    db_url=db_url,
                    llm_provider=resolved_provider,
                    llm_model=resolved_model,
                    status="error",
                    events=frozen_events,
                    result_payload=None,
                    error_message=f"Unexpected error: {exc}",
                )
                push_event(
                    "System",
                    "UI",
                    "artifact",
                    f"Saved run artifacts: json={artifacts['json_path']} | txt={artifacts['txt_path']}",
                )
                persist_batch_log(
                    status="error",
                    artifacts=artifacts,
                    result_payload=None,
                    error_message=f"Unexpected error: {exc}",
                )
            finally:
                done.set()

        push_event(
            "User",
            "ParserAgent",
            "input",
            (
                "User entered query via UI field 'User Query (Parser Agent)':\n"
                f"{query}\n"
                f"source={repo_path}\n"
                f"db={db_url or 'none'}"
            ),
        )
        threading.Thread(target=worker, daemon=True).start()

        def stream() -> str:
            yield "data: " + json.dumps(
                {
                    "type": "run_start",
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "problem_id": problem_id,
                },
                ensure_ascii=False,
            ) + "\n\n"
            while not (done.is_set() and event_queue.empty()):
                try:
                    item = event_queue.get(timeout=0.15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

            if "result" in result_holder:
                yield (
                    "data: "
                    + json.dumps({"type": "result", "payload": result_holder["result"]}, ensure_ascii=False)
                    + "\n\n"
                )
            else:
                yield (
                    "data: "
                    + json.dumps({"type": "error", "message": result_holder.get("error", "unknown error")})
                    + "\n\n"
                )
            yield "data: " + json.dumps({"type": "end"}) + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


_INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AWARE Assess UI</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --surface-muted: #eef2f7;
      --line: #d9e1ec;
      --line-strong: #b9c6d6;
      --text: #172033;
      --muted: #65748b;
      --accent: #155e75;
      --accent-strong: #0f4b5d;
      --warn: #b45309;
      --ok: #15803d;
      --err: #b91c1c;
      --user: #e8f1ff;
      --system: #f1f5f9;
      --parser: #e8f7f4;
      --executor: #f2ecfb;
    }
    * { box-sizing: border-box; }
    html, body {
      overflow-x: hidden;
    }
    body {
      margin: 0;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        linear-gradient(180deg, #eef4f8 0, #f8fafc 280px, var(--bg) 100%);
      min-height: 100vh;
    }
    .page {
      width: min(1280px, 94vw);
      margin: 24px auto 36px;
      min-width: 0;
    }
    .masthead {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }
    .title {
      font-size: clamp(26px, 3vw, 38px);
      font-weight: 760;
      letter-spacing: 0;
      margin: 0 0 6px;
    }
    .subtitle {
      color: var(--muted);
      margin: 0;
      font-size: 15px;
      line-height: 1.45;
      max-width: 660px;
    }
    .env-pill {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.72);
      color: var(--muted);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 13px;
      white-space: nowrap;
    }
    .card {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
      border-radius: 8px;
      padding: 16px;
      box-shadow: 0 18px 48px rgba(21, 31, 48, 0.08);
      min-width: 0;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 2fr 2fr 1fr;
      gap: 12px;
      margin-bottom: 12px;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 6px;
      display: block;
    }
    input, select, textarea, button {
      width: 100%;
      background: var(--surface);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 11px;
      font: inherit;
      font-size: 14px;
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease, background-color .15s ease;
    }
    input:focus, textarea:focus {
      border-color: #4c9bb0;
      box-shadow: 0 0 0 3px rgba(21, 94, 117, 0.14);
    }
    textarea {
      min-height: 112px;
      resize: vertical;
      line-height: 1.45;
    }
    .run-btn {
      margin-top: 12px;
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      font-weight: 720;
      cursor: pointer;
      width: auto;
      min-width: 160px;
      padding-inline: 18px;
    }
    .run-btn:hover:not(:disabled) {
      background: var(--accent-strong);
      border-color: var(--accent-strong);
    }
    .run-btn:disabled { opacity: .6; cursor: default; }
    .running {
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
    }
    .panels {
      display: grid;
      grid-template-columns: minmax(0, 2.2fr) minmax(320px, 1fr);
      gap: 16px;
      margin-top: 16px;
      min-width: 0;
    }
    .panel-title {
      color: var(--text);
      font-size: 15px;
      font-weight: 740;
      margin: 0 0 10px;
    }
    .chat-box, .tasks-box {
      width: 100%;
      height: 470px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface-soft);
      padding: 10px;
      min-width: 0;
    }
    .msg-row {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      gap: 10px;
      margin-bottom: 10px;
      align-items: start;
      min-width: 0;
    }
    .avatar-wrap {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }
    .avatar {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      font-size: 12px;
      font-weight: 780;
      border: 1px solid var(--line-strong);
      color: #1f2937;
    }
    .avatar.user { background: var(--user); color: #1d4ed8; }
    .avatar.system { background: var(--system); color: #475569; }
    .avatar.parser { background: var(--parser); color: #0f766e; border-color: #a7d8d1; }
    .avatar.executor { background: var(--executor); color: #6d28d9; border-color: #d3c0ef; }
    .avatar.agent { background: #ecfdf5; color: #166534; }
    .avatar-initials { display: none; }
    .bubble {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      background: var(--surface);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
      line-height: 1.42;
      font-size: 13px;
      max-width: 100%;
      min-width: 0;
    }
    .bubble.user { background: var(--user); border-color: #bfd5f7; }
    .bubble.system { background: var(--system); }
    .bubble.parser { background: var(--parser); border-color: #a7d8d1; }
    .bubble.executor { background: var(--executor); border-color: #d3c0ef; }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
      font-weight: 650;
    }
    .empty {
      color: var(--muted);
      font-size: 13px;
      padding: 12px;
      border: 1px dashed var(--line-strong);
      border-radius: 6px;
      background: var(--surface);
    }
    .task-item {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      padding: 10px;
      margin-bottom: 8px;
    }
    .task-title {
      font-size: 13px;
      color: var(--text);
      line-height: 1.35;
      margin-top: 4px;
    }
    .task-updated {
      font-size: 11px;
      color: var(--muted);
      margin-top: 4px;
    }
    .agent-list {
      display: grid;
      gap: 8px;
    }
    .agent-pill {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      padding: 9px 10px;
      font-size: 13px;
      color: var(--text);
      line-height: 1.25;
    }
    .task-lane {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      margin-bottom: 10px;
      background: var(--surface);
    }
    .task-lane-title {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.35px;
      margin-bottom: 7px;
    }
    .task-lane-empty {
      font-size: 11px;
      color: var(--muted);
      border: 1px dashed var(--line-strong);
      border-radius: 6px;
      padding: 6px;
      background: var(--surface-soft);
    }
    .task-lane-running { border-color: #93c5fd; }
    .task-lane-running .task-lane-title { color: #2563eb; }
    .task-lane-waiting { border-color: #f6d58b; }
    .task-lane-waiting .task-lane-title { color: #b45309; }
    .task-lane-success { border-color: #9bd8b1; }
    .task-lane-success .task-lane-title { color: var(--ok); }
    .task-lane-error { border-color: #f4a4a4; }
    .task-lane-error .task-lane-title { color: var(--err); }
    .task-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 3px;
    }
    .task-agent { color: var(--text); font-size: 13px; font-weight: 690; margin-bottom: 3px; }
    .task-status {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.25px;
      padding: 2px 6px;
      border-radius: 999px;
      border: 1px solid transparent;
    }
    .task-status-running { color: #1d4ed8; background: #eff6ff; border-color: #bfdbfe; }
    .task-status-success { color: var(--ok); background: #f0fdf4; border-color: #bbf7d0; }
    .task-status-error { color: var(--err); background: #fef2f2; border-color: #fecaca; }
    .task-status-waiting { color: var(--warn); background: #fffbeb; border-color: #fde68a; }
    .task-phase { color: #0f766e; font-size: 12px; margin-bottom: 3px; }
    .task-content { font-size: 13px; line-height: 1.35; color: var(--text); }
    .task-steps {
      margin-top: 6px;
      border-top: 1px dashed var(--line);
      padding-top: 6px;
    }
    .task-step {
      font-size: 11px;
      line-height: 1.25;
      color: var(--muted);
      margin-bottom: 3px;
    }
    .result-block {
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface-soft);
      padding: 10px;
    }
    pre {
      margin: 0;
      color: #243247;
      max-height: 260px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.35;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    .status-ok { color: var(--ok); }
    .status-err { color: var(--err); }
    .typing {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
    }
    .typing-dots {
      display: inline-flex;
      gap: 4px;
    }
    .typing-dots span {
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: var(--accent);
      opacity: 0.35;
      animation: typingPulse 1.2s infinite ease-in-out;
    }
    .typing-dots span:nth-child(2) { animation-delay: .15s; }
    .typing-dots span:nth-child(3) { animation-delay: .3s; }
    @keyframes typingPulse {
      0%, 80%, 100% { transform: scale(.7); opacity: .35; }
      40% { transform: scale(1); opacity: 1; }
    }
    @media (max-width: 1150px) {
      .masthead { display: block; }
      .env-pill { display: inline-block; margin-top: 12px; }
      .form-grid { grid-template-columns: 1fr 1fr; }
      .panels { grid-template-columns: 1fr; }
      .chat-box, .tasks-box { height: 420px; }
    }
    @media (max-width: 720px) {
      .form-grid { grid-template-columns: 1fr; }
      .page { width: min(100% - 24px, 1280px); margin-top: 16px; }
      .card { padding: 12px; }
      .run-btn { width: 100%; }
      .msg-row { grid-template-columns: 36px minmax(0, 1fr); }
      .avatar { width: 30px; height: 30px; font-size: 11px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="masthead">
      <div>
        <h1 class="title">AWARE Assess</h1>
        <p class="subtitle">Run parser and executor agents against telemetry data, then review the BuildSpec, findings, and root-cause synthesis in one place.</p>
      </div>
      <div class="env-pill">Parser + Executor runtime</div>
    </header>

    <section class="card">
      <div class="form-grid">
        <div>
          <label class="label">Source Path (directory or JSON)</label>
          <input id="sourcePath" type="text" placeholder="/path/to/repository" />
        </div>
        <div>
          <label class="label">DB URL (optional)</label>
          <input id="dbUrl" type="text" placeholder="sqlite:///examples/example.db" />
        </div>
        <div>
          <label class="label">Max Agents (optional)</label>
          <input id="maxAgents" type="number" min="1" step="1" placeholder="5" />
        </div>
      </div>

      <label class="label">User Query (Parser Agent)</label>
      <textarea id="userQuery" placeholder="On March 4, 2021, between 18:30 and 19:00, a failure occurred..."></textarea>

      <button class="run-btn" id="runAssessBtn">Run Assess</button>
      <div id="runningText" class="running">Ready.</div>
    </section>

    <section class="panels">
      <div class="card">
        <h2 class="panel-title">Live Agent Conversation</h2>
        <div id="chatBox" class="chat-box"></div>
        <div class="result-block">
          <div class="panel-title">BuildSpec (Parser Output)</div>
          <pre id="buildSpecOutput">{}</pre>
        </div>
        <div class="result-block">
          <div class="panel-title">BuildSpec Resolution Scope</div>
          <pre id="scopeOutput">{}</pre>
        </div>
        <div class="result-block">
          <div class="panel-title">Root Cause Synthesis</div>
          <pre id="rootCauseCandidates">{}</pre>
        </div>
        <div class="result-block">
          <div class="panel-title">Findings</div>
          <pre id="findingsOutput">[]</pre>
        </div>
        <div class="result-block">
          <div class="panel-title">Anomalies</div>
          <pre id="anomaliesOutput">[]</pre>
        </div>
        <div class="result-block">
          <div class="panel-title">Preliminary Causes</div>
          <pre id="preliminaryCausesOutput">[]</pre>
        </div>
        <div class="result-block">
          <div class="panel-title">Final Reporting</div>
          <pre id="finalReportingOutput">{}</pre>
        </div>
      </div>
      <div class="card">
        <h2 class="panel-title">Current Agent Tasks</h2>
        <div id="tasksBox" class="tasks-box"></div>
      </div>
    </section>
  </main>

  <script>
    const chatBox = document.getElementById("chatBox");
    const tasksBox = document.getElementById("tasksBox");
    const buildSpecOutput = document.getElementById("buildSpecOutput");
    const scopeOutput = document.getElementById("scopeOutput");
    const rootCauseCandidates = document.getElementById("rootCauseCandidates");
    const findingsOutput = document.getElementById("findingsOutput");
    const anomaliesOutput = document.getElementById("anomaliesOutput");
    const preliminaryCausesOutput = document.getElementById("preliminaryCausesOutput");
    const finalReportingOutput = document.getElementById("finalReportingOutput");
    const runningText = document.getElementById("runningText");
    const runBtn = document.getElementById("runAssessBtn");
    let evtSource = null;
    const taskState = new Map();
    let runLoaderEl = null;

    function esc(s) {
      return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function nowHms() {
      const d = new Date();
      return d.toTimeString().slice(0, 8);
    }

    function senderProfile(sender) {
      if (sender === "User") return { cls: "user", label: "User", initials: "US" };
      if (sender === "System") return { cls: "system", label: "System", initials: "SY" };
      if (sender === "ParserAgent") return { cls: "parser", label: "ParserAgent", initials: "PA" };
      if (sender === "ExecutorAgent") return { cls: "executor", label: "ExecutorAgent", initials: "EA" };
      if (String(sender || "").indexOf("ParserAgent") >= 0) return { cls: "parser", label: sender, initials: "PA" };
      if (String(sender || "").indexOf("ExecutorAgent") >= 0) return { cls: "executor", label: sender, initials: "EA" };
      return { cls: "agent", label: sender || "Agent", initials: "AG" };
    }

    function addChatMsg(sender, recipient, phase, content) {
      const profile = senderProfile(sender);
      const cls = profile.cls;
      const senderLabel = profile.label;
      const row = document.createElement("div");
      row.className = "msg-row";
      row.innerHTML = `
        <div class="avatar-wrap">
          <div class="avatar ${cls}" title="${esc(senderLabel)}">${esc(profile.initials)}</div>
          <div class="avatar-initials">${esc(profile.initials)}</div>
        </div>
        <div class="bubble ${cls}">
          <div class="meta">${nowHms()} | ${esc(senderLabel)} -> ${esc(recipient)} | ${esc(phase)}</div>
          <div>${esc(content)}</div>
        </div>
      `;
      chatBox.appendChild(row);
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    function showRunLoader() {
      if (runLoaderEl) return;
      const row = document.createElement("div");
      row.className = "msg-row";
      row.innerHTML = `
        <div class="avatar-wrap">
          <div class="avatar parser" title="ParserAgent">PA</div>
          <div class="avatar-initials">PA</div>
        </div>
        <div class="bubble parser">
          <div class="meta">${nowHms()} | ParserAgent -> Runtime | running</div>
          <div class="typing">
            <span>Assess runtime is analyzing (Parser + Executor)</span>
            <span class="typing-dots"><span></span><span></span><span></span></span>
          </div>
        </div>
      `;
      runLoaderEl = row;
      chatBox.appendChild(runLoaderEl);
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    function hideRunLoader() {
      if (!runLoaderEl) return;
      runLoaderEl.remove();
      runLoaderEl = null;
    }

    function replaceUnknownValues(obj) {
      if (Array.isArray(obj)) {
        return obj.map(replaceUnknownValues);
      }
      if (obj && typeof obj === "object") {
        const out = {};
        for (const [k, v] of Object.entries(obj)) {
          out[k] = replaceUnknownValues(v);
        }
        return out;
      }
      if (typeof obj === "string" && obj.toLowerCase() === "unknown") {
        return "to_be_determined";
      }
      return obj;
    }

    function updateTask(sender, phase, content) {
      if (!sender || String(sender).indexOf("Agent") < 0) return;
      const prev = taskState.get(sender) || {
        agent: sender,
        status: "waiting",
        title: "Waiting for task...",
        updatedAt: nowHms(),
      };
      const next = {
        agent: sender,
        status: taskStatusFromPhase(phase, prev.status),
        title: taskTitleFromEvent(phase, content, prev.title),
        updatedAt: nowHms(),
      };
      taskState.set(sender, next);
      renderTasks();
    }

    function taskStatusFromPhase(phase, previousStatus) {
      const p = String(phase || "").toLowerCase();
      if (previousStatus === "error") return "error";
      if (previousStatus === "success" && p !== "failed" && p !== "error") return "success";
      if (p === "failed" || p === "error") return "error";
      if (p === "success" || p === "summary" || p === "buildspec" || p === "agent_result") return "success";
      if (p === "status" && previousStatus === "waiting") return "waiting";
      return "running";
    }

    function taskTitleFromEvent(phase, content, previousTitle) {
      const p = String(phase || "").toLowerCase();
      const map = {
        init: "Initializing agent",
        input: "Reading user input",
        load_knowledge: "Loading knowledge context",
        explore_repo: "Scanning repository",
        read_file: "Reading file",
        thinking: "Generating BuildSpec",
        llm_output: "Parsing model output",
        normalization: "Normalizing output",
        validation: "Validating BuildSpec",
        repair: "Repairing invalid output",
        selection: "Selecting target files",
        reasoning_disabled: "Running deterministic logic",
        load_templates: "Loading agent templates",
        plan_targets: "Planning telemetry targets",
        instantiate_agent: "Instantiating sub-agent",
        dispatch: "Executing assigned analysis",
        read_shared_memory: "Reading shared memory",
        finding: "Publishing finding",
        agent_result: "Task completed",
        buildspec: "BuildSpec created",
        summary: "Assess summary ready",
        success: "Completed successfully",
        failed: "Execution failed",
        error: "Execution error",
      };
      if (map[p]) return map[p];
      const message = String(content || "").trim().split("\\n")[0];
      if (message) return message.slice(0, 80);
      return previousTitle || "Running task";
    }

    function renderTasks() {
      const entries = [...taskState.entries()];
      if (!entries.length) {
        tasksBox.innerHTML = '<div class="empty">No active agent tasks yet.</div>';
        return;
      }
      // Preserve agent creation order (Map insertion order), do not sort alphabetically.
      const agents = entries.map(([, state]) => state);
      tasksBox.innerHTML = `
        <div>
          ${agents.map((state) => `
            <div class="task-item">
              <div class="task-head">
                <div class="task-agent">${esc(state.agent)}</div>
                <span class="task-status task-status-${esc(state.status)}">${esc(String(state.status).toUpperCase())}</span>
              </div>
              <div class="task-title">${esc(state.title)}</div>
              <div class="task-updated">updated ${esc(state.updatedAt)}</div>
            </div>
          `).join("")}
        </div>
      `;
    }

    function startRun() {
      try {
        const sourcePath = document.getElementById("sourcePath").value.trim();
        const dbUrl = document.getElementById("dbUrl").value.trim();
        const userQuery = document.getElementById("userQuery").value.trim();
        const maxAgentsRaw = document.getElementById("maxAgents").value.trim();

        if (!sourcePath || !userQuery) {
          runningText.innerHTML = '<span class="status-err">Source path and query are required.</span>';
          addChatMsg("System", "UI", "validation", "Source path and query are required.");
          return;
        }
        if (maxAgentsRaw && !/^\\d+$/.test(maxAgentsRaw)) {
          runningText.innerHTML = '<span class="status-err">Max Agents must be a positive integer.</span>';
          addChatMsg("System", "UI", "validation", "Max Agents must be a positive integer.");
          return;
        }

        if (typeof EventSource === "undefined") {
          runningText.innerHTML = '<span class="status-err">EventSource is not supported by this browser.</span>';
          addChatMsg("System", "UI", "error", "EventSource is not supported by this browser.");
          return;
        }

        if (evtSource) evtSource.close();
        chatBox.innerHTML = '<div class="empty">Run started. Live conversation will appear here.</div>';
        tasksBox.innerHTML = '<div class="empty">Waiting for agent activity...</div>';
        buildSpecOutput.textContent = "{}";
        scopeOutput.textContent = "{}";
        rootCauseCandidates.textContent = "{}";
        findingsOutput.textContent = "[]";
        anomaliesOutput.textContent = "[]";
        preliminaryCausesOutput.textContent = "[]";
        finalReportingOutput.textContent = "{}";
        taskState.clear();
        runBtn.disabled = true;
        runningText.textContent = "Starting run...";
        showRunLoader();
        addChatMsg("System", "UI", "status", "Connecting to /api/parse-stream...");

        const params = new URLSearchParams({
          query: userQuery,
          source_path: sourcePath,
          db_url: dbUrl,
          buildspec_only: "false",
        });
        if (maxAgentsRaw) {
          params.set("max_agents", maxAgentsRaw);
        }

        evtSource = new EventSource(`/api/parse-stream?${params.toString()}`);
        evtSource.onopen = () => {
          addChatMsg("System", "UI", "status", "Stream connected.");
        };
        evtSource.onmessage = (evt) => {
          let data = null;
          try {
            data = JSON.parse(evt.data);
          } catch (e) {
            addChatMsg("System", "UI", "error", `Invalid stream payload: ${String(e)}`);
            return;
          }

          if (data.type === "run_start") {
            runningText.textContent = `Running: ${data.run_id}`;
            return;
          }

          if (data.type === "event") {
            addChatMsg(data.sender, data.recipient || "Runtime", data.phase, data.content);
            updateTask(data.sender, data.phase, data.content);
            if (runLoaderEl) {
              chatBox.appendChild(runLoaderEl);
              chatBox.scrollTop = chatBox.scrollHeight;
            }
            return;
          }

          if (data.type === "parser_result") {
            const parserPayload = data && data.payload ? data.payload : {};
            const bs = parserPayload && parserPayload.buildspec ? parserPayload.buildspec : {};
            const attempts = parserPayload && parserPayload.attempts ? parserPayload.attempts : "n/a";
            buildSpecOutput.textContent = JSON.stringify(replaceUnknownValues(bs), null, 2);
            addChatMsg(
              "ParserAgent",
              "Runtime",
              "buildspec",
              `BuildSpec created (attempts=${attempts}).`,
            );
            updateTask("ParserAgent", "buildspec", "BuildSpec created and available in panel.");
            return;
          }

          if (data.type === "result") {
            hideRunLoader();
            const payload = data && data.payload ? data.payload : {};
            const parser = payload && payload.parser ? payload.parser : null;
            if (parser && parser.buildspec) {
              buildSpecOutput.textContent = JSON.stringify(replaceUnknownValues(parser.buildspec), null, 2);
            }
            const executor = payload && payload.executor ? payload.executor : null;
            if (!executor) {
              rootCauseCandidates.textContent = "[]";
              addChatMsg(
                "ParserAgent",
                "Runtime",
                "success",
                "BuildSpec created successfully. Executor not started (BuildSpec-only mode).",
              );
              updateTask("ParserAgent", "success", "BuildSpec created.");
              runningText.innerHTML = '<span class="status-ok">BuildSpec created successfully.</span>';
              return;
            }
            const summary = executor && executor.summary ? executor.summary : "n/a";
            const confidence = (!executor || executor.confidence === null || executor.confidence === undefined)
              ? "n/a"
              : executor.confidence;
            const assessment = payload && payload.assessment_output ? payload.assessment_output : {};
            const scope = assessment && assessment.buildspec_resolution_scope ? assessment.buildspec_resolution_scope : {};
            const causes = Array.isArray(assessment.preliminary_causes) ? assessment.preliminary_causes : [];
            const findings = Array.isArray(assessment.findings) ? assessment.findings : [];
            const anomalies = Array.isArray(assessment.anomalies) ? assessment.anomalies : [];
            const synthesis = assessment && assessment.root_cause_synthesis ? assessment.root_cause_synthesis : {};
            const finalReporting = assessment && assessment.final_reporting ? assessment.final_reporting : {};
            scopeOutput.textContent = JSON.stringify(scope, null, 2);
            rootCauseCandidates.textContent = JSON.stringify(synthesis, null, 2);
            findingsOutput.textContent = JSON.stringify(findings, null, 2);
            anomaliesOutput.textContent = JSON.stringify(anomalies, null, 2);
            preliminaryCausesOutput.textContent = JSON.stringify(causes, null, 2);
            finalReportingOutput.textContent = JSON.stringify(finalReporting, null, 2);
            addChatMsg(
              "ExecutorAgent",
              "Runtime",
              "summary",
              `Summary: ${summary}\nConfidence: ${confidence}\nFindings: ${findings.length}\nAnomalies: ${anomalies.length}\nPreliminary causes:\n${causes.map(function(x){ return "- " + x; }).join("\\n")}`,
            );
            updateTask("ExecutorAgent", "summary", "Executor completed assess execution.");
            runningText.innerHTML = '<span class="status-ok">Run completed successfully.</span>';
            return;
          }

          if (data.type === "error") {
            hideRunLoader();
            addChatMsg("System", "UI", "error", data.message || "Unknown error");
            runningText.innerHTML = '<span class="status-err">Run failed.</span>';
            return;
          }

          if (data.type === "end") {
            hideRunLoader();
            runBtn.disabled = false;
            if (evtSource) evtSource.close();
          }
        };

        evtSource.onerror = () => {
          hideRunLoader();
          addChatMsg("System", "UI", "error", "Stream connection failed or was interrupted.");
          runningText.innerHTML = '<span class="status-err">Stream error.</span>';
          runBtn.disabled = false;
          if (evtSource) evtSource.close();
        };
      } catch (err) {
        hideRunLoader();
        runBtn.disabled = false;
        addChatMsg("System", "UI", "error", `Frontend error: ${String(err)}`);
        runningText.innerHTML = '<span class="status-err">Frontend error.</span>';
      }
    }

    document.getElementById("runAssessBtn").addEventListener("click", startRun);

    (async () => {
      try {
        const res = await fetch("/api/config");
        if (!res.ok) return;
        const cfg = await res.json();
        if (cfg.ui_default_source_path) {
          document.getElementById("sourcePath").value = cfg.ui_default_source_path;
        }
        if (cfg.executor_max_agents) {
          document.getElementById("maxAgents").value = cfg.executor_max_agents;
        }
      } catch (_) {}
    })();

    renderTasks();
  </script>
</body>
</html>
"""

# ASGI application instance (kept for direct `uvicorn ui.server:app` runs).
app = create_app()
