# Usage

Aware exposes a CLI through the `aware` command.

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

## Run a Problems CSV Batch

Run every incident sequentially from an AWARE problems CSV:

```bash
aware batch \
  --problems-csv /path/to/problems.csv \
  --rca-version v3 \
  --max-agents 8 \
  --results-csv output/bank-v3-results.csv \
  --graph-dir output/bank-v3-graphs
```

Use `--limit 5` for a smoke test. Each CSV row may provide its own
`source_path`; otherwise pass `--repo` as a fallback. Expected component, time,
and reason columns are used only after execution for scoring and are never sent
to ParserAgent or ExecutorAgent.

The command writes:

- one results CSV with component, reason, time, and overall scores;
- one JSON and text artifact per incident;
- a durable batch manifest and per-incident backend journals;
- one causal-graph PNG per V3 incident when the graph is non-empty.

`aware assess` and `aware execute` also accept `--rca-version v2|v3`.

## Run the Full Assessment

Use `assess` for the full Parser + Executor workflow.

```bash
aware assess \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/repository
```

## Useful Options

| Option | Scope | Description |
| --- | --- | --- |
| `--repo` | CLI | Path to the telemetry repository or batch fallback. |
| `--query` | Parser / Assess | Natural-language incident request. |
| `--buildspec-json` | Execute | Path to an existing BuildSpec JSON file. |
| `--problems-csv` | Batch | AWARE problems catalogue to execute. |
| `--rca-version` | Execute / Assess / Batch / UI | Select V2 or V3. |
| `--results-csv` | Batch | Results and scoring CSV destination. |
| `--graph-dir` | Batch | V3 causal graph PNG directory. |
| `--db-url` | Execute / Assess / Batch | SQLite database URL override. |
| `--max-agents` | Execute / Assess / Batch | Maximum number of executor sub-agents per incident. |
| `--llm-model` | Parser / Assess | OpenAI model override. |

## BuildSpec Multiple Files

The BuildSpec supports multiple files per telemetry domain:

- `absolute_log_file`: list of absolute log paths
- `absolute_trace_file`: list of absolute trace paths
- `absolute_metrics_file`: list of absolute metrics paths
