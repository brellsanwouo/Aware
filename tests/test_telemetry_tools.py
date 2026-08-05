"""Telemetry tools tests for header/semantic understanding."""

from __future__ import annotations

from tools import telemetry_tools


def test_nezha_timestamp_columns_and_units() -> None:
    assert telemetry_tools.find_timestamp_field(
        ["Time", "TimeStamp", "PodName", "CpuUsageRate(%)"]
    ) == "TimeStamp"
    assert telemetry_tools.find_timestamp_field(
        ["TraceID", "SpanID", "StartTimeUnixNano", "EndTimeUnixNano"]
    ) == "StartTimeUnixNano"

    expected = 1_675_079_501
    assert telemetry_tools.parse_timestamp_seconds("1675079501") == expected
    assert telemetry_tools.parse_timestamp_seconds("1675079501000") == expected
    assert telemetry_tools.parse_timestamp_seconds("1675079501000000") == expected
    assert telemetry_tools.parse_timestamp_seconds("1675079501000000000") == expected


def test_openrca_telecom_camel_case_start_time_is_a_timestamp() -> None:
    assert telemetry_tools.find_timestamp_field(
        ["callType", "startTime", "elapsedTime", "cmdb_id"]
    ) == "startTime"
    assert telemetry_tools.parse_timestamp_seconds("1586534700000") == 1586534700
    semantic = telemetry_tools.detect_semantic_columns(
        ["serviceName", "startTime", "name", "value", "cmdb_id"],
        domain="metrics",
    )
    assert "serviceName" in semantic["component_columns"]
    assert "name" in semantic["reason_columns"]


def test_summarize_inter_service_network_latency() -> None:
    rows = [
        {
            "SpanID": "parent",
            "ParentID": "root",
            "PodName": "gateway-123-abc",
            "EndTimeUnixNano": "1675079502000000000",
        },
        {
            "SpanID": "child",
            "ParentID": "parent",
            "PodName": "contacts-456-def",
            "EndTimeUnixNano": "1675079501000000000",
        },
    ]

    summary = telemetry_tools.summarize_inter_service_network_latency(rows)

    assert summary[0]["component"] == "contacts-456-def"
    assert summary[0]["p90_ms"] == 1000.0
    assert summary[0]["peak_ts"] == 1675079501


def test_detect_semantic_columns_and_summary() -> None:
    fieldnames = ["timestamp", "cmdb_id", "log_name", "value", "mrt", "sr"]
    semantic = telemetry_tools.detect_semantic_columns(fieldnames, domain="logs")
    assert "cmdb_id" in semantic["component_columns"]
    assert "log_name" in semantic["reason_columns"] or "value" in semantic["reason_columns"]

    rows = [
        {
            "timestamp": "1614868200",
            "cmdb_id": "MG01",
            "log_name": "timeout error",
            "value": "timeout to payment",
            "mrt": "190.42",
            "sr": "100.0",
        },
        {
            "timestamp": "1614868210",
            "cmdb_id": "IG02",
            "log_name": "retry failed",
            "value": "retry failed",
            "mrt": "42.88",
            "sr": "99.0",
        },
    ]
    summary = telemetry_tools.summarize_semantic_window(
        rows=rows,
        semantic_columns=semantic,
        timestamp_field="timestamp",
    )
    assert isinstance(summary["top_component_values"], list)
    assert isinstance(summary["top_reason_values"], list)
    assert isinstance(summary["numeric_ranges"], dict)
    bounds = summary["window_observed_time_bounds"]
    assert bounds["min_ts"] == 1614868200
    assert bounds["max_ts"] == 1614868210


def test_long_form_kpi_summary_extracts_component_and_memory_reason() -> None:
    semantic = telemetry_tools.detect_semantic_columns(
        ["timestamp", "cmdb_id", "kpi_name", "value"], domain="metrics"
    )
    signals = telemetry_tools.summarize_long_form_kpi_anomalies(
        [
            {
                "timestamp": "1614789180",
                "cmdb_id": "MG01",
                "kpi_name": "OSLinux_MEMORY_MEMUsedMemPerc",
                "value": "68.0",
            },
            {
                "timestamp": "1614789240",
                "cmdb_id": "MG01",
                "kpi_name": "OSLinux_MEMORY_MEMUsedMemPerc",
                "value": "98.0",
            },
            {
                "timestamp": "1614789240",
                "cmdb_id": "Tomcat02",
                "kpi_name": "OSLinux_CPU_CPUidleutil",
                "value": "99.8",
            },
        ],
        semantic_columns=semantic,
        timestamp_field="timestamp",
    )

    assert signals == [
        {
            "component": "MG01",
            "reason": "high memory usage",
            "kpi_name": "OSLinux_MEMORY_MEMUsedMemPerc",
            "value": 98.0,
            "minimum": 68.0,
            "range": 30.0,
            "support_count": 1,
            "timestamp": 1614789240,
        }
    ]


def test_ordered_csv_window_stops_after_requested_range(tmp_path) -> None:
    path = tmp_path / "ordered.csv"
    lines = ["timestamp,value"] + [f"{1000 + index},{index}" for index in range(1000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    view = telemetry_tools.load_csv_window(path, 1100, 1120)

    assert view.window_rows == 21
    assert view.total_rows < 1000


def test_service_latency_summary_prefers_earliest_downstream_surge() -> None:
    semantic = telemetry_tools.detect_semantic_columns(
        ["service", "timestamp", "mrt", "sr"], domain="metrics"
    )
    rows = []
    for minute in range(8):
        rows.extend(
            [
                {
                    "service": "cartservice-grpc",
                    "timestamp": str(1000 + minute * 60),
                    "mrt": str(400.0 if minute in {3, 4} else 1.0),
                    "sr": "100",
                },
                {
                    "service": "checkoutservice-grpc",
                    "timestamp": str(1000 + minute * 60),
                    "mrt": str(1500.0 if minute in {4, 5} else 40.0),
                    "sr": "100",
                },
            ]
        )

    signals = telemetry_tools.summarize_service_latency_anomalies(
        rows,
        semantic_columns=semantic,
        timestamp_field="timestamp",
    )

    assert [item["component"] for item in signals] == ["cartservice"]
    assert signals[0]["onset_ts"] == 1120


def test_nezha_metric_semantics_use_pod_name_as_component() -> None:
    semantic = telemetry_tools.detect_semantic_columns(
        [
            "TimeStamp",
            "PodName",
            "CpuUsageRate(%)",
            "NodeCpuUsageRate(%)",
            "PodClientLatencyP90(s)",
        ],
        domain="metrics",
    )

    assert semantic["component_columns"] == ["PodName"]


def test_summarize_log_failure_signals_uses_all_rows() -> None:
    semantic = telemetry_tools.detect_semantic_columns(
        ["TimeUnixNano", "PodName", "Log"], domain="logs"
    )
    rows = [
        {
            "TimeUnixNano": str((1_661_140_920 + index) * 1_000_000_000),
            "PodName": "frontend-abc",
            "Log": '{"error":"product id not specified","message":"Request error"}',
        }
        for index in range(5)
    ]

    signals = telemetry_tools.summarize_log_failure_signals(
        rows,
        semantic_columns=semantic,
        timestamp_field="TimeUnixNano",
    )

    assert signals[0]["category"] == "early_return"
    assert signals[0]["component"] == "frontend-abc"
    assert signals[0]["count"] == 5
    assert signals[0]["first_ts"] == 1_661_140_920


def test_log_semantics_prefer_pod_over_node_address() -> None:
    semantic = telemetry_tools.detect_semantic_columns(
        ["TimeUnixNano", "Node", "PodName", "Log"], domain="logs"
    )

    assert semantic["component_columns"][0] == "PodName"


def test_log_failure_summary_distinguishes_direct_return_from_propagated_error() -> None:
    semantic = telemetry_tools.detect_semantic_columns(
        ["TimeUnixNano", "PodName", "Log"], domain="logs"
    )
    rows = [
        {
            "TimeUnixNano": str((1_661_140_920 + index) * 1_000_000_000),
            "PodName": "checkout-abc",
            "Log": "Could not charge the card: charge card error.",
        }
        for index in range(4)
    ] + [
        {
            "TimeUnixNano": str((1_661_140_930 + index) * 1_000_000_000),
            "PodName": "frontend-abc",
            "Log": "Failed to complete the order: Place order error. Request error.",
        }
        for index in range(4)
    ]

    signals = telemetry_tools.summarize_log_failure_signals(
        rows,
        semantic_columns=semantic,
        timestamp_field="TimeUnixNano",
    )

    direct = next(item for item in signals if item["component"] == "checkout-abc")
    propagated = next(item for item in signals if item["component"] == "frontend-abc")
    assert direct["category"] == "early_return"
    assert direct["propagated"] is False
    assert propagated["category"] == "exception"
    assert propagated["propagated"] is True


def test_apply_component_focus_filters_rows() -> None:
    view = telemetry_tools.CsvWindowView(
        fieldnames=["timestamp", "cmdb_id", "value"],
        rows=[
            {"timestamp": "1614868200", "cmdb_id": "Tomcat01", "value": "ok"},
            {"timestamp": "1614868210", "cmdb_id": "Tomcat02", "value": "timeout"},
            {"timestamp": "1614868220", "cmdb_id": "Tomcat02", "value": "retry"},
        ],
        lines=[
            "timestamp,cmdb_id,value",
            "1614868200,Tomcat01,ok",
            "1614868210,Tomcat02,timeout",
            "1614868220,Tomcat02,retry",
        ],
        total_rows=3,
        window_rows=3,
        timestamp_field="timestamp",
    )
    focused = telemetry_tools.apply_component_focus(
        view,
        domain="logs",
        component="Tomcat02",
    )
    assert focused.component == "Tomcat02"
    assert "cmdb_id" in focused.matched_columns
    assert focused.view.window_rows == 2
