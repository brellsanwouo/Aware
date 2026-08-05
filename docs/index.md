# Aware

Aware is a minimal **Assess** prototype for telemetry-based root-cause analysis. It combines a Parser agent, an Executor agent, and dynamic analysis sub-agents to turn an incident question into a validated BuildSpec and an execution report.

This Lite edition is intentionally CLI-only.

The project focuses on a clear runtime flow:

- parse the user request into a BuildSpec
- select the relevant telemetry files
- run specialized log, trace, and metrics agents
- filter analysis on `failure_time_range_ts`
- persist run artifacts as JSON, text, and optional SQLite records

## Technology Stack

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white)
![Google ADK](https://img.shields.io/badge/Google%20ADK-enabled-4285F4?style=flat&logo=google&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-enabled-412991?style=flat&logo=openai&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-2.x-E92063?style=flat&logo=pydantic&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-supported-003B57?style=flat&logo=sqlite&logoColor=white)

## Interface

Use the CLI to run parser-only, executor-only, or full assessment workflows from a terminal.

## Core Components

| Component | Role |
| --- | --- |
| `ParserAgent` | Converts a user incident request into a validated BuildSpec. |
| `ExecutorAgent` | Instantiates analysis sub-agents and coordinates execution. |
| `tools/` | Provides file loading, time-window filtering, and telemetry helpers. |
| `templates/` | Defines dynamic agent templates for logs, traces, and metrics. |
| Knowledge DB | Stores runs, events, task results, and findings in SQLite when memory is enabled. |

## Next Steps

- [Install and configure Aware](getting-started.md)
- [Run the CLI commands](usage.md)
- [Review the runtime architecture](architecture.md)
