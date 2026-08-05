"""BuildSpec contract and validation for ParserAgent output."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


class FailureTimeRange(BaseModel):
    """Human-readable failure time range."""

    start: str
    end: str

    @model_validator(mode="after")
    def check_order(self) -> "FailureTimeRange":
        start = _parse_hms(self.start)
        end = _parse_hms(self.end)
        if end <= start:
            raise ValueError("failure_time_range.end must be strictly after start.")
        return self

    @property
    def duration_seconds(self) -> int:
        return int((_parse_hms(self.end) - _parse_hms(self.start)).total_seconds())


class FailureTimeRangeTs(BaseModel):
    """Unix timestamp failure time range."""

    start: int
    end: int

    @model_validator(mode="after")
    def check_order(self) -> "FailureTimeRangeTs":
        if self.end <= self.start:
            raise ValueError("failure_time_range_ts.end must be strictly after start.")
        return self

    @property
    def duration_seconds(self) -> int:
        return int(self.end - self.start)


class Uncertainty(BaseModel):
    """Known unknowns about root cause."""

    root_cause_time: str
    root_cause_component: str
    root_cause_reason: str

    @model_validator(mode="after")
    def check_values(self) -> "Uncertainty":
        allowed = {"known", "unknown"}
        for label, value in (
            ("root_cause_time", self.root_cause_time),
            ("root_cause_component", self.root_cause_component),
            ("root_cause_reason", self.root_cause_reason),
        ):
            if value not in allowed:
                raise ValueError(f"{label} must be one of {sorted(allowed)}.")
        return self


class BuildSpec(BaseModel):
    """Expected parser output contract."""

    task_type: Literal[
        "task_1",
        "task_2",
        "task_3",
        "task_4",
        "task_5",
        "task_6",
        "task_7",
    ]
    date: str
    filename_date: str
    failure_time_range: FailureTimeRange
    failure_time_range_ts: FailureTimeRangeTs
    failures_detected: int = Field(ge=0)
    uncertainty: Uncertainty
    objective: str = Field(min_length=10)
    filename_date_directory: str
    # Some observability datasets legitimately omit one signal family (OpenRCA
    # Telecom has traces and metrics, but no logs). The keys remain mandatory;
    # an empty list explicitly records that the domain is unavailable.
    absolute_log_file: list[str]
    absolute_trace_file: list[str]
    absolute_metrics_file: list[str]

    @model_validator(mode="after")
    def validate_contract(self) -> "BuildSpec":
        datetime.strptime(self.date, "%Y-%m-%d")

        if self.failure_time_range.duration_seconds != self.failure_time_range_ts.duration_seconds:
            raise ValueError(
                "failure_time_range and failure_time_range_ts must represent the same duration."
            )

        _require_absolute_path(self.filename_date_directory, "filename_date_directory")
        for idx, path in enumerate(self.absolute_log_file):
            _require_absolute_path(path, f"absolute_log_file[{idx}]")
        for idx, path in enumerate(self.absolute_trace_file):
            _require_absolute_path(path, f"absolute_trace_file[{idx}]")
        for idx, path in enumerate(self.absolute_metrics_file):
            _require_absolute_path(path, f"absolute_metrics_file[{idx}]")
        return self


class BuildSpecValidationResult(BaseModel):
    """Structured validation output."""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    normalized: BuildSpec | None = None


def validate_buildspec(
    payload: dict[str, object],
    expected_repository_path: str | None = None,
) -> BuildSpecValidationResult:
    """Validate parser output against contract and optional repository root."""
    try:
        normalized = BuildSpec.model_validate(payload)
    except ValidationError as exc:
        return BuildSpecValidationResult(
            is_valid=False,
            errors=_format_pydantic_errors(exc),
            normalized=None,
        )

    if expected_repository_path:
        repo_path = _require_absolute_path(expected_repository_path, "expected_repository_path")
        for label, raw_paths in (
            ("filename_date_directory", normalized.filename_date_directory),
            ("absolute_log_file", normalized.absolute_log_file),
            ("absolute_trace_file", normalized.absolute_trace_file),
            ("absolute_metrics_file", normalized.absolute_metrics_file),
        ):
            values = [raw_paths] if isinstance(raw_paths, str) else list(raw_paths)
            for idx, raw_path in enumerate(values):
                path = Path(raw_path)
                if not str(path).startswith(str(repo_path)):
                    return BuildSpecValidationResult(
                        is_valid=False,
                        errors=[f"{label}[{idx}] must be inside expected_repository_path."],
                        normalized=None,
                    )

    return BuildSpecValidationResult(is_valid=True, errors=[], normalized=normalized)


def _format_pydantic_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", [])) or "buildspec"
        message = item.get("msg", "validation error")
        errors.append(f"{location}: {message}")
    return errors


def _parse_hms(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Invalid time `{value}`. Expected HH:MM:SS.") from exc


def _require_absolute_path(path_value: str, field: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path.")
    return path
