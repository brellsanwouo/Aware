# Architecture

Aware is organized around a V3 agent runtime with explicit boundaries between dataset preparation, parsing, execution, tools, templates, and persistence. The UI and CLI share the same dataset catalogue builders and source-directory resolver.

## Runtime Flow

```text
Dataset + extracted source -> Nezha/OpenRCA catalogue -> incident request
Single incident question -----------------------------> incident request
Incident request -> ParserAgent
  -> BuildSpec
  -> ExecutorAgent
  -> Metrics, logs, traces, and focused V3 sub-agents
  -> Findings, root-cause synthesis, and causal graph
  -> Scores, reports, journals, and optional SQLite memory
```

## Parser Agent

`ParserAgent` converts an incident request into a BuildSpec. Canonical Nezha and OpenRCA batch incidents take a deterministic, validated parser path; free-form questions use the retrying LLM parser. It loads BuildSpec rules from:

```text
knowledge/parser_buildspec_kb.md
```

The knowledge file contains:

- `task_1..task_7` mapping
- BuildSpec contract
- normalization rules
- file selection rules for logs, traces, and metrics

## Executor Agent

`ExecutorAgent` instantiates sub-agents from templates and coordinates their analysis against selected telemetry files. The metrics, logs, and traces domains run as needed; V3 specialists and focused follow-ups can investigate unresolved evidence. `EXECUTOR_MAX_AGENTS` is a ceiling on all agents created **per incident**, including those follow-ups. It is not a concurrency setting.

Implementation:

```text
agents/executor_agent.py
```

Executor knowledge is loaded from:

```text
knowledge/executor_rca_v3_kb.md
```

## Dynamic Agent Templates

Executor sub-agents are defined in:

```text
templates/assess_templates.py
```

Each template defines:

- `agent_name`
- `role`
- `objective`
- BuildSpec `target_field`
- tools to use
- telemetry domain

## Tools

Telemetry helpers live in:

```text
tools/telemetry_tools.py
```

Examples:

- `load_csv_window`
- `build_llm_observation_context`
- `count_matches`
- `sample_matching_lines`
- `max_numeric_column`

These helpers keep data access and time-window filtering outside of agent orchestration.

## Batch persistence

By default, each CLI or UI batch gets a UTC timestamped directory under
`output/batches/<batch-id>/`. It contains `problems.csv`, `config.json`,
`results.csv`, per-incident journals and reports, and graph PNGs when a causal
graph is produced. With memory enabled, each incident gets its own SQLite file
under `memory/`. See [Outputs](outputs.md) for the full layout.
