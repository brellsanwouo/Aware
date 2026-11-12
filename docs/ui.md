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
| Telemetry directory | Path to the extracted dataset or one of its telemetry folders. For a batch, the backend finds the incident labels higher in that dataset and uses the right source directory for each incident. |
| Incident question | Incident request sent to the Parser agent. Include the date and the start/end of the failure interval. |

The incident timezone is selected automatically from the dataset profile
(Nezha uses UTC; OpenRCA and Generic default to UTC+08:00). The UI displays
the `EXECUTOR_MAX_AGENTS` limit from `.env` beneath Model, Reasoning, and Memory.
This is the total number of Executor agents that may be created for one incident,
including initial domain agents, V3 specialists, and focused follow-ups; it is
not a concurrency limit. The default is 5. Each incident in a batch has its own
limit.
Each run uses a separate SQLite memory file; the memory database control is
hidden in the UI.

## Runtime Panels

- **Dashboard** opens first. It shows the selected dataset, mode and telemetry directory, background incident preparation, batch progress, run metrics, the latest diagnosis and recent agent activity. Result and event links appear when those outputs exist.
- The configuration sidebar keeps the launch controls and runtime status together. On smaller screens, the dashboard appears first with a link to the configuration form; launching returns to the dashboard.
- **Assessment summary** surfaces the component, reason, occurrence time, and confidence.
- **Live analysis** shows streamed events and the latest state of each agent for both single incidents and batches.
- **Findings** presents evidence and anomalies as readable cards.
- **Causal graph** shows the selected component, cause, time, and confidence above the diagram and inside the outcome node when a diagnosis is available. Evidence, components, hypotheses, and the outcome are grouped in related rows. Select a node by mouse or keyboard to highlight its direct relations and read its full label and provenance. The graph fits the available width on desktop; small screens keep readable text with horizontal scrolling. `Export SVG` saves a scalable vector image. `Export PNG` saves the complete graph at up to 4× resolution, capped at 24 megapixels, with a white background, regardless of the current zoom or selection.
- **Technical data** keeps the BuildSpec, scope, synthesis, and final JSON available for inspection.
- Run metrics show elapsed time, event count, finding count, the exact number of ephemeral agents created, and execution state.

The dataset and source path are retained locally in the browser. Live monitoring can be disconnected; this closes the browser stream but does not cancel an assessment already running on the server.

## Nezha example

Select `Nezha`. The UI switches the timezone to `UTC`, uses the configured agent limit, uses the locally extracted example when available, and inserts this question:

```text
On January 30, 2023, between 11:50:30 and 11:53:30 UTC, one failure occurred. Identify the root cause component, occurrence time, and reason.
```

The interval must contain UTC clock times as stored by Nezha. No manual conversion to UTC+08 is needed. The final occurrence timestamp is displayed as a readable date and time in the selected timezone.

## Nezha dataset batch

The UI supports both `Single problem` and `Dataset batch` run modes. To assess the complete dataset:

1. Select `Nezha` and `Dataset batch`.
2. Enter the extracted dataset directory or a telemetry folder inside it. The backend finds the official `*-fault_list.json` files and the UI shows the incident count when ready.
3. Leave `Problem limit` blank to run all incidents, or enter a smaller number for a smoke test.
4. Click `Analyze batch`. If preparation is still running, the click waits for it and then starts the batch.

There is no problems CSV download or upload step in the UI. The backend returns the prepared incidents directly and the browser reuses that list when the batch starts.

Problems run sequentially. Once the backend prepares the incidents, the full queue is visible with a `PENDING` status. During execution, exactly one row changes to `RUNNING`; completed rows become `PASS`, `FAIL`, or `ERROR`. The `Agents` column reports how many ephemeral agents were created for each incident. The `Graph` column opens the causal graph for that row. The graph view also provides an incident selector, so completing a later assessment does not remove access to earlier graphs in the same batch. The browser also shows progress, overall success rate, and separate component, reason, and time rates. `Export results CSV` downloads the detailed predictions, agent counts, scores, run IDs, and log links after at least one problem completes.

If execution fails, the incident row shows `ERROR` and the error message. The live event feed remains available during a batch, so a run that is still working is distinguishable from one that failed to start. The batch control is labelled `Stop batch`; it stops the browser queue while an assessment already started on the server may continue.

Every browser batch receives a unique batch ID. The backend persists a batch manifest and complete per-incident journals even if the browser connection closes:

```text
output/batches/<batch_id>/manifest.json
output/batches/<batch_id>/incidents/<problem_id>.json
output/batches/<batch_id>/incidents/<problem_id>.log
```

The JSON journal contains the run ID, status, exact agent count, every emitted runtime event, artifact paths, error details, the causal graph when produced, and the complete result payload. The batch table exposes `JSON` and `TXT` links per incident, while `Backend batch log` opens the manifest for the whole batch.

Each prepared incident contains:

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

Nezha uses the configured runtime agent limit. ExecutorAgent creates at most one seed agent per available domain, in the order metrics, logs, then traces. All selected files in a domain are consolidated into one context and one LLM decision, so neither the number of files nor the number of CSV shards multiplies the number of agents or model calls. Each ephemeral agent reads shared memory, analyzes its assigned evidence need, writes its result and findings to SQLite, and terminates.

After each useful seed result, strict convergence rules may cancel domains that can no longer change the result: one unique long-form KPI anomaly can stop after metrics, and one repeated local return/exception can stop after logs. Propagated caller errors and competing hypotheses never trigger this shortcut. Otherwise a cross-domain synthesis checkpoint runs after normal coverage. A medium/low-confidence result, any unresolved field, or an explicit coordinator verification request can create a component-focused follow-up even when a provisional component has already been named. Requested domains may revisit the same files with this narrower focus. There are at most two follow-ups and no recursive expansion, so the normal range is one to three seed agents and five remains the default hard adaptive maximum.

Canonical dataset incidents for Nezha and OpenRCA use a deterministic, validated Parser fast path. Free-form questions still use the retrying Parser LLM. OpenRCA catalogues join the official observation windows from `query.csv` with the component, timestamp, and reason ground truth from `record.csv`; a window is split at midpoints only when it contains multiple labelled incidents.

The root-cause reason list in executor knowledge is a non-exhaustive candidate vocabulary. Agents may add a telemetry-backed novel cause; absence from the initial list does not invalidate it.

## OpenRCA Bank, Market, and Telecom

The three OpenRCA datasets use local datetimes in `UTC+08:00`. Their `record.csv` timestamps are converted consistently with that offset; do not select UTC for these catalogues.

Select the desired OpenRCA dataset, `Dataset batch`, and its telemetry directory. The backend prepares the catalogue automatically and populates the incident queue. Each catalogue contains one simple full-RCA incident per ground-truth record:

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
3. Click `Analyze incident`.
4. Watch Parser validation and Executor analysis events.
5. Review the final JSON sections in the result panels.
