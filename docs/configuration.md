# Configuration

Aware reads configuration from environment variables and optional CLI parameters.

## LLM Provider

Aware currently uses an OpenAI-compatible provider.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required | API key used by the LLM client. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. |
| `OPENAI_MODEL` | `gpt-5-mini` | Default model used by the agents. |

## Parser and Executor

| Variable | Default | Description |
| --- | --- | --- |
| `PARSER_MAX_ATTEMPTS` | `5` | Maximum parser repair attempts. |
| `EXECUTOR_MAX_AGENTS` | `5` | Global limit for instantiated executor sub-agents. |
| `AWARE_PARSER_KB_FILE` | `knowledge/parser_buildspec_kb.md` | Parser knowledge file path. |
| `AWARE_EXECUTOR_KB_FILE` | `knowledge/executor_rca_kb.md` | Executor knowledge file path. |

## Runtime Toggles

| Variable | Default | Description |
| --- | --- | --- |
| `AWARE_ENABLE_REASONING` | `true` | Enables richer reasoning prompts. |
| `AWARE_ENABLE_MEMORY` | `true` | Enables shared memory and SQLite writes. |

When `AWARE_ENABLE_REASONING=false`, the LLM is still used, but prompts are more direct.

When `AWARE_ENABLE_MEMORY=false`, inter-agent memory and SQLite writes are disabled.

## Knowledge DB

Default database URL:

```text
sqlite:///output/assess.db
```

Override it from the CLI:

```bash
aware assess \
  --query "checkout timeout" \
  --repo /path/to/repository \
  --db-url sqlite:///output/custom-assess.db
```

Or pass it directly with the CLI option `--db-url`.
