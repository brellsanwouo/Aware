## POSSIBLE ROOT CAUSE REASONS:
The entries below are examples and prior knowledge, not an exhaustive or closed list.
Agents may propose a new cause when telemetry provides evidence for it.
- high CPU usage
- high memory usage
- network latency
- network packet loss
- high disk I/O read usage
- high disk space usage
- high JVM CPU load
- JVM Out of Memory (OOM) Heap
- CPU fault
- db close
- db connection limit
- network delay
- network loss
- container CPU load
- container memory load
- container network latency
- container network packet corruption
- container network packet retransmission
- container packet loss
- container process termination
- container read I/O load
- container write I/O load
- node CPU load
- node CPU spike
- node disk read I/O consumption
- node disk space consumption
- node disk write I/O consumption
- node memory consumption
- return / early-return fault
- exception
- CPU contention
- CPU consumed / saturation

## POSSIBLE ROOT CAUSE COMPONENTS:
These are Bank examples only. OpenRCA Market and Telecom component identifiers must be discovered from telemetry and are not restricted to this list.
- apache01
- apache02
- Tomcat01
- Tomcat02
- Tomcat03
- Tomcat04
- MG01
- MG02
- IG01
- IG02
- Mysql01
- Mysql02
- Redis01
- Redis02

## DATA SCHEMA
### metric_app.csv
- header: `timestamp,rr,sr,cnt,mrt,tc`
- timestamp unit: seconds
- component/service field: `tc`
- key metrics: `rr`, `sr`, `cnt`, `mrt`

### metric_container.csv
- header: `timestamp,cmdb_id,kpi_name,value`
- timestamp unit: seconds
- component field: `cmdb_id`
- KPI name field: `kpi_name`
- KPI numeric field: `value`

### trace_span.csv
- header: `timestamp,cmdb_id,parent_id,span_id,trace_id,duration`
- timestamp unit: milliseconds
- component field: `cmdb_id`
- trace graph fields: `parent_id`, `span_id`, `trace_id`
- timing field: `duration`

### log_service.csv
- header: `log_id,timestamp,cmdb_id,log_name,value`
- timestamp unit: seconds
- component field: `cmdb_id`
- reason/message fields: `log_name`, `value`

### OpenRCA Market
- metrics use `timestamp,cmdb_id,kpi_name,value` plus a service-level variant;
- traces use `timestamp,cmdb_id,...,duration,...` in milliseconds;
- proxy/service logs use `log_id,timestamp,cmdb_id,log_name,value`;
- components may be pods, services, or nodes according to `cmdb_id` and metric level.

### OpenRCA Telecom
- there are no log files; do not treat this as missing evidence or invent a log path;
- trace timestamps are in camel-case `startTime` and are expressed in milliseconds;
- most metrics use `timestamp` in milliseconds, while application metrics use `startTime`;
- component identifiers appear in `cmdb_id`, `serviceName`, or the relevant resource column;
- KPI `name` values carry important signals for CPU, database, and network faults.

## RCA EXECUTION RULES FOR EXECUTOR AGENTS
- Analyze rows strictly within `failure_time_range_ts`.
- Group all selected files of one telemetry family into one initial analytical need.
- Create focused follow-ups only for a weakly supported component, never for a component already corroborated by two domains.
- Allow at most two focused follow-ups by default and do not recursively expand follow-ups.
- Use header semantics first: infer component/reason candidates from column roles.
- Root cause component must come from telemetry values (typically `cmdb_id` or `tc`) and not from file names.
- Prefer trace evidence for downstream faulty component selection when multiple component candidates exist.
- Cross-check log evidence to confirm reason and timestamp when possible.
- Aggregate repeated log failure signatures across the full window; do not infer a fault type from only the first sampled lines.
- Distinguish near-total pod CPU saturation (`cpu consumed`) from elevated but sub-saturation CPU (`cpu contention`).
- Prefer explicit failure signatures over moderate secondary CPU or latency symptoms propagated from another component.
- Keep uncertainty as `known|unknown` based on evidence completeness.
- Treat known reasons as candidate vocabulary only. Never reject a well-supported novel cause because it is absent from the list.
