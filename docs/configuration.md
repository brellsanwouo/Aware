# Configuration

Aware reads configuration from environment variables and optional CLI parameters.
The same provider and runtime settings apply to dataset batches in the CLI and UI.

## LLM Provider

Aware supports OpenAI-compatible APIs and Ollama Cloud. Select the provider with
`AWARE_LLM_PROVIDER` (`openai-compatible` by default, or `ollama`). The `ollama`
value selects the cloud endpoint, not a local Ollama process.
`ollama-cloud` remains an accepted alias.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required for `openai-compatible` | API key used by the OpenAI-compatible client. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. |
| `OPENAI_MODEL` | `gpt-5-mini` | Default model used by the agents. |

For Ollama Cloud, set:

```env
AWARE_LLM_PROVIDER=ollama
OLLAMA_API_KEY=your_ollama_cloud_api_key
OLLAMA_MODEL=gemma4:31b
```

`OLLAMA_BASE_URL` defaults to `https://ollama.com/v1`, and
`OLLAMA_TIMEOUT_SECONDS` defaults to `120`. Use a model available in your
Ollama Cloud account. The `--llm-model` CLI option takes precedence over
`OLLAMA_MODEL`; the UI uses `OLLAMA_MODEL`. With the OpenAI-compatible provider,
`--llm-model` similarly overrides `OPENAI_MODEL`. The active model is resolved
from the selected provider at the start of each run.

The Ollama Cloud adapter includes the expected JSON schema in the prompt
instead of sending a `response_format` parameter. Aware checks returned JSON
in the Parser and Executor and retries invalid Parser results. An Executor
response that cannot be parsed falls back to its existing heuristic path.
An API key is required; keep it in `.env` or the process environment, not in
a command argument.

## Parser and Executor

| Variable | Default | Description |
| --- | --- | --- |
| `PARSER_MAX_ATTEMPTS` | `5` | Maximum parser repair attempts. |
| `EXECUTOR_MAX_AGENTS` | `5` | Maximum total Executor sub-agents created per incident analysis, including initial domain agents, V3 specialists, and focused follow-ups. The value is displayed in the UI. |
| `EXECUTOR_MAX_EXPANSIONS` | `2` | Maximum focused follow-up agents after the initial domain coverage; clamped to 0–4. |
| `AWARE_PARSER_KB_FILE` | `knowledge/parser_buildspec_kb.md` | Parser knowledge file path. |
| `AWARE_EXECUTOR_V3_KB_FILE` | `knowledge/executor_rca_v3_kb.md` | V3 mechanism-oriented technical knowledge. |

Seed analysis is demand-driven: metrics run first, followed by logs and traces, then independent V3 metric experts when metrics are available. The safety limit is a ceiling, not a target agent count. Each batch incident gets its own SQLite database by default when memory is enabled.

## Runtime Toggles

| Variable | Default | Description |
| --- | --- | --- |
| `AWARE_ENABLE_REASONING` | `true` | Enables richer reasoning prompts. |
| `AWARE_ENABLE_MEMORY` | `true` | Enables shared memory and SQLite writes. |

When `AWARE_ENABLE_REASONING=false`, the LLM is still used, but prompts are more direct.

When `AWARE_ENABLE_MEMORY=false`, inter-agent memory and SQLite writes are disabled.

## Knowledge DB

Default database file path (one file per run):

```text
output/memory/run-YYYYMMDDTHHMMSSZ-xxxxxxxx.db
```

The timestamp is UTC and the suffix prevents collisions when runs start in the
same second. CLI and UI batch incidents each receive a separate database under
`output/batches/<batch-id>/memory/`. AWARE resolves the path to an absolute
SQLite URL, which is included in the run's event stream and artifacts. The
default database is created when analysis
starts; the UI does not restore a previously saved database URL.

Override it from the CLI:

```bash
aware assess \
  --query "checkout timeout" \
  --repo /path/to/repository \
  --db-url sqlite:///output/custom-assess.db
```

An explicit CLI URL intentionally reuses that database and may put it outside
the batch directory. UI API callers can also pass an explicit `db_url` request
parameter; the browser UI uses a fresh database for each run.

## Batch CLI parameters

`aware batch` accepts `--dataset` (`nezha`, `openrca_bank`, `openrca_market`,
or `openrca_telecom`) and `--source` for an extracted dataset. `--limit` selects
the number of incidents; `--max-agents`, `--max-attempts`, and `--llm-model`
override the corresponding runtime values for that command. `--batch-id`,
`--results-csv`, `--graph-dir`, `--db-url`, and `--json` control naming, output
locations, and the printed summary. Omitting the output overrides keeps the
generated files together in one timestamped batch directory. See [Usage](usage.md).

The UI refreshes the configured provider and model from `.env` while it is
open. Values explicitly exported in the server's shell still take precedence.
