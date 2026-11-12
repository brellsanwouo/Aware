# Getting Started

## Requirements

- Python 3.11 or newer
- An OpenAI-compatible API endpoint and key, or an Ollama Cloud account and API key
- An **extracted** Nezha or OpenRCA dataset with its incident labels and telemetry

The raw datasets are excluded from Git. Download and extract the dataset you want
to analyze before running a batch. A ZIP file alone is not a valid `--source` for
execution.

## Install

Create a virtual environment and install AWARE:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Install the test and documentation dependencies only when needed:

```bash
pip install -e ".[dev,docs]"
```

## Choose an LLM provider

Copy the configuration template:

```bash
cp .env.example .env
```

For an OpenAI-compatible API, set `AWARE_LLM_PROVIDER=openai-compatible`,
`OPENAI_API_KEY`, and `OPENAI_MODEL`. For Ollama Cloud, set
`AWARE_LLM_PROVIDER=ollama`, `OLLAMA_API_KEY`, and `OLLAMA_MODEL`. The `ollama`
provider name selects **Ollama Cloud**. See [Configuration](configuration.md)
for the endpoint and runtime settings. Keep actual keys in `.env` or your shell;
`.env` is ignored by Git.

## Run a dataset batch

Analyze the first five incidents from an extracted Nezha dataset:

```bash
aware batch --dataset nezha --source /path/to/extracted/Nezha --limit 5
```

OpenRCA uses the same command structure. For example:

```bash
aware batch --dataset openrca_bank --source /path/to/extracted/Bank-003 --limit 5
```

Use `openrca_market` or `openrca_telecom` for the other OpenRCA datasets.
`--source` may also point to a telemetry folder inside an extracted dataset.
AWARE prepares the incident requests from the catalogue, so a batch does not
need `--query` or a manually prepared CSV. `--max-agents 5` can override the
per-incident agent ceiling from `.env`.

At startup, the CLI reports the provider, model, reasoning and memory settings,
incident count, and agent limit. It writes a timestamped directory under
`output/batches/` containing the selected catalogue, scores, reports, graphs,
journals, and one SQLite memory file per incident when memory is enabled.

For the browser interface, run `aware ui --host 127.0.0.1 --port 8787` and
open `http://127.0.0.1:8787`. See [Web UI](ui.md) for its batch workflow.
