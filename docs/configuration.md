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
| `EXECUTOR_MAX_EXPANSIONS` | `2` | Maximum focused follow-up agents after the initial domain coverage; clamped to 0–4. |
| `AWARE_PARSER_KB_FILE` | `knowledge/parser_buildspec_kb.md` | Parser knowledge file path. |
| `AWARE_EXECUTOR_V3_KB_FILE` | `knowledge/executor_rca_v3_kb.md` | V3 mechanism-oriented technical knowledge. |

Seed analysis is demand-driven: metrics run first, followed by logs and traces, then independent V3 metric experts when metrics are available. The safety limit is a ceiling, not a target agent count. V3 uses `sqlite:///output/assess_v3.db` by default.

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
sqlite:///output/assess_v3.db
```

Override it from the CLI:

```bash
aware assess \
  --query "checkout timeout" \
  --repo /path/to/repository \
  --db-url sqlite:///output/custom-assess.db
```

Or from the UI using the `DB URL` field.
