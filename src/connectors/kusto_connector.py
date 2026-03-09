"""
Kusto / Azure Data Explorer MCP Connector.

Connects to the Kusto MCP Server to execute KQL queries, retrieve
ingestion errors, query failures, and pipeline telemetry data.
Supports both direct KQL execution and pre-built diagnostic queries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

from src.connectors.mcp_base import BaseMCPClient, MCPClientError
from src.models import AlertSource, RawLogEntry

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────
# Pre-built KQL Diagnostic Queries
# ─────────────────────────────────────────────────

KQL_QUERIES = {
    "ingestion_failures": """
        .show ingestion failures
        | where FailedOn > ago({timespan})
        | project FailedOn, Table, IngestionSourcePath, Details, ErrorCode,
                  RootActivityId, OperationId
        | order by FailedOn desc
        | take {limit}
    """,

    "query_errors": """
        .show queries
        | where StartedOn > ago({timespan})
        | where State == "Failed"
        | project StartedOn, Text, FailureReason, User, Application,
                  Duration, ResourceUtilization
        | order by StartedOn desc
        | take {limit}
    """,

    "table_ingestion_latency": """
        .show table {table_name} ingestion status
        | where Timestamp > ago({timespan})
        | summarize AvgLatency=avg(IngestionLatencyInSeconds),
                    MaxLatency=max(IngestionLatencyInSeconds),
                    FailedCount=countif(Status != "Succeeded"),
                    TotalCount=count()
          by bin(Timestamp, 1h)
        | order by Timestamp desc
    """,

    "cluster_health": """
        .show diagnostics
        | project Timestamp, IsHealthy, MachinesTotal, MachinesDown,
                  IngestionUtilization, CacheUtilization, CpuUtilization
        | take 1
    """,

    "recent_commands": """
        .show commands
        | where StartedOn > ago({timespan})
        | where State == "Failed"
        | project StartedOn, CommandType, Text, FailureReason,
                  Duration, ResourceUtilization, Principal
        | order by StartedOn desc
        | take {limit}
    """,

    "data_freshness": """
        {table_name}
        | summarize MaxTimestamp=max({timestamp_column})
        | extend DataAge=now()-MaxTimestamp
        | project MaxTimestamp, DataAge,
                  IsStale=DataAge > {staleness_threshold}
    """,

    "error_pattern_analysis": """
        .show ingestion failures
        | where FailedOn > ago({timespan})
        | summarize FailureCount=count(), FirstSeen=min(FailedOn),
                    LastSeen=max(FailedOn)
          by ErrorCode, Table
        | order by FailureCount desc
        | take {limit}
    """,

    "similar_errors": """
        .show ingestion failures
        | where FailedOn > ago({timespan})
        | where Details has "{error_pattern}"
        | project FailedOn, Table, Details, ErrorCode
        | order by FailedOn desc
        | take {limit}
    """,
}


class KustoMCPClient(BaseMCPClient):
    """
    MCP client for Azure Data Explorer (Kusto).

    Provides methods to:
    - Execute arbitrary KQL queries
    - Retrieve ingestion failures and query errors
    - Check cluster health and data freshness
    - Analyze error patterns for RAG similarity matching
    """

    def __init__(
        self,
        server_url: str,
        database: str,
        cluster_url: str = "",
        **kwargs: Any,
    ):
        super().__init__(server_url=server_url, **kwargs)
        self.database = database
        self.cluster_url = cluster_url

    async def validate_connection(self) -> bool:
        """Validate connectivity by running a simple KQL query."""
        try:
            result = await self.execute_query("print 'connection_ok'")
            return result is not None
        except Exception as e:
            logger.error("kusto_validation_failed", error=str(e))
            return False

    async def execute_query(
        self,
        query: str,
        database: Optional[str] = None,
        timeout_seconds: int = 120,
    ) -> Any:
        """
        Execute a KQL query via the Kusto MCP server.

        Args:
            query: KQL query string
            database: Target database (defaults to configured database)
            timeout_seconds: Query timeout

        Returns:
            Query results as returned by the MCP server
        """
        db = database or self.database
        logger.info("kusto_query", database=db, query_preview=query[:100])

        return await self.call_tool(
            tool_name="execute_query",
            arguments={
                "query": query.strip(),
                "database": db,
                "timeout": timeout_seconds,
            },
        )

    async def get_ingestion_failures(
        self,
        timespan: str = "24h",
        limit: int = 50,
    ) -> list[RawLogEntry]:
        """
        Retrieve recent ingestion failures from Kusto.

        Args:
            timespan: KQL timespan (e.g., '1h', '24h', '7d')
            limit: Maximum number of results

        Returns:
            List of RawLogEntry objects parsed from failure data
        """
        query = KQL_QUERIES["ingestion_failures"].format(
            timespan=timespan, limit=limit
        )
        raw_result = await self.execute_query(query)
        return self._parse_ingestion_failures(raw_result)

    async def get_query_errors(
        self,
        timespan: str = "24h",
        limit: int = 50,
    ) -> list[RawLogEntry]:
        """Retrieve recent failed queries."""
        query = KQL_QUERIES["query_errors"].format(
            timespan=timespan, limit=limit
        )
        raw_result = await self.execute_query(query)
        return self._parse_query_errors(raw_result)

    async def get_cluster_health(self) -> dict[str, Any]:
        """Get current cluster diagnostics."""
        query = KQL_QUERIES["cluster_health"]
        return await self.execute_query(query)

    async def get_error_patterns(
        self,
        timespan: str = "7d",
        limit: int = 20,
    ) -> Any:
        """Analyze error patterns for recurring issues."""
        query = KQL_QUERIES["error_pattern_analysis"].format(
            timespan=timespan, limit=limit
        )
        return await self.execute_query(query)

    async def find_similar_errors(
        self,
        error_pattern: str,
        timespan: str = "90d",
        limit: int = 20,
    ) -> Any:
        """
        Find similar errors matching a pattern in historical data.
        Used for RAG-style similarity matching on error messages.
        """
        # Sanitize pattern for KQL injection prevention
        safe_pattern = error_pattern.replace('"', '\\"').replace("'", "\\'")[:200]
        query = KQL_QUERIES["similar_errors"].format(
            timespan=timespan, error_pattern=safe_pattern, limit=limit
        )
        return await self.execute_query(query)

    async def check_data_freshness(
        self,
        table_name: str,
        timestamp_column: str = "Timestamp",
        staleness_threshold: str = "1h",
    ) -> Any:
        """Check data freshness for a specific table."""
        query = KQL_QUERIES["data_freshness"].format(
            table_name=table_name,
            timestamp_column=timestamp_column,
            staleness_threshold=staleness_threshold,
        )
        return await self.execute_query(query)

    async def get_ingestion_latency(
        self,
        table_name: str,
        timespan: str = "24h",
    ) -> Any:
        """Get ingestion latency statistics for a table."""
        query = KQL_QUERIES["table_ingestion_latency"].format(
            table_name=table_name, timespan=timespan
        )
        return await self.execute_query(query)

    # ─── Internal Parsers ─────────────────────────

    def _parse_ingestion_failures(self, raw: Any) -> list[RawLogEntry]:
        """Parse raw Kusto ingestion failure results into RawLogEntry models."""
        entries = []
        if not raw or not isinstance(raw, (list, str)):
            return entries

        # Handle both tabular (list of dicts) and text responses
        rows = raw if isinstance(raw, list) else self._parse_text_table(raw)

        for row in rows:
            if isinstance(row, dict):
                try:
                    entries.append(
                        RawLogEntry(
                            timestamp=datetime.fromisoformat(
                                str(row.get("FailedOn", datetime.utcnow().isoformat()))
                            ),
                            source=AlertSource.KUSTO,
                            level="ERROR",
                            message=str(row.get("Details", "Unknown ingestion failure")),
                            metadata={
                                "table": row.get("Table", ""),
                                "error_code": row.get("ErrorCode", ""),
                                "source_path": row.get("IngestionSourcePath", ""),
                                "activity_id": row.get("RootActivityId", ""),
                            },
                            pipeline_name=f"kusto_ingestion_{row.get('Table', 'unknown')}",
                        )
                    )
                except Exception as e:
                    logger.warning("parse_failure_skipped", error=str(e), row=str(row)[:200])
        return entries

    def _parse_query_errors(self, raw: Any) -> list[RawLogEntry]:
        """Parse raw Kusto query error results into RawLogEntry models."""
        entries = []
        if not raw or not isinstance(raw, (list, str)):
            return entries

        rows = raw if isinstance(raw, list) else self._parse_text_table(raw)

        for row in rows:
            if isinstance(row, dict):
                try:
                    entries.append(
                        RawLogEntry(
                            timestamp=datetime.fromisoformat(
                                str(row.get("StartedOn", datetime.utcnow().isoformat()))
                            ),
                            source=AlertSource.KUSTO,
                            level="ERROR",
                            message=str(row.get("FailureReason", "Query failed")),
                            metadata={
                                "query_text": str(row.get("Text", ""))[:500],
                                "user": row.get("User", ""),
                                "duration": row.get("Duration", ""),
                            },
                            pipeline_name="kusto_query",
                        )
                    )
                except Exception as e:
                    logger.warning("parse_query_error_skipped", error=str(e))
        return entries

    @staticmethod
    def _parse_text_table(text: str) -> list[dict[str, str]]:
        """Parse a text-formatted table response into list of dicts."""
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return []

        headers = [h.strip() for h in lines[0].split("|") if h.strip()]
        results = []
        for line in lines[2:]:  # Skip header separator
            values = [v.strip() for v in line.split("|") if v.strip()]
            if len(values) == len(headers):
                results.append(dict(zip(headers, values)))
        return results
