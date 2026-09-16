# Outputs

Each successful or failed run can produce local artifacts and optional SQLite records.

## Artifact Files

Runs are saved automatically under:

```text
output/json/<timestamp>_<run_id>.json
output/txt/<timestamp>_<run_id>.txt
```

The JSON artifact contains structured parser, executor, and assessment data. The text artifact is a human-readable report.

## Assessment Sections

The final assessment output can include:

| Section | Description |
| --- | --- |
| `buildspec_resolution_scope` | How the BuildSpec scope was resolved. |
| `root_cause_synthesis` | Cross-domain synthesis from logs, traces, and metrics. |
| `findings` | Structured findings reported by executor sub-agents. |
| `anomalies` | Anomalies detected during telemetry analysis. |
| `preliminary_causes` | Candidate causes inferred from the executor output. |
| `final_reporting` | Final report fields requested by the BuildSpec. |

## SQLite Knowledge DB

When memory is enabled, Aware writes runtime information to SQLite.

Default URL:

```text
sqlite:///output/assess_v3.db
```

Tables:

- `runs`
- `events`
- `task_results`
- `findings`

This database is useful for later inspection, debugging, and comparing assessment runs.
