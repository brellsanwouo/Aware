# Configuration

Aware reads configuration from environment variables and optional CLI parameters.

## LLM Provider

Aware supports OpenAI-compatible APIs and Ollama Cloud. Select the provider with
`AWARE_LLM_PROVIDER` (`openai-compatible` by default, or `ollama`).
`ollama-cloud` remains an accepted alias.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required | API key used by the LLM client. |
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
`OLLAMA_MODEL`; the UI uses `OLLAMA_MODEL`.

Ollama Cloud does not currently support structured outputs. Aware includes the
expected JSON schema in the prompt, checks returned JSON in the Parser and
Executor, and retries invalid Parser results. An Executor response that cannot
be parsed falls back to its existing heuristic path. An API key is required;
keep it in `.env` or the process environment, not in a command argument.

## Parser and Executor

| Variable | Default | Description |
| --- | --- | --- |
| `PARSER_MAX_ATTEMPTS` | `5` | Maximum parser repair attempts. |
| `EXECUTOR_MAX_AGENTS` | `5` | Maximum total Executor sub-agents created per incident analysis, including initial domain agents, V3 specialists, and focused follow-ups. The value is displayed in the UI. |
| `EXECUTOR_MAX_EXPANSIONS` | `2` | Maximum focused follow-up agents after the initial domain coverage; clamped to 0–4. |
| `AWARE_PARSER_KB_FILE` | `knowledge/parser_buildspec_kb.md` | Parser knowledge file path. |
| `AWARE_EXECUTOR_V3_KB_FILE` | `knowledge/executor_rca_v3_kb.md` | V3 mechanism-oriented technical knowledge. |

Seed analysis is demand-driven: metrics run first, followed by logs and traces, then independent V3 metric experts when metrics are available. The safety limit is a ceiling, not a target agent count. Each V3 run gets its own SQLite database by default.

## Runtime Toggles

| Variable | Default | Description |
| --- | --- | --- |
| `AWARE_ENABLE_REASONING` | `true` | Enables richer reasoning prompts. |
| `AWARE_ENABLE_MEMORY` | `true` | Enables shared memory and SQLite writes. |

When `AWARE_ENABLE_REASONING=false`, the LLM is still used, but prompts are more direct.

When `AWARE_ENABLE_MEMORY=false`, inter-agent memory and SQLite writes are disabled.

## Knowledge DB

Default database URL pattern (one file per run):

```text
sqlite:///output/memory/run-YYYYMMDDTHHMMSSZ-xxxxxxxx.db
```

The timestamp is UTC and the suffix prevents collisions when runs start in the
same second. Batch incidents each receive a separate database. The generated
URL is included in the run's event stream and artifacts. A previously saved
browser DB URL is no longer restored automatically.

Override it from the CLI:

```bash
aware assess \
  --query "checkout timeout" \
  --repo /path/to/repository \
  --db-url sqlite:///output/custom-assess.db
```

An explicit CLI URL intentionally reuses that database. The UI always creates
a fresh database for each run.

The UI refreshes the configured provider and model from `.env` while it is
open. Values explicitly exported in the server's shell still take precedence.
