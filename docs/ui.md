# Web UI

The web UI provides a live view of the Parser and Executor runtime. It is useful when you want to inspect agent events, generated BuildSpecs, findings, anomalies, and root-cause synthesis without reading raw artifacts.

## Start the UI

```bash
aware ui --host 127.0.0.1 --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

The UI always runs the V3 strategy. V3 keeps the initial opinions independent, adds conservative JVM, MySQL, and
Redis metric experts, reconciles their evidence after analysis, and emits an
inspectable causal graph. The graph separates raw evidence, observed components,
candidate hypotheses, contradictions, and the selected outcome.

## Main Inputs

| Field | Description |
| --- | --- |
| Dataset | Select `Nezha`, `OpenRCA · Bank`, `OpenRCA · Market`, or `OpenRCA · Telecom` to activate the corresponding paths, timezone, catalogue, and telemetry layout. |
| Telemetry directory | Path to the extracted telemetry case or repository. Selecting Nezha pre-fills the locally available example case. |
| Incident question | Incident request sent to the Parser agent. Include the date and the start/end of the failure interval. |
| Incident timezone | Timezone used to convert the interval to UNIX timestamps. Nezha uses `UTC`; the Bank benchmark historically uses `UTC+08:00`. |
| Safety task budget | Optional stricter cap for ephemeral agents. Blank uses the controlled V3 policy: up to three domain agents, independent metric experts, plus at most two justified follow-ups. |
| Memory database | Optional SQLite database URL. Leave blank to use the configured default. |

## Runtime Panels

- The fixed configuration sidebar keeps the incident inputs and runtime status visible.
- **Root cause synthesis** surfaces the component, reason, occurrence time, and confidence first.
- **Live analysis** shows streamed events and the latest state of each agent.
- **Findings** presents evidence and anomalies as readable cards.
- **Causal graph** visualizes evidence-to-hypothesis support and contradictions. `Download PNG` exports the currently selected single-run or batch-incident graph at 2× resolution with a white background.
- **Technical data** keeps the BuildSpec, scope, synthesis, and final JSON available for inspection.
- Run metrics show elapsed time, event count, finding count, the exact number of ephemeral agents created, and execution state.

The dataset, timezone, source path, database URL, and agent limit are retained locally in the browser. Live monitoring can be disconnected; this closes the browser stream but does not cancel an assessment already running on the server.

## Nezha example

Select `Nezha`. The UI switches the timezone to `UTC`, leaves the adaptive agent budget unlimited, uses the locally extracted example when available, and inserts this question:

```text
On January 30, 2023, between 11:50:30 and 11:53:30 UTC, one failure occurred. Identify the root cause component, occurrence time, and reason.
```

The interval must contain UTC clock times as stored by Nezha. No manual conversion to UTC+08 is needed. The final occurrence timestamp is displayed as a readable date and time in the selected timezone.

## Nezha CSV batch

The UI supports both `Single problem` and `CSV batch` run modes. To assess the complete dataset:

1. Select `Nezha`, then select `CSV batch`.
2. Wait for the UI to load the 101 incidents automatically from the official `*-fault_list.json` files.
3. Leave `Problem limit` blank to run all 101 incidents, or enter a smaller number for a smoke test.
4. Click `Analyze batch`.

Downloading and uploading a problems CSV is optional. The download link exports the same automatically generated catalogue, while the file field can deliberately replace it with a custom AWARE catalogue.

Problems run sequentially. As soon as the CSV is loaded, the full queue is visible with a `PENDING` status. During execution, exactly one row changes to `RUNNING`; completed rows become `PASS` or `FAIL`. The `Agents` column reports how many ephemeral agents were created for each incident. The `Graph` column opens the causal graph for that row. The graph view also provides an incident selector, so completing a later assessment does not remove access to earlier graphs in the same batch. The browser also shows progress, overall success rate, and separate component, reason, and time rates. `Export results CSV` downloads the detailed predictions, agent counts, scores, run IDs, and log links after at least one problem completes.

Every browser batch receives a unique batch ID. The backend persists a batch manifest and complete per-incident journals even if the browser connection closes:

```text
output/batches/<batch_id>/manifest.json
output/batches/<batch_id>/incidents/<problem_id>.json
output/batches/<batch_id>/incidents/<problem_id>.log
```

The JSON journal contains the run ID, status, exact agent count, every emitted runtime event, artifact paths, error details, the causal graph when produced, and the complete result payload. The batch table exposes `JSON` and `TXT` links per incident, while `Backend batch log` opens the manifest for the whole batch.

The accepted input schema is:

```text
problem_id,system,date,start_time,end_time,timezone_offset_minutes,source_path,expected_component,expected_time,expected_reason
```

The generated catalogue contains 56 OnlineBoutique incidents and 45 TrainTicket incidents. Each row is a simple incident, not a Parser task. The UI constructs the standard diagnostic question from its date and UTC interval. Ground-truth columns are only used by the browser to score a completed diagnosis; they are not sent to ParserAgent or ExecutorAgent.

Scoring is deliberately strict and reproducible:

- component: exact pod-name match;
- reason: normalized match, so `network_delay` and `network delay` are equivalent;
- occurrence time: predicted UNIX timestamp within 60 seconds of the label;
- overall success: all three checks pass.

### Adaptive agent lifecycle

Nezha leaves `Safety task budget` blank. ExecutorAgent creates at most one seed agent per available domain, in the order metrics, logs, then traces. All selected files in a domain are consolidated into one context and one LLM decision, so neither the number of files nor the number of CSV shards multiplies the number of agents or model calls. Each ephemeral agent reads shared memory, analyzes its assigned evidence need, writes its result and findings to SQLite, and terminates.

After each useful seed result, strict convergence rules may cancel domains that can no longer change the result: one unique long-form KPI anomaly can stop after metrics, and one repeated local return/exception can stop after logs. Propagated caller errors and competing hypotheses never trigger this shortcut. Otherwise a cross-domain synthesis checkpoint runs after normal coverage. A medium/low-confidence result, any unresolved field, or an explicit coordinator verification request can create a component-focused follow-up even when a provisional component has already been named. Requested domains may revisit the same files with this narrower focus. There are at most two follow-ups and no recursive expansion, so the normal range is one to three seed agents and five remains the default hard adaptive maximum. `Safety task budget` can impose a smaller ceiling.

Canonical CSV incidents for Nezha and OpenRCA use a deterministic, validated Parser fast path. Free-form questions still use the retrying Parser LLM. OpenRCA catalogues join the official observation windows from `query.csv` with the component, timestamp, and reason ground truth from `record.csv`; a window is split at midpoints only when it contains multiple labelled incidents.

The root-cause reason list in executor knowledge is a non-exhaustive candidate vocabulary. Agents may add a telemetry-backed novel cause; absence from the initial list does not invalidate it.

## OpenRCA Bank, Market, and Telecom

The three OpenRCA datasets use local datetimes in `UTC+08:00`. Their `record.csv` timestamps are converted consistently with that offset; do not select UTC for these catalogues.

Select the desired OpenRCA dataset and then `CSV batch`. The UI retrieves the catalogue from the backend automatically and immediately populates the incident queue. The download link remains available as an optional export. Each catalogue contains one simple full-RCA incident per ground-truth record:

| Dataset | Problems | Systems |
| --- | ---: | --- |
| Bank | 136 | `bank` |
| Market | 148 | `cloudbed-1` and `cloudbed-2` |
| Telecom | 51 | `telecom` |

Each problem asks for component, occurrence time, and reason using the official interval parsed from `query.csv`. This avoids converting the benchmark's partial `task_1`–`task_7` questions into UI tasks. When an official query interval contains several labelled failures, it is divided at the midpoint between consecutive `record.csv` occurrences so that every UI row remains a single incident. Market rows automatically point to the correct cloudbed directory.

OpenRCA file selection is deterministic: only telemetry files below the incident's `YYYY_MM_DD` directory are passed to ExecutorAgent. A telemetry family may be explicitly empty when it does not exist. In particular, Telecom contains traces and metrics but no log files, so its BuildSpec uses `absolute_log_file: []` instead of inventing a path or rejecting the incident.

Agent counts, per-incident JSON/TXT journals, the batch manifest, scoring, and results CSV work exactly as described for Nezha. The dataset profile is also inferred from each problem ID during a batch, preventing an accidentally selected UI profile from applying Nezha rules to an OpenRCA CSV.

## Typical Flow

1. Select the dataset profile.
2. Enter the telemetry directory and incident query.
3. Verify the incident timezone and click `Analyze incident`.
4. Watch Parser validation and Executor analysis events.
5. Review the final JSON sections in the result panels.
