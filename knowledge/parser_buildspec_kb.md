# ParserAgent Knowledge Base (BuildSpec)

This file is the authoritative instruction source for ParserAgent BuildSpec generation.

## BuildSpec Contract

Required keys (all mandatory):
- `task_type`
- `date`
- `filename_date`
- `failure_time_range`
- `failure_time_range_ts`
- `failures_detected`
- `uncertainty`
- `objective`
- `filename_date_directory`
- `absolute_log_file` (array of 0..n absolute paths)
- `absolute_trace_file` (array of 0..n absolute paths)
- `absolute_metrics_file` (array of 0..n absolute paths)

## task_type Mapping (MUST follow exactly)

- `task_1`: Determine root cause TIME only
- `task_2`: Determine root cause REASON only
- `task_3`: Determine root cause COMPONENT only
- `task_4`: Determine TIME + REASON
- `task_5`: Determine TIME + COMPONENT
- `task_6`: Determine COMPONENT + REASON
- `task_7`: Determine TIME + COMPONENT + REASON (full RCA)

## Parsing and Normalization Rules

- Date must be `YYYY-MM-DD`.
- `filename_date` must be `YYYY_MM_DD`.
- `failure_time_range.start/end` must be `HH:MM:SS` with `end > start`.
- `failure_time_range_ts.start/end` must be UNIX epoch seconds and represent the same duration as `failure_time_range`.
- Timestamp conversion reference timezone is fixed to `UTC+08:00`.
- `failures_detected` must be an integer (0 or more).
- `uncertainty` fields must be `known` or `unknown`.
- Paths must be absolute and anchored in the provided repository path.

## Repository Selection Rules

- Use scanned `repository_files` as the source of truth.
- Select all relevant available log, trace, and metric files for the incident date.
- Use an empty array only when the repository contains no source for that telemetry family (for example, OpenRCA Telecom contains no logs).
- Default to one file per category only when there is a single clear match.
- Do not invent files when candidates exist in `repository_files`.
- Prefer files under the selected `filename_date` directory.
- Ensure the selected log/trace/metrics files are distinct.

## Output Rule

- Return only one strict JSON object.
- No markdown, no explanation, no extra keys.
