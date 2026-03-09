"""
Demo data generator for Alert Whisperer.
Provides realistic sample alerts, incidents, and logs for UI development and demos.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from src.models import (
    AlertFeedItem,
    AlertSource,
    AlertStatus,
    AnalysisResult,
    DashboardMetrics,
    HistoricalIncident,
    ICMTicket,
    ParsedFailure,
    PipelineType,
    RawLogEntry,
    RoutingDecision,
    Severity,
)


def generate_demo_alerts() -> list[ParsedFailure]:
    """Generate realistic demo alerts."""
    now = datetime.utcnow()
    return [
        ParsedFailure(
            failure_id="alert-001",
            pipeline_name="spark_etl_daily_ingest",
            pipeline_type=PipelineType.SPARK_BATCH,
            source=AlertSource.LOG_ANALYTICS,
            timestamp=now - timedelta(minutes=12),
            severity=Severity.CRITICAL,
            error_class="OutOfMemoryError",
            error_message="java.lang.OutOfMemoryError: Java heap space at org.apache.spark.sql.catalyst.expressions.UnsafeRow.copy(UnsafeRow.java:531)",
            root_cause_summary="Spark executor ran out of heap memory during a large shuffle operation in stage 12. A data skew in the customer_transactions join is causing one partition to be 10x larger than others, exhausting the 8GB executor memory.",
            affected_components=["spark_etl_daily_ingest", "customer_analytics_dashboard", "daily_reporting"],
            job_id="job-spark-2024-001",
            cluster_id="cluster-prod-01",
            log_snippet="24/03/15 02:15:33 ERROR Executor: Exception in task 47.3 in stage 12.0\njava.lang.OutOfMemoryError: Java heap space\n\tat java.util.Arrays.copyOf(Arrays.java:3236)\n\tat org.apache.spark.sql.catalyst.expressions.UnsafeRow.copy(UnsafeRow.java:531)\n\tat org.apache.spark.sql.execution.ShuffledRowRDD$$anonfun$compute$1.apply(ShuffledRowRDD.java:123)",
            rerun_url="https://databricks.com/jobs/spark_etl_daily_ingest/run",
            log_url="https://portal.azure.com/#blade/log-analytics/spark_etl_daily_ingest",
            runbook_url="https://confluence.company.com/display/RUNBOOKS/RUNBOOKS-101",
        ),
        ParsedFailure(
            failure_id="alert-002",
            pipeline_name="synapse_pipeline_customer360",
            pipeline_type=PipelineType.SYNAPSE_PIPELINE,
            source=AlertSource.SYNAPSE,
            timestamp=now - timedelta(minutes=45),
            severity=Severity.HIGH,
            error_class="UserErrorInvalidFolderPath",
            error_message="ErrorCode=UserErrorInvalidFolderPath: The folder path 'raw/customers/2024/03/16/' does not exist or is empty",
            root_cause_summary="The Synapse Copy activity expects data at the date-partitioned path but the upstream data export job hasn't completed yet. This is a timing dependency — the pipeline triggered before data landed in ADLS Gen2.",
            affected_components=["synapse_pipeline_customer360", "Customer 360 Dashboard"],
            job_id="run-synapse-2024-002",
            log_snippet="Activity 'Copy_CustomerData' failed after 3 retries.\nErrorCode: UserErrorInvalidFolderPath\nMessage: The folder path does not exist or is empty.\nSource: Azure Data Lake Storage Gen2\nPath: raw/customers/2024/03/16/",
            rerun_url="https://portal.azure.com/#blade/synapse/pipeline/customer360/run",
            log_url="https://portal.azure.com/#blade/synapse/monitor/pipeline-runs",
        ),
        ParsedFailure(
            failure_id="alert-003",
            pipeline_name="kusto_ingestion_telemetry",
            pipeline_type=PipelineType.KUSTO_INGESTION,
            source=AlertSource.KUSTO,
            timestamp=now - timedelta(minutes=5),
            severity=Severity.CRITICAL,
            error_class="Permanent_MappingNotFound",
            error_message="Permanent_MappingNotFound: Ingestion mapping 'telemetry_v2_mapping' not found for table 'TelemetryEvents'",
            root_cause_summary="Streaming ingestion from EventHub is failing because the data mapping 'telemetry_v2_mapping' was deleted or renamed during a recent schema migration. All EventHub partitions are affected — telemetry data is being dropped.",
            affected_components=["kusto_ingestion_telemetry", "Real-time dashboards", "Alerting system", "SLA monitoring"],
            log_snippet="IngestionResult: Permanent failure\nErrorCode: Permanent_MappingNotFound\nDetails: Mapping 'telemetry_v2_mapping' not found.\nTable: TelemetryEvents\nSource: EventHub partition 0-7\nImpact: All partitions affected",
            log_url="https://dataexplorer.azure.com/clusters/telemetry/databases/TelemetryDB",
        ),
        ParsedFailure(
            failure_id="alert-004",
            pipeline_name="spark_streaming_clickstream",
            pipeline_type=PipelineType.SPARK_STREAMING,
            source=AlertSource.LOG_ANALYTICS,
            timestamp=now - timedelta(hours=2),
            severity=Severity.MEDIUM,
            error_class="StreamingQueryException",
            error_message="StreamingQueryException: Query terminated with exception: org.apache.kafka.common.errors.TimeoutException: Timeout expired while fetching topic metadata",
            root_cause_summary="The Spark Structured Streaming job lost connection to Kafka brokers due to a transient network issue. The streaming query terminated but should auto-recover on restart.",
            affected_components=["spark_streaming_clickstream", "Real-time clickstream analytics"],
            job_id="job-streaming-001",
            cluster_id="cluster-streaming-01",
            log_snippet="Streaming query 'clickstream_v2' terminated.\nException: TimeoutException - Kafka broker unreachable\nRetry count: 5/5 exhausted\nLast successful batch: 2024-03-15T00:15:00Z",
            rerun_url="https://databricks.com/jobs/spark_streaming_clickstream/run",
        ),
        ParsedFailure(
            failure_id="alert-005",
            pipeline_name="synapse_dw_refresh",
            pipeline_type=PipelineType.SYNAPSE_PIPELINE,
            source=AlertSource.SYNAPSE,
            timestamp=now - timedelta(hours=1, minutes=30),
            severity=Severity.LOW,
            error_class="SqlPoolPaused",
            error_message="The dedicated SQL pool 'dw_analytics' is paused. Resume the pool to execute queries.",
            root_cause_summary="The data warehouse refresh pipeline failed because the dedicated SQL pool was paused (likely by the cost optimization auto-pause schedule). The pool needs to be resumed before the refresh can run.",
            affected_components=["synapse_dw_refresh", "Data Warehouse"],
            log_snippet="Activity 'Execute_DW_Refresh' failed.\nError: SQL pool 'dw_analytics' is in paused state.\nAction required: Resume the pool via Azure Portal or REST API.",
        ),
    ]


def generate_demo_incidents() -> list[HistoricalIncident]:
    """Generate demo historical incidents."""
    now = datetime.utcnow()
    return [
        HistoricalIncident(
            incident_id="INC-2024-0847",
            title="Spark OOM during daily customer join",
            description="Daily ETL job failed with OutOfMemoryError during customer_transactions join.",
            root_cause="Data skew in customer_id column causing partition imbalance. Top 1% of customers have 50% of transactions.",
            resolution="Added salting to the join key and increased executor memory from 8GB to 16GB. Also added adaptive query execution (AQE) config.",
            pipeline_name="spark_etl_daily_ingest",
            error_class="OutOfMemoryError",
            severity=Severity.HIGH,
            occurred_at=now - timedelta(days=15),
            resolved_at=now - timedelta(days=15) + timedelta(hours=2),
            tags=["spark", "oom", "data-skew", "etl"],
            similarity_score=0.92,
        ),
        HistoricalIncident(
            incident_id="INC-2024-0712",
            title="Kusto mapping deleted during schema migration",
            description="Telemetry ingestion stopped after schema migration script dropped old mappings.",
            root_cause="Migration script dropped all mappings including the active one. Should have created new mapping before dropping old.",
            resolution="Recreated the mapping with correct schema. Added pre-flight check to migration scripts.",
            pipeline_name="kusto_ingestion_telemetry",
            error_class="Permanent_MappingNotFound",
            severity=Severity.CRITICAL,
            occurred_at=now - timedelta(days=45),
            resolved_at=now - timedelta(days=45) + timedelta(hours=1),
            tags=["kusto", "mapping", "schema-migration"],
            similarity_score=0.88,
        ),
        HistoricalIncident(
            incident_id="INC-2024-0923",
            title="Synapse pipeline timing failure - data not landed",
            description="Customer360 pipeline failed because upstream export hadn't completed.",
            root_cause="Pipeline schedule was set to 06:00 UTC but upstream data lands between 06:30-07:00 UTC.",
            resolution="Added a Lookup activity to check for data existence before proceeding. Shifted schedule to 07:30 UTC.",
            pipeline_name="synapse_pipeline_customer360",
            error_class="UserErrorInvalidFolderPath",
            severity=Severity.MEDIUM,
            occurred_at=now - timedelta(days=30),
            resolved_at=now - timedelta(days=30) + timedelta(hours=3),
            tags=["synapse", "timing", "data-dependency"],
            similarity_score=0.85,
        ),
    ]


def generate_demo_metrics() -> DashboardMetrics:
    """Generate demo dashboard metrics."""
    return DashboardMetrics(
        total_alerts_24h=23,
        critical_count=2,
        high_count=5,
        medium_count=11,
        low_count=5,
        avg_ttd_minutes=3.2,
        avg_ttm_minutes=47.5,
        auto_resolved_count=8,
        top_error_classes=[
            {"error_class": "OutOfMemoryError", "count": 6},
            {"error_class": "TimeoutException", "count": 4},
            {"error_class": "UserErrorInvalidFolderPath", "count": 3},
            {"error_class": "Permanent_MappingNotFound", "count": 2},
            {"error_class": "StreamingQueryException", "count": 2},
            {"error_class": "AuthenticationError", "count": 2},
            {"error_class": "Other", "count": 4},
        ],
        alerts_by_pipeline=[
            {"pipeline": "spark_etl_daily_ingest", "count": 8},
            {"pipeline": "synapse_pipeline_customer360", "count": 5},
            {"pipeline": "kusto_ingestion_telemetry", "count": 4},
            {"pipeline": "spark_streaming_clickstream", "count": 3},
            {"pipeline": "synapse_dw_refresh", "count": 3},
        ],
        hourly_trend=[
            {"hour": f"{h:02d}:00", "count": random.randint(0, 5)}
            for h in range(24)
        ],
    )


def generate_demo_feed() -> list[AlertFeedItem]:
    """Generate demo alert feed items."""
    alerts = generate_demo_alerts()
    items = []
    for alert in alerts:
        items.append(
            AlertFeedItem(
                failure=alert,
                routing=RoutingDecision(
                    failure_id=alert.failure_id,
                    target_channel="dp-oncall" if "spark" in alert.pipeline_name else "analytics-oncall",
                    target_contacts=["oncall@company.com"],
                    severity=alert.severity,
                    auto_escalate=alert.severity in (Severity.CRITICAL, Severity.HIGH),
                    routing_reason="Direct ownership match",
                    confidence=0.95,
                ),
                similar_count=random.randint(0, 5),
                has_runbook=random.choice([True, False]),
            )
        )
    return items
