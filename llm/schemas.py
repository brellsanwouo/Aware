"""Shared response format schemas for LLM providers."""

from __future__ import annotations

from typing import Any


BUILDSPEC_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "buildspec",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "task_type",
                "date",
                "filename_date",
                "failure_time_range",
                "failure_time_range_ts",
                "failures_detected",
                "uncertainty",
                "objective",
                "filename_date_directory",
                "absolute_log_file",
                "absolute_trace_file",
                "absolute_metrics_file",
            ],
            "properties": {
                "task_type": {
                    "type": "string",
                    "pattern": "^task_[1-7]$",
                },
                "date": {
                    "type": "string",
                    "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                },
                "filename_date": {
                    "type": "string",
                    "pattern": "^\\d{4}_\\d{2}_\\d{2}$",
                },
                "failure_time_range": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["start", "end"],
                    "properties": {
                        "start": {"type": "string", "pattern": "^\\d{2}:\\d{2}:\\d{2}$"},
                        "end": {"type": "string", "pattern": "^\\d{2}:\\d{2}:\\d{2}$"},
                    },
                },
                "failure_time_range_ts": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["start", "end"],
                    "properties": {
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                    },
                },
                "failures_detected": {"type": "integer"},
                "uncertainty": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "root_cause_time",
                        "root_cause_component",
                        "root_cause_reason",
                    ],
                    "properties": {
                        "root_cause_time": {"type": "string"},
                        "root_cause_component": {"type": "string"},
                        "root_cause_reason": {"type": "string"},
                    },
                },
                "objective": {"type": "string", "minLength": 10},
                "filename_date_directory": {
                    "type": "string",
                    "pattern": "^/.*",
                },
                "absolute_log_file": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^/.*"},
                },
                "absolute_trace_file": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^/.*"},
                },
                "absolute_metrics_file": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^/.*"},
                },
            },
        },
    },
}
