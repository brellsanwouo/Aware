# AWARE Lite

AWARE Lite is the CLI-only edition of AWARE for telemetry-based root-cause assessment. It turns an incident question into a validated BuildSpec, runs short-lived specialized agents over logs, traces, and metrics, then writes structured results to disk.

This repository deliberately contains no web UI, batch benchmark runner, V3 mixture-of-experts runtime, or causal-graph renderer.

## Install

Requirements: Python 3.11+ and an OpenAI API key.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`.

## Commands

Generate a BuildSpec:

```bash
aware parse \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/telemetry
```

Execute an existing BuildSpec:

```bash
aware execute \
  --buildspec-json /path/to/buildspec.json \
  --repo /path/to/telemetry
```

Run parsing and assessment together:

```bash
aware assess \
  --query "On 2021-03-04 between 18:30:00 and 19:00:00 checkout timeout" \
  --repo /path/to/telemetry
```

Use `aware --help` or `aware COMMAND --help` for all options.

## Runtime

- `ParserAgent` converts the incident request into a validated BuildSpec.
- `ExecutorAgent` creates only the telemetry agents required by that BuildSpec.
- Each specialized agent performs a bounded task and records its result.
- Runs are saved under `output/` as JSON and text, with optional SQLite memory.

The analysis is restricted to the incident interval in `failure_time_range_ts`. BuildSpecs may reference multiple log, trace, and metrics files.

## Configuration

The main environment variables are:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL` (optional)
- `OPENAI_MODEL` (default: `gpt-5-mini`)
- `PARSER_MAX_ATTEMPTS`
- `EXECUTOR_MAX_AGENTS`
- `AWARE_ENABLE_REASONING`
- `AWARE_ENABLE_MEMORY`

More details are available in [the documentation](docs/getting-started.md).

## Tests

```bash
pytest -q
```

## Citation

```bibtex
@inproceedings{sanwouo:hal-05402186,
  TITLE = {{Dynamic Agent Generation for Self-Adaptive Root Cause Analysis}},
  AUTHOR = {Sanwouo, Brell and Temple, Paul and Quinton, Cl{\'e}ment},
  URL = {https://hal.science/hal-05402186},
  BOOKTITLE = {{SEAMS'26 - 21st International Conference on Software Engineering for Adaptive and Self-Managing Systems}},
  ADDRESS = {Rio de Janeiro, Brazil},
  YEAR = {2026},
  MONTH = Apr,
  HAL_ID = {hal-05402186}
}
```

## License

MIT — see [LICENSE](LICENSE).
