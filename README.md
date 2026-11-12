# AWARE V3 Assess ADK MVP

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white)
![Google ADK](https://img.shields.io/badge/Google%20ADK-enabled-4285F4?style=flat&logo=google&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-enabled-412991?style=flat&logo=openai&logoColor=white)
![Ollama Cloud](https://img.shields.io/badge/Ollama%20Cloud-enabled-111827?style=flat)
![FastAPI](https://img.shields.io/badge/FastAPI-supported-009688?style=flat&logo=fastapi&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-2.x-E92063?style=flat&logo=pydantic&logoColor=white)
![Typer](https://img.shields.io/badge/Typer-CLI-111827?style=flat)
![SQLite](https://img.shields.io/badge/SQLite-supported-003B57?style=flat&logo=sqlite&logoColor=white)
![Pytest](https://img.shields.io/badge/Pytest-ready-0A9EDC?style=flat&logo=pytest&logoColor=white)

V3-only **Assess** prototype for AWARE (Parser + Executor), with explicit separation:

- `tools/` for file/data handling
- `templates/` for agent templates to instantiate
- minimal ADK agents (`ParserAgent`, `ExecutorAgent`, dynamic sub-agents)
- LLM used as the decision engine (Parser and Executor sub-agents)
- strict analysis filtering on `failure_time_range_ts`

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Create your local `.env`:

```bash
cp .env.example .env
```

Choose `AWARE_LLM_PROVIDER=openai-compatible` with `OPENAI_API_KEY` and
`OPENAI_MODEL`, or `AWARE_LLM_PROVIDER=ollama` with `OLLAMA_API_KEY` and
`OLLAMA_MODEL` for Ollama Cloud. Keep keys in `.env` or the shell.
The raw datasets are ignored by Git and must be extracted before analysis.

## Run a Dataset Batch

The same CLI command structure runs Nezha and OpenRCA. AWARE prepares the
incident questions from the dataset catalogue; no manual `--query` is needed:

```bash
aware batch --dataset nezha --source /path/to/extracted/Nezha --limit 5
aware batch --dataset openrca_bank --source /path/to/extracted/Bank-003 --limit 5
aware batch --dataset openrca_market --source /path/to/extracted/Market-002 --limit 5
aware batch --dataset openrca_telecom --source /path/to/extracted/Telecom-001 --limit 5
```

`--source` can also point to a telemetry folder inside the extracted dataset.
Add `--max-agents 5` to set the agent ceiling per incident, `--max-attempts 5`
to set the Parser retry limit, or `--llm-model MODEL` to override the selected
provider's model. The provider, reasoning, and memory settings come from `.env`.

The CLI prints the active provider, model, reasoning, memory, incident count,
and agent ceiling at startup. Each batch keeps its catalogue, configuration,
results, journals, reports, graphs, and optional per-incident SQLite memory
under a UTC timestamped `output/batches/batch-cli-<timestamp>-<suffix>/` folder.
The UI uses the same timestamp format with a `batch-ui-` prefix.

See [Usage](docs/usage.md) for all batch parameters and CSV inputs.

## Other CLI Commands

```bash
aware parse \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/telemetry
```

Run the Executor step from an existing BuildSpec:

```bash
aware execute \
  --buildspec-json /path/to/buildspec.json \
  --repo /path/to/repository
```

Parser + Executor in a single command:

```bash
aware assess \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/repository
```

Run an existing CSV batch with the same per-batch output layout:

```bash
aware batch \
  --problems-csv /path/to/problems.csv \
  --limit 5
```

## Web UI

Start the UI:

```bash
aware ui --host 127.0.0.1 --port 8787
```

Then open:

```text
http://127.0.0.1:8787
```

In the UI, select a dataset, `Dataset batch`, and an extracted dataset or
telemetry directory, then click `Analyze batch`. Use `Single problem` and
`Analyze incident` for a manually entered question. The dashboard shows live
agent activity, results, and causal graphs. See [Web UI](docs/ui.md).

## Architecture Runtime

### 1) Agent Templates (Dynamic Instantiation)

Executor sub-agents are defined in:

- `templates/assess_templates.py`

Each template defines:

- `agent_name`
- `role`
- `objective`
- BuildSpec `target_field`
- `tools` to use
- `domain`

### 2) Tools (Data Handling)

Read/window/parse operations are in:

- `tools/telemetry_tools.py`

Examples:

- `load_csv_window`
- `build_llm_observation_context`
- `count_matches`
- `sample_matching_lines`
- `max_numeric_column`

### 3) Minimal Agents + LLM Decision Engine

`ExecutorAgent` instantiates sub-agents from the templates.

Each sub-agent:

- runs tools on its target file
- builds a compact observation context
- asks the LLM to decide the findings
- falls back to a heuristic path if the LLM response is invalid

Implementation:

- `agents/executor_agent.py`

Each run is automatically saved in:
- `output/json/<timestamp>_<run_id>.json`
- `output/txt/<timestamp>_<run_id>.txt`

## Knowledge DB (SQLite)

Executor sub-agent information is stored in a SQLite database:
- table `runs`
- table `events`
- table `task_results`
- table `findings`
- table `component_memory`

Default database files:
- `output/memory/run-YYYYMMDDTHHMMSSZ-xxxxxxxx.db` for standalone runs
- `output/batches/<batch-id>/memory/<run-id>.db` for CLI and UI batches

AWARE reports each resolved absolute SQLite URL at runtime.

You can override it with:
- UI: `db_url` request parameter for API callers
- CLI: `--db-url sqlite:///...`

## Provider LLM

- OpenAI-compatible APIs (`openai-compatible`) and Ollama Cloud (`ollama`, also accepts `ollama-cloud`).

Useful `.env` variables:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL` (optional, default: `https://api.openai.com/v1`)
- `OPENAI_MODEL` (optional, default: `gpt-5-mini`)
- `AWARE_LLM_PROVIDER=ollama` to use Ollama Cloud
- `OLLAMA_API_KEY` and `OLLAMA_MODEL` when using Ollama Cloud
- `OLLAMA_BASE_URL` (optional, default: `https://ollama.com/v1`)
- `OLLAMA_TIMEOUT_SECONDS` (optional, default: `120`)
- `PARSER_MAX_ATTEMPTS`
- `AWARE_PARSER_KB_FILE` (optional, default: `knowledge/parser_buildspec_kb.md`)
- `AWARE_EXECUTOR_V3_KB_FILE` (optional, default: `knowledge/executor_rca_v3_kb.md`)
- `EXECUTOR_MAX_AGENTS` (default `5`: total Executor sub-agents allowed per incident analysis)
- `EXECUTOR_MAX_EXPANSIONS` (optional: focused follow-ups after initial domain coverage; default `2`, maximum `4`)
- `AWARE_ENABLE_REASONING` (`true|false`, default `true`)
  - `false`: the LLM is still used, but in fast reasoning mode (more direct prompts)
- `AWARE_ENABLE_MEMORY` (`true|false`, default `true`)
  - `false`: disables shared inter-agent memory and SQLite writes for agent results

### BuildSpec Note (Multiple Files)

BuildSpec now accepts multiple files per domain:

- `absolute_log_file`: list of absolute paths
- `absolute_trace_file`: list of absolute paths
- `absolute_metrics_file`: list of absolute paths

## Knowledge Base (Separate File)

`ParserAgent` loads its BuildSpec instructions from an external file:

- `knowledge/parser_buildspec_kb.md`

This file contains:
- the `task_1..task_7` mapping
- the BuildSpec contract
- normalization rules
- file selection rules (log/trace/metrics)

`ExecutorAgent` loads its V3 knowledge base:

- `knowledge/executor_rca_v3_kb.md`

This file contains:
- possible components/reasons
- telemetry file structure (header + timestamp units)
- analysis rules to avoid false components (for example, file name)

You can point to another file with:

```bash
export AWARE_PARSER_KB_FILE=/path/to/my_kb.md
```

## Tests

```bash
pytest -q
```

## Citation

If you use this project in your research, please cite:

```bibtex
@inproceedings{sanwouo:hal-05402186,
  TITLE = {{Dynamic Agent Generation for Self-Adaptive Root Cause Analysis}},
  AUTHOR = {Sanwouo, Brell and Temple, Paul and Quinton, Cl{\'e}ment},
  URL = {https://hal.science/hal-05402186},
  BOOKTITLE = {{SEAMS'26 - 21st International Conference on Software Engineering for Adaptive and Self-Managing Systems}},
  ADDRESS = {Rio de Janeiro, Brazil},
  YEAR = {2026},
  MONTH = Apr,
  PDF = {https://hal.science/hal-05402186v1/file/SEAMS26.pdf},
  HAL_ID = {hal-05402186},
  HAL_VERSION = {v1},
}
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## Support

Questions, ideas, or collaborations are welcome. Feel free to reach out: brell.sanwouo@inria.fr | sanwouobrell@gmail.com
