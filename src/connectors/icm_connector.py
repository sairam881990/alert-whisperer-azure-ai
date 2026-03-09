"""
ICM (Incident Management) MCP Connector.

Connects to the ICM MCP Server to retrieve incident data, resolution
history, and ticket metadata. Supports polling for new incidents and
querying historical incidents for RAG similarity matching.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import structlog

from src.connectors.mcp_base import BaseMCPClient
from src.models import (
    AlertStatus,
    DocumentChunk,
    HistoricalIncident,
    ICMTicket,
    Severity,
)

logger = structlog.get_logger(__name__)


class ICMMCPClient(BaseMCPClient):
    """
    MCP client for ICM (Incident Management System).

    Provides methods to:
    - Poll for new or updated incidents
    - Retrieve incident details and resolution history
    - Query historical incidents for similar error patterns
    - Extract resolution notes for RAG knowledge base
    - Create and update incidents programmatically
    """

    def __init__(self, server_url: str, **kwargs: Any):
        super().__init__(server_url=server_url, **kwargs)
        self._last_poll_time: Optional[datetime] = None

    async def validate_connection(self) -> bool:
        """Validate connectivity to ICM MCP server."""
        try:
            result = await self.call_tool(
                tool_name="list_incidents",
                arguments={"limit": 1, "status": "active"},
            )
            return result is not None
        except Exception as e:
            logger.error("icm_validation_failed", error=str(e))
            return False

    async def get_incident(self, ticket_id: str) -> Optional[ICMTicket]:
        """
        Retrieve a single incident by ID.

        Args:
            ticket_id: ICM ticket identifier

        Returns:
            ICMTicket model or None
        """
        logger.info("icm_get_incident", ticket_id=ticket_id)

        result = await self.call_tool(
            tool_name="get_incident",
            arguments={"incident_id": ticket_id},
        )

        return self._parse_incident(result)

    async def list_active_incidents(
        self,
        severity: Optional[str] = None,
        team: Optional[str] = None,
        limit: int = 50,
    ) -> list[ICMTicket]:
        """
        List active (non-resolved) incidents.

        Args:
            severity: Filter by severity level
            team: Filter by owning team
            limit: Maximum results

        Returns:
            List of active ICMTicket objects
        """
        arguments: dict[str, Any] = {
            "status": "active",
            "limit": limit,
        }
        if severity:
            arguments["severity"] = severity
        if team:
            arguments["owning_team"] = team

        result = await self.call_tool(
            tool_name="list_incidents",
            arguments=arguments,
        )

        return self._parse_incident_list(result)

    async def poll_new_incidents(
        self,
        since: Optional[datetime] = None,
    ) -> list[ICMTicket]:
        """
        Poll for new or updated incidents since last check.

        Args:
            since: Timestamp to check from (defaults to last poll time)

        Returns:
            List of new/updated incidents
        """
        check_from = since or self._last_poll_time or (datetime.utcnow() - timedelta(minutes=5))

        logger.info("icm_polling", since=check_from.isoformat())

        result = await self.call_tool(
            tool_name="list_incidents",
            arguments={
                "updated_after": check_from.isoformat(),
                "limit": 100,
            },
        )

        self._last_poll_time = datetime.utcnow()
        incidents = self._parse_incident_list(result)

        if incidents:
            logger.info("icm_new_incidents_found", count=len(incidents))

        return incidents

    async def search_historical_incidents(
        self,
        query: str,
        days_back: int = 90,
        limit: int = 20,
    ) -> list[HistoricalIncident]:
        """
        Search historical incidents for similar patterns.
        Used for RAG similarity matching.

        Args:
            query: Search query (error message, keywords)
            days_back: How far back to search
            limit: Maximum results

        Returns:
            List of HistoricalIncident objects with resolution data
        """
        since = (datetime.utcnow() - timedelta(days=days_back)).isoformat()

        logger.info("icm_historical_search", query=query[:100], days_back=days_back)

        result = await self.call_tool(
            tool_name="search_incidents",
            arguments={
                "query": query,
                "created_after": since,
                "include_resolved": True,
                "limit": limit,
            },
        )

        return self._parse_historical_incidents(result)

    async def get_resolution_notes(
        self,
        ticket_id: str,
    ) -> Optional[str]:
        """
        Get resolution notes and post-mortem data for an incident.

        Args:
            ticket_id: ICM ticket identifier

        Returns:
            Resolution notes text or None
        """
        result = await self.call_tool(
            tool_name="get_incident_details",
            arguments={
                "incident_id": ticket_id,
                "include_timeline": True,
                "include_resolution": True,
            },
        )

        if isinstance(result, dict):
            return result.get("resolution_notes", "")
        if isinstance(result, str):
            return result
        return None

    async def get_incidents_for_indexing(
        self,
        days_back: int = 180,
        limit: int = 500,
    ) -> list[DocumentChunk]:
        """
        Retrieve resolved incidents for RAG vector store indexing.

        Args:
            days_back: How far back to index
            limit: Maximum incidents

        Returns:
            List of DocumentChunk objects ready for embedding
        """
        since = (datetime.utcnow() - timedelta(days=days_back)).isoformat()

        result = await self.call_tool(
            tool_name="search_incidents",
            arguments={
                "created_after": since,
                "status": "resolved",
                "include_resolved": True,
                "limit": limit,
            },
        )

        incidents = self._parse_historical_incidents(result)
        chunks = []

        for inc in incidents:
            content = (
                f"Incident: {inc.title}\n"
                f"Pipeline: {inc.pipeline_name}\n"
                f"Error Class: {inc.error_class}\n"
                f"Root Cause: {inc.root_cause}\n"
                f"Resolution: {inc.resolution}\n"
                f"Severity: {inc.severity.value}\n"
                f"Tags: {', '.join(inc.tags)}"
            )

            chunks.append(
                DocumentChunk(
                    chunk_id=f"icm_{inc.incident_id}",
                    source_type="icm",
                    source_id=inc.incident_id,
                    source_title=inc.title,
                    content=content,
                    metadata={
                        "pipeline": inc.pipeline_name,
                        "error_class": inc.error_class,
                        "severity": inc.severity.value,
                        "occurred_at": inc.occurred_at.isoformat(),
                    },
                )
            )

        logger.info("icm_indexed", total_chunks=len(chunks))
        return chunks

    async def update_incident(
        self,
        ticket_id: str,
        status: Optional[str] = None,
        notes: Optional[str] = None,
        assigned_to: Optional[str] = None,
    ) -> bool:
        """
        Update an incident's status, notes, or assignment.

        Returns:
            True if update succeeded
        """
        arguments: dict[str, Any] = {"incident_id": ticket_id}
        if status:
            arguments["status"] = status
        if notes:
            arguments["notes"] = notes
        if assigned_to:
            arguments["assigned_to"] = assigned_to

        try:
            await self.call_tool(tool_name="update_incident", arguments=arguments)
            logger.info("icm_incident_updated", ticket_id=ticket_id, status=status)
            return True
        except Exception as e:
            logger.error("icm_update_failed", ticket_id=ticket_id, error=str(e))
            return False

    # ─── Internal Parsers ─────────────────────────

    def _parse_incident(self, raw: Any) -> Optional[ICMTicket]:
        """Parse raw MCP response into ICMTicket."""
        if not raw:
            return None

        if isinstance(raw, str):
            # Text response - create minimal ticket
            return ICMTicket(
                ticket_id="unknown",
                title="Parsed from text",
                description=raw[:500],
                severity=Severity.MEDIUM,
                status=AlertStatus.NEW,
                created_at=datetime.utcnow(),
            )

        if isinstance(raw, dict):
            return ICMTicket(
                ticket_id=str(raw.get("id", raw.get("ticket_id", "unknown"))),
                title=raw.get("title", ""),
                description=raw.get("description", ""),
                severity=self._map_severity(raw.get("severity", "medium")),
                status=self._map_status(raw.get("status", "new")),
                created_at=self._parse_datetime(raw.get("created_at")),
                updated_at=self._parse_datetime(raw.get("updated_at")),
                assigned_to=raw.get("assigned_to"),
                owning_team=raw.get("owning_team"),
                tags=raw.get("tags", []),
                resolution_notes=raw.get("resolution_notes"),
                related_pipeline=raw.get("related_pipeline"),
                ttd_minutes=raw.get("ttd_minutes"),
                ttm_minutes=raw.get("ttm_minutes"),
                root_cause=raw.get("root_cause"),
            )

        return None

    def _parse_incident_list(self, raw: Any) -> list[ICMTicket]:
        """Parse a list of incidents."""
        if not raw:
            return []

        items = raw if isinstance(raw, list) else [raw]
        tickets = []
        for item in items:
            ticket = self._parse_incident(item)
            if ticket:
                tickets.append(ticket)
        return tickets

    def _parse_historical_incidents(self, raw: Any) -> list[HistoricalIncident]:
        """Parse raw data into HistoricalIncident models."""
        if not raw:
            return []

        items = raw if isinstance(raw, list) else [raw]
        incidents = []

        for item in items:
            if isinstance(item, dict):
                try:
                    incidents.append(
                        HistoricalIncident(
                            incident_id=str(item.get("id", item.get("ticket_id", ""))),
                            title=item.get("title", ""),
                            description=item.get("description", ""),
                            root_cause=item.get("root_cause", "Unknown"),
                            resolution=item.get("resolution", item.get("resolution_notes", "No resolution recorded")),
                            pipeline_name=item.get("related_pipeline", item.get("pipeline", "unknown")),
                            error_class=item.get("error_class", "Unknown"),
                            severity=self._map_severity(item.get("severity", "medium")),
                            occurred_at=self._parse_datetime(item.get("created_at")),
                            resolved_at=self._parse_datetime(item.get("resolved_at")),
                            tags=item.get("tags", []),
                        )
                    )
                except Exception as e:
                    logger.warning("historical_parse_error", error=str(e))

        return incidents

    @staticmethod
    def _map_severity(value: str) -> Severity:
        """Map various severity strings to Severity enum."""
        mapping = {
            "1": Severity.CRITICAL, "critical": Severity.CRITICAL, "sev1": Severity.CRITICAL,
            "2": Severity.HIGH, "high": Severity.HIGH, "sev2": Severity.HIGH,
            "3": Severity.MEDIUM, "medium": Severity.MEDIUM, "sev3": Severity.MEDIUM,
            "4": Severity.LOW, "low": Severity.LOW, "sev4": Severity.LOW,
        }
        return mapping.get(str(value).lower(), Severity.MEDIUM)

    @staticmethod
    def _map_status(value: str) -> AlertStatus:
        """Map various status strings to AlertStatus enum."""
        mapping = {
            "new": AlertStatus.NEW, "active": AlertStatus.NEW,
            "acknowledged": AlertStatus.ACKNOWLEDGED, "acked": AlertStatus.ACKNOWLEDGED,
            "investigating": AlertStatus.INVESTIGATING,
            "mitigated": AlertStatus.MITIGATED,
            "resolved": AlertStatus.RESOLVED,
            "closed": AlertStatus.CLOSED,
        }
        return mapping.get(str(value).lower(), AlertStatus.NEW)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        """Parse various datetime formats safely."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        return datetime.utcnow()
