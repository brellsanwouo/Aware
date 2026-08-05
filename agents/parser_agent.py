"""Google ADK ParserAgent for BuildSpec extraction."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from agents.base import Agent
from llm.base import LLMClient, LLMError
from llm.factory import create_llm_client
from llm.schemas import BUILDSPEC_RESPONSE_FORMAT
from aware_models.buildspec import BuildSpec, validate_buildspec
from tools import time_tools


class ParserAgentError(RuntimeError):
    """Raised when parser fails to produce a valid BuildSpec."""


class ParserRunResult(BaseModel):
    """Structured parser output."""

    buildspec: BuildSpec
    attempts: int
    errors_by_attempt: list[list[str]] = Field(default_factory=list)
    raw_responses: list[str] = Field(default_factory=list)
    repository_files: list[str] = Field(default_factory=list)
    selected_paths: dict[str, list[str]] = Field(default_factory=dict)


class ParserEvent(BaseModel):
    """One parser conversation event."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sender: str
    recipient: str
    phase: str
    content: str


class ParserAgent(Agent):
    """ADK parser agent that extracts and validates BuildSpec via LLM retries."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    def __init__(
        self,
        llm_client: LLMClient | None,
        max_attempts: int = 5,
        knowledge_file: str | None = None,
        enable_reasoning: bool = True,
        enable_memory: bool = True,
    ) -> None:
        super().__init__(
            name="ParserAgent",
            description="Parse user query + repository into a validated BuildSpec.",
        )
        self.llm_client = llm_client
        self.max_attempts = max(1, int(max_attempts))
        self.enable_reasoning = bool(enable_reasoning)
        self.enable_memory = bool(enable_memory)
        self.knowledge_file = knowledge_file or os.getenv(
            "AWARE_PARSER_KB_FILE",
            "knowledge/parser_buildspec_kb.md",
        )

    def generate_buildspec(
        self,
        user_query: str,
        repository_path: str,
        on_event: Callable[[ParserEvent], None] | None = None,
        timezone_offset_minutes: int = 480,
        dataset_profile: str = "generic",
    ) -> ParserRunResult:
        """Generate BuildSpec with retry-until-valid workflow."""
        repo = Path(repository_path)
        if not repo.is_absolute():
            repo = (Path.cwd() / repo).resolve()
        self._emit(
            on_event,
            phase="init",
            recipient="Runtime",
            content=(
                f"ParserAgent received request. repository={repo}; max_attempts={self.max_attempts}; "
                f"reasoning={'on' if self.enable_reasoning else 'off'}; "
                f"memory={'on' if self.enable_memory else 'off'}."
                f" timezone_offset_minutes={timezone_offset_minutes}; dataset={dataset_profile}."
            ),
        )
        self._emit(
            on_event,
            phase="input",
            recipient="Runtime",
            content=f"Received user query: {user_query}",
        )
        repository_files = _collect_repository_files(repo)
        self._emit(
            on_event,
            phase="explore_repo",
            recipient="Runtime",
            content=f"Read repo... found {len(repository_files)} files.",
        )
        if repository_files:
            self._emit(
                on_event,
                phase="explore_repo",
                recipient="Runtime",
                content="Listing a bounded sample of repository files...",
            )
            event_file_limit = 20
            for rel_path in repository_files[:event_file_limit]:
                self._emit(
                    on_event,
                    phase="discover_file",
                    recipient="Runtime",
                    content=f"discovered path <{rel_path}> (content not read)",
                )
            omitted = len(repository_files) - event_file_limit
            if omitted > 0:
                self._emit(
                    on_event,
                    phase="discover_file_summary",
                    recipient="Runtime",
                    content=f"Repository scan retained all paths internally; omitted {omitted} per-file UI events.",
                )

        errors_by_attempt: list[list[str]] = []
        raw_responses: list[str] = []
        previous_errors: list[str] = []
        knowledge_source, knowledge_text = _load_parser_knowledge(self.knowledge_file)
        self._emit(
            on_event,
            phase="load_knowledge",
            recipient="Runtime",
            content=(
                f"Loaded parser knowledge from {knowledge_source} "
                f"(chars={len(knowledge_text)})."
            ),
        )

        llm_client = self._ensure_llm_client(on_event)

        # Batch catalogues already provide a canonical date/time request and an
        # explicit dataset profile. File selection and BuildSpec validation are
        # deterministic here; asking an LLM to repeat that work adds latency and
        # variability without adding RCA evidence. Free-form/generic requests
        # continue through the retrying LLM parser below.
        profile = dataset_profile.strip().lower()
        canonical_incident = bool(
            profile in {"nezha", "openrca_bank", "openrca_market", "openrca_telecom"}
            and re.search(r"\b\d{4}-\d{2}-\d{2}\b", user_query)
            and len(re.findall(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", user_query)) >= 2
        )
        if canonical_incident:
            payload = _buildspec_payload_without_llm(
                user_query=user_query,
                repository_path=repo,
                repository_files=repository_files,
                timezone_offset_minutes=timezone_offset_minutes,
                dataset_profile=profile,
            )
            validation = validate_buildspec(
                payload=payload,
                expected_repository_path=str(repo),
            )
            selected_paths = {
                "absolute_log_file": _as_str_list(payload.get("absolute_log_file")),
                "absolute_trace_file": _as_str_list(payload.get("absolute_trace_file")),
                "absolute_metrics_file": _as_str_list(payload.get("absolute_metrics_file")),
            }
            path_errors = _validate_selected_paths_against_scan(
                selected_paths=selected_paths,
                repository_path=repo,
                repository_files=repository_files,
            )
            if validation.is_valid and not path_errors and validation.normalized is not None:
                self._emit(
                    on_event,
                    phase="deterministic_buildspec",
                    recipient="Runtime",
                    content=(
                        f"Canonical {profile} incident parsed without a Parser LLM call; "
                        f"selected logs={len(selected_paths['absolute_log_file'])}, "
                        f"traces={len(selected_paths['absolute_trace_file'])}, "
                        f"metrics={len(selected_paths['absolute_metrics_file'])}."
                    ),
                )
                self._emit(
                    on_event,
                    phase="success",
                    recipient="Runtime",
                    content="BuildSpec validated successfully through the canonical incident fast path.",
                )
                return ParserRunResult(
                    buildspec=validation.normalized,
                    attempts=0,
                    errors_by_attempt=[],
                    raw_responses=[],
                    repository_files=repository_files,
                    selected_paths=selected_paths,
                )

        for attempt in range(1, self.max_attempts + 1):
            self._emit(
                on_event,
                phase="thinking",
                recipient="LLM",
                content=f"Attempt {attempt}/{self.max_attempts}: generating BuildSpec.",
            )
            system_prompt, user_prompt = self._build_prompts(
                user_query=user_query,
                repository_path=str(repo),
                repository_files=repository_files,
                previous_errors=previous_errors,
                knowledge_text=knowledge_text,
                timezone_offset_minutes=timezone_offset_minutes,
            )
            try:
                response_text = llm_client.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_format=BUILDSPEC_RESPONSE_FORMAT,
                )
            except LLMError as exc:
                previous_errors = [f"llm: {exc}"]
                errors_by_attempt.append(previous_errors)
                raw_responses.append("")
                self._emit(
                    on_event,
                    phase="error",
                    recipient="Runtime",
                    content=f"LLM call failed on attempt {attempt}: {exc}",
                )
                continue

            raw_responses.append(response_text)
            self._emit(
                on_event,
                phase="llm_output",
                recipient="Validator",
                content=f"LLM returned response on attempt {attempt} (chars={len(response_text)}).",
            )
            payload = _extract_json_payload(response_text)
            if payload is None:
                previous_errors = ["buildspec: LLM did not return a valid JSON object."]
                errors_by_attempt.append(previous_errors)
                self._emit(
                    on_event,
                    phase="validation",
                    recipient="LLM",
                    content="Validation failed: response is not a valid JSON object.",
                )
                continue

            normalized_payload = _normalize_candidate_payload(
                payload=payload,
                repository_path=repo,
                repository_files=repository_files,
                user_query=user_query,
                timezone_offset_minutes=timezone_offset_minutes,
                dataset_profile=dataset_profile,
            )
            if normalized_payload != payload:
                self._emit(
                    on_event,
                    phase="normalization",
                    recipient="Validator",
                    content=(
                        f"Applied payload normalization before validation (attempt {attempt})."
                    ),
                )

            validation = validate_buildspec(
                payload=normalized_payload,
                expected_repository_path=str(repo),
            )
            selected_paths = {
                "absolute_log_file": _as_str_list(normalized_payload.get("absolute_log_file")),
                "absolute_trace_file": _as_str_list(normalized_payload.get("absolute_trace_file")),
                "absolute_metrics_file": _as_str_list(normalized_payload.get("absolute_metrics_file")),
            }
            path_errors = _validate_selected_paths_against_scan(
                selected_paths=selected_paths,
                repository_path=repo,
                repository_files=repository_files,
            )
            task_type_errors = _validate_task_type_against_query(
                payload=normalized_payload,
                user_query=user_query,
            )
            uncertainty_scope_errors = _validate_uncertainty_matches_task_type(
                payload=normalized_payload,
            )
            combined_errors = path_errors + task_type_errors + uncertainty_scope_errors
            if validation.is_valid and not combined_errors and validation.normalized is not None:
                self._emit(
                    on_event,
                    phase="selection",
                    recipient="Runtime",
                    content=(
                        "Resolved file links:\n"
                        f"- repo={repo}\n"
                        f"- log={', '.join(selected_paths['absolute_log_file'])}\n"
                        f"- trace={', '.join(selected_paths['absolute_trace_file'])}\n"
                        f"- metrics={', '.join(selected_paths['absolute_metrics_file'])}"
                    ),
                )
                self._emit(
                    on_event,
                    phase="success",
                    recipient="Runtime",
                    content=f"BuildSpec validated successfully at attempt {attempt}.",
                )
                return ParserRunResult(
                    buildspec=validation.normalized,
                    attempts=attempt,
                    errors_by_attempt=errors_by_attempt,
                    raw_responses=raw_responses,
                    repository_files=repository_files,
                    selected_paths=selected_paths,
                )
            previous_errors = validation.errors + combined_errors
            errors_by_attempt.append(previous_errors)
            self._emit(
                on_event,
                phase="validation",
                recipient="LLM",
                content=(
                    f"BuildSpec invalid at attempt {attempt}. "
                    f"Errors: {'; '.join(previous_errors[:6])}"
                ),
            )
            self._emit(
                on_event,
                phase="repair",
                recipient="LLM",
                content="Requesting corrected BuildSpec from LLM using validation errors.",
            )

        self._emit(
            on_event,
            phase="failed",
            recipient="Runtime",
            content=(
                f"Failed after {self.max_attempts} attempts. "
                f"Last errors: {'; '.join(previous_errors[:6])}"
            ),
        )
        raise ParserAgentError(
            "ParserAgent failed to produce a valid BuildSpec after "
            f"{self.max_attempts} attempts. Last errors: {previous_errors}"
        )

    def _ensure_llm_client(
        self,
        on_event: Callable[[ParserEvent], None] | None = None,
    ) -> LLMClient:
        """Return the parser LLM client, creating it from environment when missing."""
        if self.llm_client is not None:
            return self.llm_client
        provider = (os.getenv("AWARE_LLM_PROVIDER", "openai-compatible") or "openai-compatible").strip()
        model = os.getenv("OPENAI_MODEL")
        try:
            self.llm_client = create_llm_client(
                provider=provider,
                openai_api_key=os.getenv("OPENAI_API_KEY"),
                openai_base_url=os.getenv("OPENAI_BASE_URL"),
                openai_model=model,
            )
        except (ValueError, LLMError) as exc:
            raise ParserAgentError(
                f"ParserAgent could not configure LLM client from environment: {exc}"
            ) from exc
        self._emit(
            on_event,
            phase="llm_config",
            recipient="Runtime",
            content=(
                "ParserAgent configured LLM client from environment. "
                f"provider={provider}; model={model or 'default'}."
            ),
        )
        return self.llm_client

    def _build_prompts(
        self,
        user_query: str,
        repository_path: str,
        repository_files: list[str],
        previous_errors: list[str],
        knowledge_text: str,
        timezone_offset_minutes: int,
    ) -> tuple[str, str]:
        system_prompt = (
            "You are ParserAgent for RCA Assess.\n"
            "Return ONLY one valid JSON object with EXACT keys.\n"
            "Do not add any text before or after JSON.\n"
            "Never place explanations in numeric fields.\n"
            "Never stringify uncertainty: uncertainty must be an object with three string keys.\n"
            "Use repository_path exactly; all output paths must be absolute and anchored to repository_path.\n"
            "You must decide ALL fields yourself from user_query and repository_files (no placeholders).\n"
            f"Reasoning mode: {'deep' if self.enable_reasoning else 'fast'}.\n"
            f"Interpret user dates/times with UTC offset {timezone_offset_minutes:+d} minutes.\n"
            "Follow these domain instructions exactly:\n"
            f"{knowledge_text}\n"
        )

        files_section = (
            "\n".join(f"- {item}" for item in repository_files)
            if repository_files
            else "- (no files found)"
        )
        user_prompt = (
            f"user_query={user_query}\n"
            f"repository_path={repository_path}\n"
            "repository_files:\n"
            f"{files_section}\n"
            "goal=Generate a valid BuildSpec JSON.\n"
            "File-path rule:\n"
            "- For absolute_log_file, absolute_trace_file, absolute_metrics_file, output arrays (0..n) of matching files from repository_files.\n"
            "- Use [] only when that telemetry family has no matching file in repository_files.\n"
            "- If multiple files are relevant in the selected date scope, include all of them (do not force single-file output).\n"
            "- Use a single file only when there is one clear candidate.\n"
            "- Prefer explicit names/types: logs*, *.log*, trace*, span*, metric*, cpu*, latency*.\n"
            "- Output absolute paths by joining repository_path with the selected file.\n"
            "- Do not invent files when repository_files is not empty.\n"
        )
        if previous_errors:
            user_prompt += "previous_validation_errors:\n"
            user_prompt += "\n".join(f"- {item}" for item in previous_errors[:12]) + "\n"
            user_prompt += "Fix all errors and return corrected JSON only.\n"
        return system_prompt, user_prompt

    def _emit(
        self,
        callback: Callable[[ParserEvent], None] | None,
        *,
        phase: str,
        recipient: str,
        content: str,
    ) -> None:
        if callback is None:
            return
        callback(
            ParserEvent(
                sender=self.name,
                recipient=recipient,
                phase=phase,
                content=content,
            )
        )


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


def _normalize_candidate_payload(
    payload: dict[str, object],
    repository_path: Path,
    user_query: str,
    repository_files: list[str] | None = None,
    timezone_offset_minutes: int = 480,
    dataset_profile: str = "generic",
) -> dict[str, object]:
    # Important: this function must NOT decide RCA semantics.
    # Semantic decisions are delegated to the LLM.
    # We only apply lightweight format/path normalization.
    if "absolute_metrics_file" not in payload and "absolute_metric_file" in payload:
        payload = dict(payload)
        payload["absolute_metrics_file"] = payload.get("absolute_metric_file")

    normalized: dict[str, object] = dict(payload)

    for key in ("task_type", "date", "filename_date", "objective"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = value.strip()

    # Normalize time format only from provided values (no inference from query).
    tr = normalized.get("failure_time_range")
    start_hms: str | None = None
    end_hms: str | None = None
    if isinstance(tr, dict):
        start_hms = _extract_hms_from_object(tr.get("start"))
        end_hms = _extract_hms_from_object(tr.get("end"))
        if start_hms and end_hms:
            normalized["failure_time_range"] = {"start": start_hms, "end": end_hms}

    # Normalize ts types only from provided values.
    tr_ts = normalized.get("failure_time_range_ts")
    if isinstance(tr_ts, dict):
        start_i = _to_int(tr_ts.get("start"))
        end_i = _to_int(tr_ts.get("end"))
        if start_i is not None and end_i is not None:
            normalized["failure_time_range_ts"] = {"start": start_i, "end": end_i}

    # Canonical timestamp conversion uses tool logic at UTC+08.
    date_value = str(normalized.get("date", "")).strip()
    if (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value)
        and start_hms is not None
        and end_hms is not None
    ):
        start_ts, end_ts = time_tools.time_range_to_unix_utc8(
            date_value=date_value,
            start_time=start_hms,
            end_time=end_hms,
            timezone_offset_minutes=timezone_offset_minutes,
        )
        normalized["failure_time_range_ts"] = {"start": start_ts, "end": end_ts}

    failures = normalized.get("failures_detected")
    if isinstance(failures, str) and re.fullmatch(r"\d+", failures.strip()):
        normalized["failures_detected"] = int(failures.strip())

    unc = normalized.get("uncertainty")
    if isinstance(unc, dict):
        out: dict[str, str] = {}
        for key in ("root_cause_time", "root_cause_component", "root_cause_reason"):
            if key in unc and unc.get(key) is not None:
                out[key] = str(unc.get(key)).strip().lower()
        normalized["uncertainty"] = out

    task_value = normalized.get("task_type")
    if isinstance(task_value, str):
        task_value = task_value.strip()
        if re.fullmatch(r"task_[1-7]", task_value):
            normalized["task_type"] = task_value
            # Keep BuildSpec objective coherent: task_type defines which fields remain unknown.
            normalized["uncertainty"] = _uncertainty_from_task_type(task_value)

    filename_date_value = ""
    if isinstance(normalized.get("filename_date"), str):
        filename_date_value = str(normalized.get("filename_date")).strip()

    for path_key in (
        "filename_date_directory",
        "absolute_log_file",
        "absolute_trace_file",
        "absolute_metrics_file",
    ):
        raw = normalized.get(path_key)
        if isinstance(raw, str) and raw.strip():
            if path_key == "filename_date_directory":
                normalized[path_key] = str(_normalize_absolute_path(raw, repository_path))
            else:
                normalized[path_key] = [str(_normalize_absolute_path(raw, repository_path))]
            continue
        if isinstance(raw, list):
            normalized_list = [
                str(_normalize_absolute_path(item, repository_path))
                for item in raw
                if isinstance(item, str) and item.strip()
            ]
            if normalized_list:
                normalized[path_key] = normalized_list
            continue
        if path_key != "filename_date_directory":
            fallback = _infer_domain_paths(
                repository_path=repository_path,
                repository_files=repository_files or [],
                domain=path_key,
                filename_date=filename_date_value,
            )
            if fallback:
                normalized[path_key] = fallback
        elif isinstance(raw, str) and raw.strip():
            normalized[path_key] = str(_normalize_absolute_path(raw, repository_path))

    if dataset_profile.strip().lower() == "nezha":
        _apply_nezha_file_selection(
            normalized=normalized,
            repository_path=repository_path,
            repository_files=repository_files or [],
            date_value=date_value,
            start_hms=start_hms,
            end_hms=end_hms,
        )
    elif dataset_profile.strip().lower().startswith("openrca_"):
        _apply_openrca_file_selection(
            normalized=normalized,
            repository_path=repository_path,
            repository_files=repository_files or [],
            date_value=date_value,
        )

    # Keep telemetry file lists focused on the target filename_date when possible.
    for path_key in ("absolute_log_file", "absolute_trace_file", "absolute_metrics_file"):
        values = _as_str_list(normalized.get(path_key))
        if not values:
            continue
        focused = _filter_paths_by_filename_date(values, filename_date_value)
        if focused:
            normalized[path_key] = focused

    return normalized


def _apply_nezha_file_selection(
    *,
    normalized: dict[str, object],
    repository_path: Path,
    repository_files: list[str],
    date_value: str,
    start_hms: str | None,
    end_hms: str | None,
) -> None:
    """Select Nezha shards deterministically without reading fault labels."""
    if not date_value or not start_hms or not end_hms:
        return
    date_marker = f"/rca_data/{date_value}/"
    dated = [item for item in repository_files if date_marker in f"/{item}"]
    if not dated:
        return

    midnight = datetime.strptime("00:00:00", "%H:%M:%S")
    start_seconds = int(
        (datetime.strptime(start_hms, "%H:%M:%S") - midnight).total_seconds()
    )
    end_seconds = int(
        (datetime.strptime(end_hms, "%H:%M:%S") - midnight).total_seconds()
    )

    def in_window_shard(rel: str, domain: str) -> bool:
        if f"/{domain}/" not in f"/{rel}":
            return False
        match = re.search(r"/(\d{2})_(\d{2})_(?:log|trace)\.csv$", f"/{rel}")
        if not match:
            return False
        shard_seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60
        return start_seconds - 60 <= shard_seconds <= end_seconds

    logs = [str((repository_path / rel).resolve()) for rel in dated if in_window_shard(rel, "log")]
    traces = [str((repository_path / rel).resolve()) for rel in dated if in_window_shard(rel, "trace")]
    metrics = [
        str((repository_path / rel).resolve())
        for rel in dated
        if "/metric/" in f"/{rel}" and rel.lower().endswith(".csv")
    ]
    if logs:
        normalized["absolute_log_file"] = sorted(logs)
    if traces:
        normalized["absolute_trace_file"] = sorted(traces)
    if metrics:
        normalized["absolute_metrics_file"] = sorted(metrics)

    sample = (logs or traces or metrics)[0] if (logs or traces or metrics) else ""
    if sample:
        marker = f"{Path('rca_data') / date_value}"
        prefix = sample.split(marker, 1)[0]
        normalized["filename_date_directory"] = str(Path(prefix) / marker)


def _apply_openrca_file_selection(
    *,
    normalized: dict[str, object],
    repository_path: Path,
    repository_files: list[str],
    date_value: str,
) -> None:
    """Select every available OpenRCA signal file for one incident date."""
    if not date_value:
        return
    filename_date = date_value.replace("-", "_")
    dated = [
        rel
        for rel in repository_files
        if filename_date in Path(rel).parts and rel.lower().endswith(".csv")
    ]
    if not dated:
        return

    domains = {
        "absolute_log_file": "/log/",
        "absolute_trace_file": "/trace/",
        "absolute_metrics_file": "/metric/",
    }
    for field, marker in domains.items():
        normalized[field] = sorted(
            str((repository_path / rel).resolve())
            for rel in dated
            if marker in f"/{rel.lower()}"
        )

    dated_directories = {
        (repository_path / rel).resolve().parent.parent
        for rel in dated
        if any(marker in f"/{rel.lower()}" for marker in domains.values())
    }
    if len(dated_directories) == 1:
        normalized["filename_date_directory"] = str(next(iter(dated_directories)))


def _coerce_date(value: object, query: str) -> str | None:
    if isinstance(value, str):
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)
        if match:
            return match.group(1)

    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", query)
    if match:
        return match.group(1)

    month_match = re.search(
        r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b",
        query,
    )
    if month_match:
        month_name, day, year = month_match.groups()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                dt = datetime.strptime(f"{month_name} {day} {year}", fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _coerce_filename_date(
    value: object,
    date_value: str,
    repository_files: list[str],
    repository_path: Path,
) -> str:
    expected = date_value.replace("-", "_")
    if isinstance(value, str):
        candidate = value.strip().replace("-", "_")
        if re.fullmatch(r"\d{4}_\d{2}_\d{2}", candidate):
            if not repository_files:
                return candidate
            if any(candidate in rel for rel in repository_files):
                return candidate

    if repository_files:
        if any(expected in rel for rel in repository_files):
            return expected
        from_repo = _infer_filename_date_from_repository_files(repository_files)
        if from_repo:
            return from_repo
    return expected


def _coerce_time_range(value: object, query: str) -> tuple[str, str]:
    if isinstance(value, dict):
        raw_start = _extract_hms_from_object(value.get("start"))
        raw_end = _extract_hms_from_object(value.get("end"))
        if raw_start and raw_end:
            start_dt = datetime.strptime(raw_start, "%H:%M:%S")
            end_dt = datetime.strptime(raw_end, "%H:%M:%S")
            if end_dt > start_dt:
                return raw_start, raw_end

    matches = re.findall(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", query)
    if len(matches) >= 2:
        start = _norm_hms(matches[0])
        end = _norm_hms(matches[1])
        if datetime.strptime(end, "%H:%M:%S") > datetime.strptime(start, "%H:%M:%S"):
            return start, end
    if len(matches) == 1:
        start = _norm_hms(matches[0])
        end_dt = datetime.strptime(start, "%H:%M:%S") + timedelta(minutes=30)
        return start, end_dt.strftime("%H:%M:%S")
    return "18:30:00", "19:00:00"


def _coerce_time_range_ts(
    value: object,
    date_value: str,
    start_time: str,
    end_time: str,
) -> tuple[int, int]:
    expected_start, expected_end = time_tools.time_range_to_unix_utc8(
        date_value=date_value,
        start_time=start_time,
        end_time=end_time,
    )
    expected_duration = expected_end - expected_start

    if isinstance(value, dict):
        start_raw = _to_int(value.get("start"))
        end_raw = _to_int(value.get("end"))
        if start_raw is not None and end_raw is not None and end_raw > start_raw:
            norm_start, norm_end = _normalize_ts_range(start_raw, end_raw, date_value)
            if (norm_end - norm_start) == expected_duration:
                return norm_start, norm_end
    return expected_start, expected_end


def _normalize_ts_range(start_raw: int, end_raw: int, date_value: str) -> tuple[int, int]:
    start_norm = _normalize_single_ts(start_raw, date_value)
    end_norm = _normalize_single_ts(end_raw, date_value)
    return start_norm, end_norm


def _normalize_single_ts(value: int, date_value: str) -> int:
    return time_tools.normalize_unix_like_to_utc8(value, date_value)


def _coerce_failures_detected(value: object, query: str) -> int:
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        number = re.search(r"\b(\d+)\b", value)
        if number:
            return int(number.group(1))
    if "failure" in query.lower():
        return 1
    return 0


def _coerce_uncertainty(value: object, query: str) -> dict[str, str]:
    result = {
        "root_cause_time": "unknown",
        "root_cause_component": "unknown",
        "root_cause_reason": "unknown",
    }
    if query:
        lowered = query.lower()
        if "component and reason are known" in lowered or "reason and component are known" in lowered:
            result["root_cause_component"] = "known"
            result["root_cause_reason"] = "known"
        if "time is known" in lowered:
            result["root_cause_time"] = "known"
        if "component is known" in lowered:
            result["root_cause_component"] = "known"
        if "reason is known" in lowered:
            result["root_cause_reason"] = "known"

        if "time only" in lowered:
            result["root_cause_time"] = "unknown"
            result["root_cause_component"] = "known"
            result["root_cause_reason"] = "known"
        if "reason only" in lowered:
            result["root_cause_time"] = "known"
            result["root_cause_component"] = "known"
            result["root_cause_reason"] = "unknown"
        if "component only" in lowered:
            result["root_cause_time"] = "known"
            result["root_cause_component"] = "unknown"
            result["root_cause_reason"] = "known"

    if isinstance(value, dict):
        for key in result:
            item = value.get(key)
            if item is not None:
                val = str(item).strip().lower()
                result[key] = "known" if val not in {"unknown", "to_determine"} else "unknown"
        return result
    if isinstance(value, str):
        val = value.strip().lower()
        if val in {"known", "unknown"}:
            return {
                "root_cause_time": val,
                "root_cause_component": val,
                "root_cause_reason": val,
            }
    return result


def _coerce_task_type(value: object, uncertainty: dict[str, str], query: str) -> str:
    requested = _derive_requested_dimensions(query)
    if requested:
        # Query intent is authoritative for task_type mapping.
        derived = _task_type_from_dimensions(requested) or "task_7"
    else:
        requested = {
            dim
            for dim, state in {
                "time": uncertainty.get("root_cause_time"),
                "component": uncertainty.get("root_cause_component"),
                "reason": uncertainty.get("root_cause_reason"),
            }.items()
            if state == "unknown"
        }
        derived = _task_type_from_dimensions(requested) or _task_type_from_uncertainty(uncertainty)

    if isinstance(value, str) and re.fullmatch(r"task_[1-7]", value.strip()):
        provided = value.strip()
        # Keep LLM-provided value only when it matches the requested dimensions.
        if provided == derived:
            return provided
    return derived


def _task_type_from_uncertainty(uncertainty: dict[str, str]) -> str:
    requested = {
        dim for dim, state in {
            "time": uncertainty.get("root_cause_time"),
            "component": uncertainty.get("root_cause_component"),
            "reason": uncertainty.get("root_cause_reason"),
        }.items()
        if state == "unknown"
    }
    return _task_type_from_dimensions(requested) or "task_7"


def _task_type_from_dimensions(dimensions: set[str]) -> str | None:
    if dimensions == {"time"}:
        return "task_1"
    if dimensions == {"reason"}:
        return "task_2"
    if dimensions == {"component"}:
        return "task_3"
    if dimensions == {"time", "reason"}:
        return "task_4"
    if dimensions == {"time", "component"}:
        return "task_5"
    if dimensions == {"component", "reason"}:
        return "task_6"
    if dimensions == {"time", "component", "reason"}:
        return "task_7"
    return None


def _unknown_root_fields_from_task_type(task_type: str) -> set[str]:
    mapping: dict[str, set[str]] = {
        "task_1": {"root_cause_time"},
        "task_2": {"root_cause_reason"},
        "task_3": {"root_cause_component"},
        "task_4": {"root_cause_time", "root_cause_reason"},
        "task_5": {"root_cause_time", "root_cause_component"},
        "task_6": {"root_cause_component", "root_cause_reason"},
        "task_7": {"root_cause_time", "root_cause_component", "root_cause_reason"},
    }
    return mapping.get(task_type, {"root_cause_time", "root_cause_component", "root_cause_reason"})


def _uncertainty_from_task_type(task_type: str) -> dict[str, str]:
    unknown_fields = _unknown_root_fields_from_task_type(task_type)
    result = {
        "root_cause_time": "known",
        "root_cause_component": "known",
        "root_cause_reason": "known",
    }
    for field in unknown_fields:
        result[field] = "unknown"
    return result


def _derive_requested_dimensions(query: str) -> set[str]:
    lowered = (query or "").lower()
    if not lowered.strip():
        return set()

    # Explicit shortcuts.
    if "full rca" in lowered or (
        "time + component + reason" in lowered or "component + reason + time" in lowered
    ):
        return {"time", "component", "reason"}
    if "time only" in lowered or "only time" in lowered or "uniquement le temps" in lowered:
        return {"time"}
    if "reason only" in lowered or "only reason" in lowered or "uniquement la raison" in lowered:
        return {"reason"}
    if "component only" in lowered or "only component" in lowered or "uniquement le composant" in lowered:
        return {"component"}
    if (
        ("time only" in lowered or "only time" in lowered)
        and not ("component" in lowered or "reason" in lowered)
    ):
        return {"time"}
    if (
        ("reason only" in lowered or "only reason" in lowered)
        and not ("component" in lowered or "time" in lowered or "datetime" in lowered)
    ):
        return {"reason"}
    if (
        ("component only" in lowered or "only component" in lowered)
        and not ("reason" in lowered or "time" in lowered or "datetime" in lowered)
    ):
        return {"component"}

    # Remove dimensions explicitly marked as known.
    known: set[str] = set()
    if "component and reason are known" in lowered or "reason and component are known" in lowered:
        known.update({"component", "reason"})
    if re.search(r"\btime\s+(is|are)\s+known\b", lowered):
        known.add("time")
    if re.search(r"\bcomponent\s+(is|are)\s+known\b", lowered):
        known.add("component")
    if re.search(r"\breason\s+(is|are)\s+known\b", lowered):
        known.add("reason")

    # Find what is explicitly requested to be determined.
    requested: set[str] = set()
    intent_window = r"(identify|determine|find|pinpoint|tasked with identifying|identifier|déterminer|determiner|trouver)"
    if re.search(intent_window + r"[\s\S]{0,100}\b(time|datetime|occurrence)\b", lowered):
        requested.add("time")
    if re.search(intent_window + r"[\s\S]{0,100}\bcomponent\b", lowered):
        requested.add("component")
    if re.search(intent_window + r"[\s\S]{0,100}\breason\b", lowered):
        requested.add("reason")

    # Unknown markers can also imply determination targets.
    if re.search(r"\b(time|datetime|occurrence)[\s\S]{0,30}\bunknown\b", lowered):
        requested.add("time")
    if re.search(r"\bcomponent[\s\S]{0,30}\bunknown\b", lowered):
        requested.add("component")
    if re.search(r"\breason[\s\S]{0,30}\bunknown\b", lowered):
        requested.add("reason")

    return {item for item in requested if item not in known}


def _coerce_objective(value: object, task_type: str, query: str) -> str:
    if isinstance(value, str) and len(value.strip()) >= 10:
        return value.strip()
    defaults = {
        "task_1": "Pinpoint the root cause occurrence datetime",
        "task_2": "Identify the root cause reason for the failure",
        "task_3": "Identify the root cause component responsible for the failure",
        "task_4": "Identify the exact root cause datetime and reason for the failure",
        "task_5": "Identify the root cause component and exact root cause datetime",
        "task_6": "Identify the root cause component and reason for the failure",
        "task_7": "Identify the root cause component, the exact root cause datetime and reason for the failure",
    }
    if query:
        return defaults.get(task_type, defaults["task_7"])
    return defaults["task_7"]


def _coerce_date_directory(
    value: object,
    repository_path: Path,
    filename_date: str,
    selected_file_paths: list[str] | None = None,
) -> Path:
    path = _normalize_absolute_path(value, repository_path)
    if selected_file_paths:
        selected = [Path(item) for item in selected_file_paths if isinstance(item, str)]
        if selected:
            dated_candidates: list[Path] = []
            for item in selected:
                ancestor = _find_filename_date_ancestor(item.parent, repository_path, filename_date)
                if ancestor is not None:
                    dated_candidates.append(ancestor)
            if dated_candidates:
                path = _most_frequent_path(dated_candidates)
            else:
                common = Path(os.path.commonpath([str(p.parent) for p in selected]))
                if common.is_absolute() and str(common).startswith(str(repository_path)):
                    path = common
    if not str(path).startswith(str(repository_path)):
        path = repository_path / filename_date
    return path


def _coerce_file_path(
    value: object,
    repository_path: Path,
    available_paths: set[str],
    fallback_rel: str,
    keywords: tuple[str, ...],
    filename_date: str,
) -> str:
    fallback = (repository_path / fallback_rel).resolve()
    path = _normalize_absolute_path(value, repository_path)
    if str(path) in available_paths:
        return str(path)
    if available_paths:
        best = _find_best_matching_path(
            available_paths=available_paths,
            keywords=keywords,
            filename_date=filename_date,
        )
        if best:
            return best
        return str(path)
    if isinstance(value, str) and value.strip():
        return str(path)
    return str(fallback)


def _normalize_absolute_path(value: object, base_path: Path) -> Path:
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw == "/repo":
            return base_path
        if raw.startswith("/repo/"):
            return base_path / raw.removeprefix("/repo/")
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return (base_path / candidate).resolve()
    return base_path


def _repository_abs_paths(repository_path: Path, repository_files: list[str]) -> set[str]:
    results: set[str] = set()
    for rel in repository_files:
        candidate = (repository_path / rel).resolve()
        results.add(str(candidate))
    return results


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        clean = value.strip()
        return [clean] if clean else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _infer_domain_paths(
    *,
    repository_path: Path,
    repository_files: list[str],
    domain: str,
    filename_date: str = "",
) -> list[str]:
    if not repository_files:
        return []
    if domain == "absolute_log_file":
        keywords = ("log", "logs")
    elif domain == "absolute_trace_file":
        keywords = ("trace", "span")
    elif domain == "absolute_metrics_file":
        keywords = ("metric", "metrics", "cpu", "latency")
    else:
        return []

    results: list[str] = []
    dated_results: list[str] = []
    for rel in repository_files:
        lowered = rel.lower()
        if any(token in lowered for token in keywords):
            absolute = str((repository_path / rel).resolve())
            results.append(absolute)
            if filename_date and filename_date in rel:
                dated_results.append(absolute)
    return dated_results or results


def _filter_paths_by_filename_date(paths: list[str], filename_date: str) -> list[str]:
    if not filename_date:
        return paths
    focused = [item for item in paths if filename_date in item]
    return focused or paths


def _find_best_matching_path(
    available_paths: set[str],
    keywords: tuple[str, ...],
    filename_date: str,
) -> str | None:
    scored: list[tuple[int, str]] = []
    for item in available_paths:
        lowered = item.lower()
        score = 0
        for kw in keywords:
            if kw in lowered:
                score += 1
        if filename_date and filename_date in lowered:
            score += 2
        if lowered.endswith(".csv"):
            score += 1
        if score > 0:
            scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return scored[0][1]


def _extract_hms_from_object(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", value)
    if not match:
        return None
    return _norm_hms(match.group(1))


def _norm_hms(value: str) -> str:
    if len(value.split(":")) == 2:
        value = value + ":00"
    return datetime.strptime(value, "%H:%M:%S").strftime("%H:%M:%S")


def _to_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.isdigit():
            return int(candidate)
    return None


def _collect_repository_files(repository_path: Path, max_files: int = 100000) -> list[str]:
    files: list[str] = []
    try:
        for path in repository_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(repository_path).as_posix()
            files.append(rel)
            if len(files) >= max_files:
                break
    except Exception:
        return []
    files.sort()
    return files


def _validate_selected_paths_against_scan(
    selected_paths: dict[str, list[str]],
    repository_path: Path,
    repository_files: list[str],
) -> list[str]:
    if not repository_files:
        return []

    available = _repository_abs_paths(repository_path, repository_files)
    errors: list[str] = []
    categories: dict[str, tuple[str, ...]] = {
        "absolute_log_file": ("log", ".log", "logs"),
        "absolute_trace_file": ("trace", "span"),
        "absolute_metrics_file": ("metric", "metrics", "cpu", "latency"),
    }

    for field, keywords in categories.items():
        values = _as_str_list(selected_paths.get(field, []))
        candidate_exists = any(
            any(token in item.lower() for token in keywords)
            for item in repository_files
        )
        if not values:
            if candidate_exists:
                errors.append(f"{field} is missing while matching repository files exist.")
            continue
        for idx, path_value in enumerate(values):
            if path_value not in available:
                errors.append(f"{field}[{idx}] must be one of scanned repository files. got={path_value}")
                continue
            lowered = path_value.lower()
            if not any(token in lowered for token in keywords):
                if candidate_exists:
                    errors.append(f"{field}[{idx}] does not match expected {field} semantics.")

    if (
        selected_paths.get("absolute_log_file")
        and selected_paths.get("absolute_trace_file")
        and selected_paths.get("absolute_metrics_file")
    ):
        log_set = set(_as_str_list(selected_paths.get("absolute_log_file", [])))
        trace_set = set(_as_str_list(selected_paths.get("absolute_trace_file", [])))
        metric_set = set(_as_str_list(selected_paths.get("absolute_metrics_file", [])))
        if log_set & trace_set:
            errors.append("absolute_log_file and absolute_trace_file must not overlap.")
        if log_set & metric_set:
            errors.append("absolute_log_file and absolute_metrics_file must not overlap.")
        if trace_set & metric_set:
            errors.append("absolute_trace_file and absolute_metrics_file must not overlap.")

    return errors


def _validate_task_type_against_query(
    payload: dict[str, object],
    user_query: str,
) -> list[str]:
    requested = _derive_requested_dimensions(user_query)
    if not requested:
        return []
    expected = _task_type_from_dimensions(requested)
    if expected is None:
        return []
    got = str(payload.get("task_type", "")).strip()
    if got != expected:
        return [
            (
                "task_type mismatch with user request: "
                f"expected `{expected}` from requested dimensions {sorted(requested)}, got `{got or 'missing'}`."
            )
        ]
    return []


def _validate_uncertainty_matches_task_type(payload: dict[str, object]) -> list[str]:
    task_type = str(payload.get("task_type", "")).strip()
    if not re.fullmatch(r"task_[1-7]", task_type):
        return []
    uncertainty = payload.get("uncertainty")
    if not isinstance(uncertainty, dict):
        return ["uncertainty must be an object matching task_type requirements."]
    expected_unknown = _unknown_root_fields_from_task_type(task_type)
    got_unknown = {
        key
        for key in ("root_cause_time", "root_cause_component", "root_cause_reason")
        if str(uncertainty.get(key, "")).strip().lower() == "unknown"
    }
    if got_unknown != expected_unknown:
        return [
            (
                "uncertainty mismatch with task_type: "
                f"task_type `{task_type}` requires unknown fields {sorted(expected_unknown)}, "
                f"got {sorted(got_unknown)}."
            )
        ]
    return []


def _infer_filename_date_from_repository_files(repository_files: list[str]) -> str | None:
    counts: dict[str, int] = {}
    for rel in repository_files:
        for segment in Path(rel).parts:
            token = segment.replace("-", "_")
            if re.fullmatch(r"\d{4}_\d{2}_\d{2}", token):
                counts[token] = counts.get(token, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[0][0]


def _infer_filename_date_from_selected_paths(
    selected_file_paths: list[str],
    repository_path: Path,
) -> str | None:
    for raw in selected_file_paths:
        if not raw:
            continue
        path = Path(raw)
        current = path.parent
        while str(current).startswith(str(repository_path)):
            token = current.name.replace("-", "_")
            if re.fullmatch(r"\d{4}_\d{2}_\d{2}", token):
                return token
            if current == repository_path:
                break
            current = current.parent
    return None


def _find_filename_date_ancestor(
    start: Path,
    repository_path: Path,
    filename_date: str,
) -> Path | None:
    current = start
    while str(current).startswith(str(repository_path)):
        if filename_date in current.name:
            return current
        if current == repository_path:
            break
        current = current.parent
    return None


def _most_frequent_path(paths: list[Path]) -> Path:
    counts: dict[str, int] = {}
    for item in paths:
        key = str(item)
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0])))
    return Path(ranked[0][0])


def _load_parser_knowledge(knowledge_file: str) -> tuple[str, str]:
    """Load parser knowledge text from external file with safe fallback."""
    raw = (knowledge_file or "").strip() or "knowledge/parser_buildspec_kb.md"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if candidate.exists() and candidate.is_file():
        try:
            content = candidate.read_text(encoding="utf-8").strip()
            if content:
                return str(candidate), content
        except Exception:
            pass
    return str(candidate), _PARSER_KB_FALLBACK


def _buildspec_payload_without_llm(
    *,
    user_query: str,
    repository_path: Path,
    repository_files: list[str],
    timezone_offset_minutes: int = 480,
    dataset_profile: str = "generic",
) -> dict[str, object]:
    """Build a valid payload from deterministic parsing when reasoning is disabled."""
    date_value = _coerce_date(None, user_query) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename_date = _coerce_filename_date(None, date_value, repository_files, repository_path)
    start_time, end_time = _coerce_time_range(None, user_query)
    ts_start, ts_end = _coerce_time_range_ts(None, date_value, start_time, end_time)
    failures_detected = _coerce_failures_detected(None, user_query)

    uncertainty = _coerce_uncertainty(None, user_query)
    task_type = _coerce_task_type(None, uncertainty, user_query)
    uncertainty = _uncertainty_from_task_type(task_type)
    objective = _coerce_objective(None, task_type, user_query)

    log_paths = _infer_domain_paths(
        repository_path=repository_path,
        repository_files=repository_files,
        domain="absolute_log_file",
    )
    trace_paths = _infer_domain_paths(
        repository_path=repository_path,
        repository_files=repository_files,
        domain="absolute_trace_file",
    )
    metrics_paths = _infer_domain_paths(
        repository_path=repository_path,
        repository_files=repository_files,
        domain="absolute_metrics_file",
    )

    if not log_paths:
        log_paths = [str((repository_path / filename_date / "log" / "log_service.csv").resolve())]
    if not trace_paths:
        trace_paths = [str((repository_path / filename_date / "trace" / "trace_span.csv").resolve())]
    if not metrics_paths:
        metrics_paths = [str((repository_path / filename_date / "metric" / "metric_container.csv").resolve())]

    selected_for_directory = [*log_paths, *trace_paths, *metrics_paths]
    date_directory = _coerce_date_directory(
        value=str((repository_path / filename_date).resolve()),
        repository_path=repository_path,
        filename_date=filename_date,
        selected_file_paths=selected_for_directory,
    )

    payload: dict[str, object] = {
        "task_type": task_type,
        "date": date_value,
        "filename_date": filename_date,
        "failure_time_range": {"start": start_time, "end": end_time},
        "failure_time_range_ts": {"start": ts_start, "end": ts_end},
        "failures_detected": failures_detected,
        "uncertainty": uncertainty,
        "objective": objective,
        "filename_date_directory": str(date_directory),
        "absolute_log_file": log_paths,
        "absolute_trace_file": trace_paths,
        "absolute_metrics_file": metrics_paths,
    }
    return _normalize_candidate_payload(
        payload=payload,
        repository_path=repository_path,
        user_query=user_query,
        repository_files=repository_files,
        timezone_offset_minutes=timezone_offset_minutes,
        dataset_profile=dataset_profile,
    )


_PARSER_KB_FALLBACK = """
BuildSpec keys required:
task_type, date, filename_date, failure_time_range, failure_time_range_ts, failures_detected,
uncertainty, objective, filename_date_directory, absolute_log_file, absolute_trace_file, absolute_metrics_file.

task_type mapping:
task_1=time only; task_2=reason only; task_3=component only; task_4=time+reason;
task_5=time+component; task_6=component+reason; task_7=time+component+reason.

Rules:
date=YYYY-MM-DD; filename_date=YYYY_MM_DD; time range HH:MM:SS with end>start;
timestamps are unix seconds with same duration as time range;
uncertainty values must be known|unknown;
absolute_*_file fields are arrays (1..n absolute paths) chosen from repository_files when available.

Output JSON only, strict schema, no extra text.
""".strip()
