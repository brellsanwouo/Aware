# Architecture

Aware is organized around a small agent runtime with explicit boundaries between parsing, execution, tools, templates, and persistence.

## Runtime Flow

```text
User Query
  -> ParserAgent
  -> BuildSpec
  -> ExecutorAgent
  -> Dynamic sub-agents
  -> Findings / anomalies / preliminary causes
  -> Artifacts and optional SQLite memory
```

## Parser Agent

`ParserAgent` converts a user incident request into a BuildSpec. It loads BuildSpec rules from:

```text
knowledge/parser_buildspec_kb.md
```

The knowledge file contains:

- `task_1..task_7` mapping
- BuildSpec contract
- normalization rules
- file selection rules for logs, traces, and metrics

## Executor Agent

`ExecutorAgent` instantiates sub-agents from templates and coordinates their analysis against selected telemetry files.

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
