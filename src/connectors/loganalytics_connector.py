"""
Azure Log Analytics MCP Connector.

Connects to the Log Analytics MCP Server to query Spark driver/executor logs,
Synapse pipeline run history, and custom application logs. Provides structured
access to log data for failure detection and root cause analysis.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

from src.connectors.mcp_base import BaseMCPClient
from src.models import AlertSource, DocumentChunk, PipelineType, RawLogEntry

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────
# Pre-built KQL Queries for Log Analytics
# ─────────────────────────────────────────────────

LA_QUERIES = {
    "spark_driver_errors": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        | where Level_s in ("ERROR", "FATAL")
        | project TimeGenerated, Level_s, Message_s, LoggerName_s,
                  ClusterId_s, SparkJobId_s, ExceptionClass_s,
                  StackTrace_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "spark_executor_failures": """
        SparkExecutorEvent_CL
        | where TimeGenerated > ago({timespan})
        | where EventType_s == "ExecutorRemoved" or EventType_s == "ExecutorFailed"
        | project TimeGenerated, ExecutorId_s, Reason_s, ClusterId_s,
                  SparkJobId_s, EventType_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "synapse_pipeline_failures": """
        SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        | where Status_s == "Failed"
        | project TimeGenerated, PipelineName_s, RunId_s, ErrorMessage_s,
                  ActivityName_s, ActivityType_s, Duration_s,
                  ParameterJson_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "synapse_activity_errors": """
        SynapseActivityRun_CL
        | where TimeGenerated > ago({timespan})
        | where Status_s == "Failed"
        | project TimeGenerated, PipelineName_s, ActivityName_s,
                  ActivityType_s, ErrorCode_s, ErrorMessage_s,
                  Input_s, Output_s, Duration_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "oom_errors": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        | where Message_s has "OutOfMemoryError" or Message_s has "OOM"
                or ExceptionClass_s has "OutOfMemoryError"
        | project TimeGenerated, Message_s, ClusterId_s, SparkJobId_s,
                  StackTrace_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "timeout_errors": """
        union SparkLoggingEvent_CL, SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        | where Message_s has "timeout" or Message_s has "Timeout"
                or ErrorMessage_s has "timeout"
        | project TimeGenerated, Message_s, ErrorMessage_s,
                  Type, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    "connection_errors": """
        union SparkLoggingEvent_CL, SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        | where Message_s has_any ("ConnectionRefused", "ConnectionReset",
                "ConnectionTimeout", "SocketException", "UnknownHostException")
                or ErrorMessage_s has_any ("connection", "socket", "network")
        | project TimeGenerated, Message_s, ErrorMessage_s, Type
        | order by TimeGenerated desc
        | take {limit}
    """,

    "pipeline_run_history": """
        SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        | where PipelineName_s == "{pipeline_name}"
        | project TimeGenerated, Status_s, RunId_s, Duration_s,
                  ErrorMessage_s, ActivityName_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "error_frequency": """
        union SparkLoggingEvent_CL, SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        | where Level_s == "ERROR" or Status_s == "Failed"
        | summarize ErrorCount=count() by bin(TimeGenerated, 1h)
        | order by TimeGenerated desc
    """,

    "top_error_classes": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        | where Level_s in ("ERROR", "FATAL")
        | summarize Count=count(), FirstSeen=min(TimeGenerated),
                    LastSeen=max(TimeGenerated),
                    SampleMessage=any(Message_s)
          by ExceptionClass_s
        | order by Count desc
        | take {limit}
    """,

    "custom_query": "{query}",
}


class LogAnalyticsMCPClient(BaseMCPClient):
    """
    MCP client for Azure Log Analytics.

    Provides methods to:
    - Query Spark driver and executor logs
    - Retrieve Synapse pipeline failures
    - Detect specific error patterns (OOM, timeout, connection)
    - Analyze error frequencies and trends
    - Index log data for RAG knowledge base
    """

    def __init__(
        self,
        server_url: str,
        workspace_id: str = "",
        default_timespan: str = "P1D",
        **kwargs: Any,
    ):
        super().__init__(server_url=server_url, **kwargs)
        self.workspace_id = workspace_id
        self.default_timespan = default_timespan

    async def validate_connection(self) -> bool:
        """Validate connectivity to Log Analytics."""
        try:
            result = await self.execute_query("print 'ok'", timespan="PT5M")
            return result is not None
        except Exception as e:
            logger.error("log_analytics_validation_failed", error=str(e))
            return False

    async def execute_query(
        self,
        query: str,
        timespan: Optional[str] = None,
    ) -> Any:
        """
        Execute a KQL query against Log Analytics workspace.

        Args:
            query: KQL query string
            timespan: ISO 8601 duration (e.g., 'P1D' for 1 day)

        Returns:
            Query results from MCP server
        """
        ts = timespan or self.default_timespan
        logger.info("la_query", query_preview=query[:120], timespan=ts)

        return await self.call_tool(
            tool_name="query_logs",
            arguments={
                "query": query.strip(),
                "workspace_id": self.workspace_id,
                "timespan": ts,
            },
        )

    async def get_spark_driver_errors(
        self,
        timespan: str = "24h",
        limit: int = 50,
    ) -> list[RawLogEntry]:
        """Retrieve Spark driver-level errors."""
        query = LA_QUERIES["spark_driver_errors"].format(
            timespan=timespan, limit=limit
        )
        raw = await self.execute_query(query)
        return self._parse_spark_logs(raw)

    async def get_spark_executor_failures(
        self,
        timespan: str = "24h",
        limit: int = 50,
    ) -> list[RawLogEntry]:
        """Retrieve Spark executor removal/failure events."""
        query = LA_QUERIES["spark_executor_failures"].format(
            timespan=timespan, limit=limit
        )
        raw = await self.execute_query(query)
        return self._parse_spark_executor_events(raw)

    async def get_synapse_pipeline_failures(
        self,
        timespan: str = "24h",
        limit: int = 50,
    ) -> list[RawLogEntry]:
        """Retrieve Synapse pipeline run failures."""
        query = LA_QUERIES["synapse_pipeline_failures"].format(
            timespan=timespan, limit=limit
        )
        raw = await self.execute_query(query)
        return self._parse_synapse_failures(raw)

    async def get_synapse_activity_errors(
        self,
        timespan: str = "24h",
        limit: int = 50,
    ) -> list[RawLogEntry]:
        """Retrieve Synapse activity-level errors."""
        query = LA_QUERIES["synapse_activity_errors"].format(
            timespan=timespan, limit=limit
        )
        raw = await self.execute_query(query)
        return self._parse_synapse_activities(raw)

    async def detect_oom_errors(
        self,
        timespan: str = "24h",
        limit: int = 20,
    ) -> list[RawLogEntry]:
        """Detect OutOfMemoryError occurrences."""
        query = LA_QUERIES["oom_errors"].format(timespan=timespan, limit=limit)
        raw = await self.execute_query(query)
        return self._parse_spark_logs(raw)

    async def detect_timeout_errors(
        self,
        timespan: str = "24h",
        limit: int = 20,
    ) -> list[RawLogEntry]:
        """Detect timeout-related errors."""
        query = LA_QUERIES["timeout_errors"].format(timespan=timespan, limit=limit)
        raw = await self.execute_query(query)
        return self._parse_generic_logs(raw, "timeout")

    async def detect_connection_errors(
        self,
        timespan: str = "24h",
        limit: int = 20,
    ) -> list[RawLogEntry]:
        """Detect connection/network related errors."""
        query = LA_QUERIES["connection_errors"].format(timespan=timespan, limit=limit)
        raw = await self.execute_query(query)
        return self._parse_generic_logs(raw, "connection")

    async def get_pipeline_history(
        self,
        pipeline_name: str,
        timespan: str = "7d",
        limit: int = 20,
    ) -> Any:
        """Get run history for a specific pipeline."""
        query = LA_QUERIES["pipeline_run_history"].format(
            pipeline_name=pipeline_name, timespan=timespan, limit=limit
        )
        return await self.execute_query(query)

    async def get_error_frequency(
        self,
        timespan: str = "24h",
    ) -> Any:
        """Get hourly error count trend."""
        query = LA_QUERIES["error_frequency"].format(timespan=timespan)
        return await self.execute_query(query)

    async def get_top_error_classes(
        self,
        timespan: str = "7d",
        limit: int = 10,
    ) -> Any:
        """Get most frequent error classes."""
        query = LA_QUERIES["top_error_classes"].format(
            timespan=timespan, limit=limit
        )
        return await self.execute_query(query)

    async def run_custom_query(self, query: str) -> Any:
        """Run a custom KQL query provided by the user."""
        return await self.execute_query(query)

    async def get_logs_for_indexing(
        self,
        timespan: str = "30d",
        limit: int = 200,
    ) -> list[DocumentChunk]:
        """
        Retrieve error logs for RAG indexing.

        Returns:
            DocumentChunk objects for vector store
        """
        # Combine Spark and Synapse errors
        spark_logs = await self.get_spark_driver_errors(timespan=timespan, limit=limit)
        synapse_logs = await self.get_synapse_pipeline_failures(timespan=timespan, limit=limit)

        all_logs = spark_logs + synapse_logs
        chunks = []

        for log in all_logs:
            content = (
                f"Source: {log.source.value}\n"
                f"Level: {log.level}\n"
                f"Pipeline: {log.pipeline_name or 'unknown'}\n"
                f"Message: {log.message}\n"
                f"Job ID: {log.job_id or 'N/A'}\n"
                f"Metadata: {log.metadata}"
            )

            chunks.append(
                DocumentChunk(
                    chunk_id=f"log_{log.source.value}_{log.timestamp.isoformat()}",
                    source_type="log_analytics",
                    source_id=log.job_id or log.timestamp.isoformat(),
                    source_title=f"{log.source.value} error - {log.pipeline_name or 'unknown'}",
                    content=content,
                    metadata={
                        "source": log.source.value,
                        "level": log.level,
                        "pipeline": log.pipeline_name,
                        "timestamp": log.timestamp.isoformat(),
                    },
                )
            )

        logger.info("la_indexed", total_chunks=len(chunks))
        return chunks

    # ─── Internal Parsers ─────────────────────────

    def _parse_spark_logs(self, raw: Any) -> list[RawLogEntry]:
        """Parse Spark log entries from Log Analytics results."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.LOG_ANALYTICS,
                        level=row.get("Level_s", "ERROR"),
                        message=row.get("Message_s", ""),
                        metadata={
                            "exception_class": row.get("ExceptionClass_s", ""),
                            "logger": row.get("LoggerName_s", ""),
                            "stack_trace": str(row.get("StackTrace_s", ""))[:2000],
                        },
                        pipeline_name=f"spark_{row.get('ClusterId_s', 'unknown')}",
                        job_id=row.get("SparkJobId_s"),
                        cluster_id=row.get("ClusterId_s"),
                    )
                )
            except Exception as e:
                logger.warning("spark_log_parse_error", error=str(e))

        return entries

    def _parse_spark_executor_events(self, raw: Any) -> list[RawLogEntry]:
        """Parse Spark executor failure events."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.LOG_ANALYTICS,
                        level="ERROR",
                        message=f"Executor {row.get('ExecutorId_s', '?')} {row.get('EventType_s', 'failed')}: "
                                f"{row.get('Reason_s', 'Unknown reason')}",
                        metadata={
                            "executor_id": row.get("ExecutorId_s", ""),
                            "event_type": row.get("EventType_s", ""),
                        },
                        pipeline_name=f"spark_{row.get('ClusterId_s', 'unknown')}",
                        job_id=row.get("SparkJobId_s"),
                        cluster_id=row.get("ClusterId_s"),
                    )
                )
            except Exception as e:
                logger.warning("executor_event_parse_error", error=str(e))

        return entries

    def _parse_synapse_failures(self, raw: Any) -> list[RawLogEntry]:
        """Parse Synapse pipeline failure entries."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.SYNAPSE,
                        level="ERROR",
                        message=row.get("ErrorMessage_s", "Pipeline failed"),
                        metadata={
                            "pipeline_name": row.get("PipelineName_s", ""),
                            "run_id": row.get("RunId_s", ""),
                            "activity": row.get("ActivityName_s", ""),
                            "activity_type": row.get("ActivityType_s", ""),
                            "duration": row.get("Duration_s", ""),
                        },
                        pipeline_name=row.get("PipelineName_s", "unknown_synapse_pipeline"),
                        job_id=row.get("RunId_s"),
                    )
                )
            except Exception as e:
                logger.warning("synapse_failure_parse_error", error=str(e))

        return entries

    def _parse_synapse_activities(self, raw: Any) -> list[RawLogEntry]:
        """Parse Synapse activity-level errors."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.SYNAPSE,
                        level="ERROR",
                        message=(
                            f"Activity '{row.get('ActivityName_s', '?')}' failed: "
                            f"{row.get('ErrorMessage_s', row.get('ErrorCode_s', 'Unknown'))}"
                        ),
                        metadata={
                            "pipeline_name": row.get("PipelineName_s", ""),
                            "activity_name": row.get("ActivityName_s", ""),
                            "activity_type": row.get("ActivityType_s", ""),
                            "error_code": row.get("ErrorCode_s", ""),
                            "duration": row.get("Duration_s", ""),
                        },
                        pipeline_name=row.get("PipelineName_s", "unknown"),
                    )
                )
            except Exception as e:
                logger.warning("synapse_activity_parse_error", error=str(e))

        return entries

    def _parse_generic_logs(self, raw: Any, category: str) -> list[RawLogEntry]:
        """Parse generic log entries with a category tag."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                message = row.get("Message_s") or row.get("ErrorMessage_s") or "Unknown error"
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.LOG_ANALYTICS,
                        level="ERROR",
                        message=str(message),
                        metadata={"category": category, "type": row.get("Type", "")},
                        pipeline_name=row.get("PipelineName_s", f"unknown_{category}"),
                    )
                )
            except Exception as e:
                logger.warning("generic_log_parse_error", error=str(e))

        return entries

    @staticmethod
    def _normalize_rows(raw: Any) -> list[dict[str, Any]]:
        """Normalize raw response to list of dicts."""
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
        if isinstance(raw, dict):
            tables = raw.get("tables", [])
            if tables:
                columns = [c.get("name", f"col_{i}") for i, c in enumerate(tables[0].get("columns", []))]
                return [dict(zip(columns, row)) for row in tables[0].get("rows", [])]
            return [raw]
        if isinstance(raw, str):
            return []
        return []

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Parse timestamp from various formats."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        return datetime.utcnow()
