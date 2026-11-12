# Aware

Aware is a V3 **Assess** prototype for telemetry-based root-cause analysis. It combines a Parser agent, an Executor agent, and dynamic analysis sub-agents to identify an incident's root-cause component, occurrence time, and reason.

Both the `aware` CLI and the web UI can analyze one incident or a dataset batch. Nezha and OpenRCA Bank, Market, and Telecom batches use the same dataset catalogue preparation. The CLI batch command needs a dataset name and an extracted source directory; AWARE generates each incident request automatically.

The project focuses on a clear runtime flow:

- prepare dataset incidents or parse a free-form request into a BuildSpec
- select the relevant telemetry files
- run specialized log, trace, and metrics agents under a per-incident agent limit
- filter analysis on `failure_time_range_ts`
- report the selected root cause and a causal evidence graph
- persist batch scores, journals, JSON/text reports, graphs, and optional SQLite memory

## Technology Stack

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white)
![Google ADK](https://img.shields.io/badge/Google%20ADK-enabled-4285F4?style=flat&logo=google&logoColor=white)
![OpenAI-compatible](https://img.shields.io/badge/OpenAI--compatible-enabled-412991?style=flat)
![Ollama Cloud](https://img.shields.io/badge/Ollama%20Cloud-enabled-111827?style=flat)
![FastAPI](https://img.shields.io/badge/FastAPI-supported-009688?style=flat&logo=fastapi&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-2.x-E92063?style=flat&logo=pydantic&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-supported-003B57?style=flat&logo=sqlite&logoColor=white)

## Interfaces

| Interface | Use it for |
| --- | --- |
| CLI | Running dataset batches or parser-only, executor-only, and single-incident assessments. |
| Web UI | Running dataset batches or single incidents while inspecting live events and results. |

## Core Components

| Component | Role |
| --- | --- |
| `ParserAgent` | Converts a user incident request into a validated BuildSpec. |
| `ExecutorAgent` | Instantiates analysis sub-agents and coordinates execution. |
| `tools/` | Provides file loading, time-window filtering, and telemetry helpers. |
| `templates/` | Defines dynamic agent templates for logs, traces, and metrics. |
| Knowledge DB | Stores runs, events, task results, and findings in a per-run SQLite file when memory is enabled. |

## Next Steps

- [Install and configure Aware](getting-started.md)
- [Run the CLI commands](usage.md)
- [Use the web UI](ui.md)
- [Review the runtime architecture](architecture.md)
