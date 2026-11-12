# Outputs

Each successful or failed run can produce local artifacts and optional SQLite records.

## Artifact Files

Standalone runs are saved automatically under:

```text
output/json/<timestamp>_<run_id>.json
output/txt/<timestamp>_<run_id>.txt
```

The JSON artifact contains structured parser, executor, and assessment data. The text artifact is a human-readable report.

## Batch Results

`aware batch` prints its UTC timestamped batch ID and output directory. Browser
batches also use a timestamped ID. By default, both interfaces group batch files
under `output/batches/<batch-id>/`:

- `results.csv`: one row per incident, with the predicted cause, score, agent count, and artifact paths.
- `config.json`: provider, model, reasoning and memory flags, agent limit, and input locations; no API keys.
- `problems.csv`: snapshot of the incidents selected for this batch.
- `manifest.json`: incident IDs, run IDs, statuses, and journal paths.
- `incidents/<problem-id>.json` and `.log`: complete event journals and results.
- `json/` and `txt/`: per-incident structured and readable reports.
- `graphs/<problem-id>-causal-graph.png`: graph image when a graph was produced.
- `memory/<run-id>.db`: one SQLite database per incident when memory is enabled.

CLI IDs use `batch-cli-YYYYMMDDTHHMMSSZ-xxxxxx`; UI IDs use the same UTC
timestamp format with the `batch-ui-` prefix. `T` separates date and time,
and `Z` indicates UTC. Existing batch directories keep their original names.

The `results.csv` file includes the path to each incident's memory database. The
CLI PNG and UI graph identify the selected component, cause, occurrence time
in the incident's configured timezone, and confidence. Observations without a displayed graph relation are
grouped in a separate **Other findings** area beneath the causal chain.
Explicit output path overrides may place selected files outside this directory.

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

Default URL for a standalone run:

```text
sqlite:///output/memory/run-YYYYMMDDTHHMMSSZ-xxxxxxxx.db
```

For either CLI or browser batches, the default is
`sqlite:///output/batches/<batch-id>/memory/<run-id>.db`.

Tables:

- `runs`
- `events`
- `task_results`
- `findings`

This database is useful for later inspection, debugging, and comparing assessment runs.
