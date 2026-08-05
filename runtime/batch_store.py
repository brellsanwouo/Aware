"""Backend persistence for browser-orchestrated assessment batches."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def validate_batch_identifier(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not _SAFE_ID.fullmatch(clean):
        raise ValueError(f"Invalid {label}: {value!r}")
    return clean


def batch_directory(batch_id: str, output_root: str | Path = "output") -> Path:
    safe_batch = validate_batch_identifier(batch_id, "batch_id")
    return Path(output_root).resolve() / "batches" / safe_batch


def incident_log_path(
    batch_id: str,
    problem_id: str,
    output_root: str | Path = "output",
) -> Path:
    safe_problem = validate_batch_identifier(problem_id, "problem_id")
    return batch_directory(batch_id, output_root) / "incidents" / f"{safe_problem}.json"


def record_batch_incident(
    *,
    batch_id: str,
    problem_id: str,
    run_id: str,
    status: str,
    agents_created: int,
    events: list[dict[str, Any]],
    artifacts: dict[str, str],
    result_payload: dict[str, Any] | None,
    error_message: str | None,
    output_root: str | Path = "output",
) -> dict[str, str]:
    """Write a complete incident journal and update its batch manifest."""
    directory = batch_directory(batch_id, output_root)
    incident_path = incident_log_path(batch_id, problem_id, output_root)
    text_path = incident_path.with_suffix(".log")
    manifest_path = directory / "manifest.json"
    incident_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "batch_id": batch_id,
        "problem_id": problem_id,
        "run_id": run_id,
        "status": status,
        "agents_created": int(agents_created),
        "updated_at": now,
        "artifacts": artifacts,
        "error": error_message,
        "events": events,
        "result": result_payload,
    }
    lines = [
        f"batch_id: {batch_id}",
        f"problem_id: {problem_id}",
        f"run_id: {run_id}",
        f"status: {status}",
        f"agents_created: {agents_created}",
        f"updated_at: {now}",
        "",
        "events:",
    ]
    for event in events:
        lines.append(
            "[{timestamp}] {sender} -> {recipient} | {phase} | {content}".format(
                timestamp=event.get("timestamp", ""),
                sender=event.get("sender", ""),
                recipient=event.get("recipient", ""),
                phase=event.get("phase", ""),
                content=str(event.get("content", "")).replace("\n", "\\n"),
            )
        )
    lines.extend(["", "artifacts:", json.dumps(artifacts, ensure_ascii=False, indent=2)])
    if error_message:
        lines.extend(["", f"error: {error_message}"])
    lines.extend(
        [
            "",
            "result_json:",
            json.dumps(result_payload or {}, ensure_ascii=False, indent=2),
        ]
    )
    with _LOCK:
        incident_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest: dict[str, Any] = {"batch_id": batch_id, "incidents": []}
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, json.JSONDecodeError):
                pass
        incidents = manifest.get("incidents", [])
        if not isinstance(incidents, list):
            incidents = []
        entry = {
            "problem_id": problem_id,
            "run_id": run_id,
            "status": status,
            "agents_created": int(agents_created),
            "updated_at": now,
            "incident_json_path": str(incident_path),
            "incident_log_path": str(text_path),
            "artifacts": artifacts,
        }
        incidents = [
            item
            for item in incidents
            if isinstance(item, dict) and item.get("problem_id") != problem_id
        ]
        incidents.append(entry)
        manifest.update({"batch_id": batch_id, "updated_at": now, "incidents": incidents})
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "batch_manifest_path": str(manifest_path),
        "batch_incident_json_path": str(incident_path),
        "batch_incident_log_path": str(text_path),
    }
