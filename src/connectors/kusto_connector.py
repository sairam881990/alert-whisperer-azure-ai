"""
Kusto / Azure Data Explorer MCP Connector.

Connects to the Kusto MCP Server to execute KQL queries, retrieve
ingestion errors, query failures, and pipeline telemetry data.
Supports both direct KQL execution and pre-built diagnostic queries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Optional

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

    # M8: Additional production diagnostic queries
    "cluster_version": """
        .show version
    """,

    "ingestion_status": """
        .show ingestion failures
        | where FailedOn > ago({timespan})
        | summarize FailureCount=count(), LastFailure=max(FailedOn)
          by Table, ErrorCode
        | order by FailureCount desc
    """,

    "cache_utilization": """
        .show cache utilization
    """,

    "query_stats": """
        .show queries
        | where StartedOn > ago({timespan})
        | summarize QueryCount=count(), AvgDurationMs=avg(totalseconds(Duration) * 1000)
          by Database
        | order by QueryCount desc
    """,

    "table_details": """
        .show table {table_name} details
    """,

    "extent_stats": """
        .show database extents
        | summarize ExtentCount=count(), TotalOriginalSizeGB=sum(OriginalSize) / 1073741824.0
          by TableName
        | order by TotalOriginalSizeGB desc
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

    # ─── M2: Streaming / Batch-Fetch for Large Results ────

    async def stream_query_results(
        self,
        query: str,
        database: Optional[str] = None,
        batch_size: int = 1000,
        max_batches: int = 20,
        timeout_seconds: int = 120,
    ) -> AsyncIterator[list[Any]]:
        """
        M2: Stream KQL query results in batches instead of loading everything
        into memory at once. Useful for large result sets (e.g., bulk ingestion
        failure dumps, full table scans).

        Implements a batch-fetch pattern using `skip` / `take` pagination
        on top of the Kusto MCP execute_query tool.

        Args:
            query: Base KQL query (must NOT already contain a `take` / `skip`)
            database: Target database
            batch_size: Number of rows per batch
            max_batches: Maximum batches to fetch (safety cap)
            timeout_seconds: Per-batch query timeout

        Yields:
            Lists of rows (raw query result items) one batch at a time.
        """
        db = database or self.database
        logger.info(
            "kusto_stream_start",
            database=db,
            batch_size=batch_size,
            max_batches=max_batches,
            query_preview=query[:100],
        )

        # Wrap the caller's query in a sub-query so we can paginate safely
        base_query = query.strip().rstrip("|")

        for batch_num in range(max_batches):
            offset = batch_num * batch_size
            paginated_query = (
                f"let _base = ({base_query});\n"
                f"_base\n"
                f"| skip {offset}\n"
                f"| take {batch_size}"
            )

            try:
                result = await self.call_tool(
                    tool_name="execute_query",
                    arguments={
                        "query": paginated_query,
                        "database": db,
                        "timeout": timeout_seconds,
                    },
                )
            except Exception as e:
                logger.error(
                    "kusto_stream_batch_error",
                    batch_num=batch_num,
                    offset=offset,
                    error=str(e),
                )
                return

            # Normalise to a list
            if isinstance(result, list):
                batch = result
            elif isinstance(result, str):
                batch = self._parse_text_table(result)
            elif isinstance(result, dict) and "rows" in result:
                batch = result["rows"]
            else:
                batch = [result] if result else []

            if not batch:
                logger.info("kusto_stream_exhausted", batches_fetched=batch_num)
                return

            logger.debug(
                "kusto_stream_batch",
                batch_num=batch_num,
                rows_in_batch=len(batch),
            )
            yield batch

            # If the batch is smaller than batch_size, we've reached the end
            if len(batch) < batch_size:
                logger.info(
                    "kusto_stream_complete",
                    total_batches=batch_num + 1,
                    last_batch_rows=len(batch),
                )
                return

        logger.warning(
            "kusto_stream_max_batches_reached",
            max_batches=max_batches,
            total_rows_approx=max_batches * batch_size,
        )

    # ─── M4: Per-Connector Health Check ───────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        M4: Kusto-specific health check.
        Hits /health on the MCP server AND runs `.show version` to validate
        the underlying cluster is reachable.
        """
        status = await super().health_check()
        status["connector"] = "kusto"
        status["database"] = self.database
        status["cluster_url"] = self.cluster_url

        if status["healthy"]:
            try:
                version_result = await self.execute_query(
                    KQL_QUERIES["cluster_version"], timeout_seconds=10
                )
                status["cluster_reachable"] = True
                status["cluster_version"] = (
                    str(version_result)[:200] if version_result else "unknown"
                )
            except Exception as exc:
                status["cluster_reachable"] = False
                status["cluster_version_error"] = str(exc)

        return status

    # ─── M8: Additional diagnostic query helpers ───────────

    async def get_cache_utilization(self) -> Any:
        """M8: Retrieve cache utilization statistics from the cluster."""
        return await self.execute_query(KQL_QUERIES["cache_utilization"])

    async def get_query_stats(self, timespan: str = "1h") -> Any:
        """M8: Return per-database query counts and average duration."""
        query = KQL_QUERIES["query_stats"].format(timespan=timespan)
        return await self.execute_query(query)

    async def get_table_details(self, table_name: str) -> Any:
        """M8: Return metadata details for a specific Kusto table."""
        query = KQL_QUERIES["table_details"].format(table_name=table_name)
        return await self.execute_query(query)

    async def get_extent_stats(self) -> Any:
        """M8: Return extent counts and total original sizes per table."""
        return await self.execute_query(KQL_QUERIES["extent_stats"])

    async def get_ingestion_status(self, timespan: str = "24h") -> Any:
        """M8: Summarise recent ingestion failures by table and error code."""
        query = KQL_QUERIES["ingestion_status"].format(timespan=timespan)
        return await self.execute_query(query)

    # ─── M9: GraphRAG Dependency Metadata ─────────────────

    async def get_table_dependencies(
        self,
        database: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        M9: Return table lineage metadata for GraphRAG dependency mapping.

        Queries materialized views, update policies, and function references
        to build a dependency graph for ADF/Databricks/Synapse pipelines.

        Returns:
            Dict with keys:
            - materialized_views: list of {name, source_table, query}
            - update_policies: list of {table, policy_query, source_table}
            - databases: list of database names in the cluster
        """
        db = database or self.database
        logger.info("kusto_get_table_dependencies", database=db)

        dependencies: dict[str, Any] = {
            "database": db,
            "materialized_views": [],
            "update_policies": [],
            "databases": [],
        }

        # Fetch materialized views
        try:
            mv_result = await self.execute_query(
                ".show materialized-views",
                database=db,
                timeout_seconds=30,
            )
            if isinstance(mv_result, list):
                dependencies["materialized_views"] = [
                    {
                        "name": row.get("Name", row.get("MaterializedViewName", "")),
                        "source_table": row.get("SourceTable", ""),
                        "query": row.get("Query", "")[:500],
                        "is_healthy": row.get("IsHealthy", True),
                    }
                    for row in mv_result
                    if isinstance(row, dict)
                ]
            elif isinstance(mv_result, str):
                rows = self._parse_text_table(mv_result)
                dependencies["materialized_views"] = rows
        except Exception as exc:
            logger.warning("kusto_mv_fetch_failed", error=str(exc))
            dependencies["materialized_views_error"] = str(exc)

        # Fetch tables with update policies
        try:
            policy_result = await self.execute_query(
                ".show tables details | project TableName, UpdatePolicy",
                database=db,
                timeout_seconds=30,
            )
            if isinstance(policy_result, list):
                for row in policy_result:
                    if isinstance(row, dict) and row.get("UpdatePolicy"):
                        dependencies["update_policies"].append({
                            "table": row.get("TableName", ""),
                            "policy_raw": str(row.get("UpdatePolicy", ""))[:500],
                        })
        except Exception as exc:
            logger.warning("kusto_policy_fetch_failed", error=str(exc))
            dependencies["update_policies_error"] = str(exc)

        # List databases in the cluster for cross-db lineage
        try:
            db_result = await self.execute_query(
                ".show databases | project DatabaseName",
                database=db,
                timeout_seconds=15,
            )
            if isinstance(db_result, list):
                dependencies["databases"] = [
                    row.get("DatabaseName", "") for row in db_result
                    if isinstance(row, dict)
                ]
        except Exception as exc:
            logger.warning("kusto_db_list_failed", error=str(exc))

        return dependencies

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
