# Usage

Aware exposes a CLI through the `aware` command. Use `aware batch` for a Nezha
or OpenRCA dataset: it generates incident requests from the catalogue, so you
provide a dataset path and options instead of writing a `--query`. Use
`aware assess` when you have a single, free-form incident question.

## Run a Dataset Batch

One command prepares the incident catalogue and analyzes the first five incidents for any supported dataset:

```bash
aware batch --dataset nezha --source /path/to/extracted/Nezha --limit 5
aware batch --dataset openrca_bank --source /path/to/extracted/Bank-003 --limit 5
aware batch --dataset openrca_market --source /path/to/extracted/Market-002 --limit 5
aware batch --dataset openrca_telecom --source /path/to/extracted/Telecom-001 --limit 5
```

The values for `--dataset` match the UI's dataset profiles. Market and Telecom
must be extracted before use; an archive path alone cannot supply telemetry to
the Executor. The raw datasets are ignored by Git.

`--source` points to an extracted dataset directory or a telemetry folder inside it.
The CLI locates the catalogue above that folder, as the UI does. `--limit` counts incidents;
the `EXECUTOR_MAX_AGENTS` value from `.env` limits agents created for each
incident. AWARE saves the exact selected catalogue as `problems.csv` in the
batch output directory. OpenRCA uses the same `--dataset`, `--source`, and
`--limit` options as Nezha; no manual `--query` or CSV preparation is needed.

For an explicit run configuration, the same command can include the parser
retry limit, agent ceiling, and model override:

```bash
aware batch \
  --dataset openrca_bank \
  --source /path/to/extracted/Bank-003 \
  --limit 5 \
  --max-agents 5 \
  --max-attempts 5 \
  --llm-model MODEL_AVAILABLE_ON_YOUR_PROVIDER
```

The provider, its API key, reasoning, and memory are configured in `.env` or
the process environment.
`--llm-model` overrides the model for the selected provider. `--max-agents`
is a total ceiling **per incident**, not a batch-wide count or concurrency
setting. Without an override, `EXECUTOR_MAX_AGENTS` in `.env` applies.

## Run an Existing Problems CSV Batch

Run an AWARE problems CSV using the same timestamped batch directory:

```bash
aware batch \
  --problems-csv /path/to/problems.csv \
  --limit 5 \
  --max-agents 8
```

Omit `--limit 5` to run every CSV incident. Each row may provide its own
`source_path`; otherwise pass `--repo` as a fallback. Expected component, time,
and reason columns are used only after execution for scoring and are never sent
to ParserAgent or ExecutorAgent.

The command writes:

- `output/batches/<batch-id>/results.csv` with component, reason, time, and overall scores;
- `output/batches/<batch-id>/config.json` with the provider, model, reasoning, memory, and agent limit used;
- `output/batches/<batch-id>/problems.csv` with the exact selected incidents, including CSV input batches;
- `output/batches/<batch-id>/json/` and `txt/` with one report per incident;
- `output/batches/<batch-id>/manifest.json` and per-incident journals in `incidents/`;
- causal-graph PNGs in `graphs/` when the graphs are non-empty;
- one SQLite file per incident in `memory/` when memory is enabled.

At startup the CLI prints the provider, model, reasoning, memory, incident count,
and agent limit. At completion it prints the batch ID and output directory.
The default ID is `batch-cli-YYYYMMDDTHHMMSSZ-xxxxxx`, using UTC just like the
UI's `batch-ui-` IDs. A custom `--batch-id` replaces the generated ID.
Explicit `--db-url`, `--results-csv`, and `--graph-dir` paths still take precedence
and may place those files outside the batch directory.

## Run the Full Assessment

Use `assess` for the full Parser + Executor workflow.

```bash
aware assess \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/repository
```

## Parse a Request

Use `parse` when you only want to generate and validate a BuildSpec.

```bash
aware parse \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/repository
```

## Execute an Existing BuildSpec

Use `execute` when a BuildSpec already exists and you want to run the executor stage.

```bash
aware execute \
  --buildspec-json /path/to/buildspec.json \
  --repo /path/to/repository
```

## Useful Options

| Option | Scope | Description |
| --- | --- | --- |
| `--source` (`--repo`) | Batch | Extracted dataset directory, an internal telemetry directory, or fallback telemetry directory for a CSV. |
| `--repo` | Parse / Execute / Assess | Telemetry repository for one incident. |
| `--query` | Parser / Assess | Natural-language incident request. |
| `--buildspec-json` | Execute | Path to an existing BuildSpec JSON file. |
| `--problems-csv` | Batch | AWARE problems catalogue to execute. |
| `--dataset` | Batch | Build a catalogue directly from `nezha`, `openrca_bank`, `openrca_market`, or `openrca_telecom`. |
| `--limit` | Batch | Number of incidents to analyze. |
| `--max-attempts` | Parse / Assess / Batch | Maximum Parser retry attempts; overrides `PARSER_MAX_ATTEMPTS`. |
| `--batch-id` | Batch | Custom batch directory ID; otherwise a UTC timestamped ID is generated. |
| `--results-csv` | Batch | Results and scoring CSV destination. |
| `--graph-dir` | Batch | V3 causal graph PNG directory. |
| `--db-url` | Execute / Assess / Batch | SQLite database URL override. |
| `--max-agents` | Execute / Assess / Batch | Maximum number of executor sub-agents per incident. |
| `--llm-model` | Parser / Assess / Batch | LLM model override. |
| `--json` | CLI | JSON output; for `batch`, prints the final summary as JSON. |

## BuildSpec Multiple Files

The BuildSpec supports multiple files per telemetry domain:

- `absolute_log_file`: list of absolute log paths
- `absolute_trace_file`: list of absolute trace paths
- `absolute_metrics_file`: list of absolute metrics paths
