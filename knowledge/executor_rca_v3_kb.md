## PURPOSE
This knowledge describes telemetry semantics and failure mechanisms. It is not a
benchmark answer key. Components and reasons must be discovered from evidence.

## POSSIBLE ROOT CAUSE REASONS
This vocabulary is illustrative and open. A telemetry-backed novel cause is valid.
- CPU saturation or contention
- memory exhaustion or pressure
- network latency, loss, corruption, or retransmission
- disk space or I/O saturation
- JVM CPU saturation
- JVM heap exhaustion
- database connection exhaustion or unexpected close
- cache memory pressure
- process termination
- application exception or early return
- container CPU, memory, disk, or network pressure
- node CPU, memory, disk, or network pressure

## DATA SCHEMA
### metric_app.csv
- Expected fields: `timestamp,rr,sr,cnt,mrt,tc`.
- `tc` is the service/component, `rr` and `sr` describe request/success rates,
  `cnt` is traffic volume, and `mrt` is response time.
- Its timestamp is normally expressed in seconds.

### metric_container.csv
- Expected fields: `timestamp,cmdb_id,kpi_name,value`.
- `cmdb_id` is the observed component, `kpi_name` is the technical signal, and
  `value` is the numeric sample.
- Its timestamp is normally expressed in seconds.

### trace_span.csv
- Typical fields: `timestamp,cmdb_id,parent_id,span_id,trace_id,duration`.
- `cmdb_id` identifies the observed span component. Parent, span, and trace IDs
  define the call graph; `duration` is a symptom unless local evidence identifies
  the initiating component.
- Trace timestamps are commonly in milliseconds and must be normalized before
  comparison with a BuildSpec window expressed in seconds.

### log_service.csv
- Expected fields: `log_id,timestamp,cmdb_id,log_name,value`.
- `cmdb_id` is the emitting component; `log_name` and `value` carry the event or
  failure signature. Timestamps are normally expressed in seconds.

### OpenRCA Market
- Metrics use long-form container KPIs plus a service-level variant.
- Traces use component and duration fields with millisecond timestamps.
- Proxy and service logs use the normal service-log layout.
- A component may be a pod, service, or node; use the telemetry field and metric
  level rather than assuming one resource level.

### OpenRCA Telecom
- Log files are normally absent. This is a valid dataset characteristic, not
  missing evidence, and no log path should be invented.
- Traces may use camel-case `startTime` in milliseconds.
- Most metrics use millisecond `timestamp`, while application metrics may use
  `startTime`.
- Components can appear in `cmdb_id`, `serviceName`, or another resource column.
- KPI `name` values carry important CPU, database, and network semantics.

### Window and identity rules
- Analyze rows strictly inside `failure_time_range_ts` and preserve/normalize the
  source timestamp unit explicitly.
- Derive components from telemetry values such as `cmdb_id`, `tc`, or
  `serviceName`, never from a filename or a benchmark component list.
- Use header semantics before relying on example values.

## TECHNICAL MECHANISMS
### JVM
- `JVM_CPULoad` is a process-level load indicator. A high level accompanied by
  a clear upward transition supports CPU saturation; a low stable value does not.
- Heap usage commonly falls during healthy garbage collection. A single heap
  drop is therefore not sufficient proof of OOM. Seek an exceptional change,
  allocation pressure, failure logs, restarts, or service degradation.
- Repeated GC messages without a matching metric or service transition may be
  background activity rather than the initiating fault.

### MySQL
- Sustained near-capacity memory supports memory pressure, especially when it
  begins before query latency or downstream errors.
- Connection-limit or close failures require direct connection/error evidence;
  generic latency alone is insufficient.

### Redis
- A sharp step in used memory or memory percentage followed by sustained high
  usage supports cache memory pressure.
- Client latency can be propagated. Prefer a local Redis transition preceding
  client-side symptoms.

### Containers and nodes
- Prefer the earliest local resource transition over later downstream latency.
- Separate moderate elevation from saturation, and sustained load from a brief
  sampling spike.
- A component name is evidence only when read from telemetry fields, never when
  guessed from a filename or a benchmark list.

### Network
- Latency, packet loss, corruption, and retransmission are distinct mechanisms.
- A slow downstream span alone does not prove a network fault. Seek a local
  network KPI transition or repeated transport-level signature.
- When several services are slow, reconstruct parent-child timing and prefer the
  earliest local transition over propagated caller latency.

### Disk and filesystem
- Separate disk-space exhaustion from read/write I/O saturation.
- A single I/O spike is weak evidence. Prefer a sustained or exceptional change
  correlated with local service degradation.

### Logs and explicit failures
- Aggregate repeated signatures over the full window; do not classify a failure
  from the first sampled line only.
- Explicit exception, connection, termination, or resource-exhaustion signatures
  outrank moderate secondary CPU or latency symptoms.
- Absence of logs is not counter-evidence when the dataset does not provide them.

## GENERAL RCA RULES
- Group evidence by telemetry family and component while preserving provenance.
- Distinguish near-total CPU saturation from elevated but sub-saturation CPU.
- Cross-check a proposed component, mechanism, and occurrence time in another
  modality when available.
- Keep unresolved fields unresolved when direct evidence is absent.
- The reason vocabulary is open. Never reject a well-supported novel mechanism
  because it does not appear in this document.
- Candidate wording must describe one mechanism, not combine several unrelated
  explanations into a scoring-friendly sentence.

## V3 EVIDENCE POLICY
- Each expert forms its first opinion independently from raw telemetry.
- Evidence, component observations, hypotheses, and the selected outcome remain
  separate nodes in the causal graph.
- Two statements copied through shared context count as one lineage, not two
  independent confirmations.
- A hypothesis is stronger when multiple independent modalities support it and
  their timestamps form a plausible cause-before-effect sequence.
- Explicit counter-evidence must remain visible. Do not hide contradictions.
- The router may stop when component, mechanism, and time have direct support;
  otherwise it may dispatch a focused expert within the safety budget.
