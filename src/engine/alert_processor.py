"""
Alert Processor — Core engine for real-time failure detection and analysis.

Orchestrates the full alert processing pipeline:
1. Ingest raw logs / ICM tickets
2. Parse and classify failures
3. Run root cause analysis via LLM
4. Retrieve similar incidents via RAG
5. Route to the correct team
6. Generate notifications
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog

from src.connectors import (
    ConfluenceMCPClient,
    ICMMCPClient,
    KustoMCPClient,
    LogAnalyticsMCPClient,
)
from src.models import (
    AlertFeedItem,
    AlertSource,
    AlertStatus,
    AnalysisResult,
    HistoricalIncident,
    ICMTicket,
    ParsedFailure,
    PipelineOwnership,
    PipelineType,
    RawLogEntry,
    RoutingDecision,
    Severity,
)
from src.prompts.prompt_engine import PromptEngine
from src.rag.retriever import RAGRetriever

logger = structlog.get_logger(__name__)


class AlertProcessor:
    """
    Central alert processing engine.

    Coordinates all subsystems to provide end-to-end alert handling:
    - Detection → Classification → Analysis → Routing → Notification
    """

    def __init__(
        self,
        prompt_engine: PromptEngine,
        rag_retriever: RAGRetriever,
        kusto_client: Optional[KustoMCPClient] = None,
        confluence_client: Optional[ConfluenceMCPClient] = None,
        icm_client: Optional[ICMMCPClient] = None,
        log_analytics_client: Optional[LogAnalyticsMCPClient] = None,
        ownership_file: str = "config/pipeline_ownership.json",
    ):
        self.prompt_engine = prompt_engine
        self.rag_retriever = rag_retriever
        self.kusto = kusto_client
        self.confluence = confluence_client
        self.icm = icm_client
        self.log_analytics = log_analytics_client

        # Load pipeline ownership
        self.ownership_map = self._load_ownership(ownership_file)
        self._processed_alerts: dict[str, AnalysisResult] = {}

    async def process_alert(
        self,
        raw_entry: Optional[RawLogEntry] = None,
        icm_ticket: Optional[ICMTicket] = None,
    ) -> AnalysisResult:
        """
        Process a single alert through the full pipeline.

        Accepts either a raw log entry or an ICM ticket as input.
        Returns a complete AnalysisResult with root cause, routing, and recommendations.
        """
        failure_id = str(uuid.uuid4())[:12]
        logger.info("alert_processing_started", failure_id=failure_id)

        # Step 1: Parse into structured failure
        failure = self._create_failure(failure_id, raw_entry, icm_ticket)
        logger.info("failure_parsed", failure_id=failure_id, pipeline=failure.pipeline_name)

        # Step 2: Run root cause analysis
        rag_result = await self.rag_retriever.retrieve_for_alert(failure)
        rag_context = await self.rag_retriever.build_context_string(rag_result.chunks)

        messages = self.prompt_engine.build_root_cause_prompt(
            failure=failure,
            rag_context=rag_context,
            technique="auto",
        )
        rca_response = await self.prompt_engine.invoke_llm(messages)
        rca_output = self.prompt_engine.parse_root_cause_response(rca_response)

        # Update failure with LLM analysis
        failure.root_cause_summary = rca_output.root_cause_summary
        failure.error_class = rca_output.error_classification
        failure.affected_components = rca_output.affected_components

        logger.info(
            "rca_complete",
            failure_id=failure_id,
            error_class=rca_output.error_classification,
            confidence=rca_output.confidence,
        )

        # Step 3: Find similar incidents
        similar_incidents = await self.rag_retriever.find_similar_incidents(failure)

        # Step 4: Get runbook context
        runbook_steps = await self._fetch_runbook_steps(failure)

        # Step 5: Route the alert
        routing = await self._route_alert(failure)

        # Step 6: Build final analysis
        result = AnalysisResult(
            failure=failure,
            root_cause_analysis=rca_response,
            similar_incidents=similar_incidents,
            recommended_actions=rca_output.recommended_actions or self._default_actions(failure),
            runbook_steps=runbook_steps,
            routing=routing,
            confidence=rca_output.confidence,
            reasoning_trace=[
                f"Technique: {self.prompt_engine._select_technique(failure, rag_context)}",
                f"RAG chunks used: {len(rag_result.chunks)}",
                f"Similar incidents found: {len(similar_incidents)}",
                f"Topic classification: completed",
            ],
        )

        self._processed_alerts[failure_id] = result
        logger.info("alert_processing_complete", failure_id=failure_id)

        return result

    async def detect_new_failures(
        self,
        timespan: str = "1h",
    ) -> list[RawLogEntry]:
        """
        Poll all sources for new failures.
        Aggregates errors from Spark, Synapse, and Kusto.
        """
        all_entries: list[RawLogEntry] = []

        # Poll Log Analytics for Spark/Synapse errors
        if self.log_analytics:
            try:
                spark_errors = await self.log_analytics.get_spark_driver_errors(timespan=timespan)
                all_entries.extend(spark_errors)
                logger.info("spark_errors_detected", count=len(spark_errors))
            except Exception as e:
                logger.error("spark_detection_failed", error=str(e))

            try:
                synapse_errors = await self.log_analytics.get_synapse_pipeline_failures(timespan=timespan)
                all_entries.extend(synapse_errors)
                logger.info("synapse_errors_detected", count=len(synapse_errors))
            except Exception as e:
                logger.error("synapse_detection_failed", error=str(e))

        # Poll Kusto for ingestion failures
        if self.kusto:
            try:
                kusto_errors = await self.kusto.get_ingestion_failures(timespan=timespan)
                all_entries.extend(kusto_errors)
                logger.info("kusto_errors_detected", count=len(kusto_errors))
            except Exception as e:
                logger.error("kusto_detection_failed", error=str(e))

        return all_entries

    async def detect_new_icm_incidents(self) -> list[ICMTicket]:
        """Poll ICM for new/updated incidents."""
        if not self.icm:
            return []

        try:
            return await self.icm.poll_new_incidents()
        except Exception as e:
            logger.error("icm_poll_failed", error=str(e))
            return []

    async def get_alert_feed(self, limit: int = 20) -> list[AlertFeedItem]:
        """Get the recent alert feed for the dashboard."""
        items = []
        for fid, result in list(self._processed_alerts.items())[-limit:]:
            items.append(
                AlertFeedItem(
                    failure=result.failure,
                    routing=result.routing,
                    similar_count=len(result.similar_incidents),
                    has_runbook=bool(result.runbook_steps and result.runbook_steps[0] != "No runbook found."),
                )
            )
        items.reverse()  # Most recent first
        return items

    def get_processed_alert(self, failure_id: str) -> Optional[AnalysisResult]:
        """Retrieve a previously processed alert by ID."""
        return self._processed_alerts.get(failure_id)

    # ─── Alert Routing ─────────────────────────

    async def _route_alert(self, failure: ParsedFailure) -> RoutingDecision:
        """
        Route an alert to the correct team/channel.
        Uses ownership mapping with LLM fallback for unknown pipelines.
        """
        # Try direct ownership lookup first
        ownership = self.ownership_map.get(failure.pipeline_name)

        if ownership:
            return RoutingDecision(
                failure_id=failure.failure_id,
                target_channel=ownership.oncall_channel,
                target_contacts=ownership.escalation_contacts,
                severity=failure.severity,
                auto_escalate=failure.severity in (Severity.CRITICAL, Severity.HIGH),
                routing_reason=f"Direct ownership match: {ownership.owner} owns {failure.pipeline_name}",
                confidence=0.95,
            )

        # LLM-based routing for unknown pipelines
        ownership_json = json.dumps(
            {name: {"owner": o.owner, "tags": o.tags, "channel": o.oncall_channel}
             for name, o in self.ownership_map.items()},
            indent=2,
        )

        messages = self.prompt_engine.build_routing_prompt(failure, ownership_json)
        response = await self.prompt_engine.invoke_llm(messages, response_format="json")
        routing_output = self.prompt_engine.parse_routing_response(response)

        if routing_output:
            return RoutingDecision(
                failure_id=failure.failure_id,
                target_channel=routing_output.target_channel,
                target_contacts=routing_output.target_contacts,
                severity=Severity(routing_output.severity) if routing_output.severity in [s.value for s in Severity] else failure.severity,
                auto_escalate=routing_output.auto_escalate,
                routing_reason=routing_output.routing_reason,
                confidence=routing_output.confidence,
            )

        # Fallback routing
        return RoutingDecision(
            failure_id=failure.failure_id,
            target_channel="platform-oncall",
            target_contacts=["platform-lead@company.com"],
            severity=failure.severity,
            auto_escalate=failure.severity == Severity.CRITICAL,
            routing_reason="Fallback routing — pipeline ownership not found",
            confidence=0.3,
        )

    # ─── Runbook Retrieval ─────────────────────

    async def _fetch_runbook_steps(self, failure: ParsedFailure) -> list[str]:
        """Fetch runbook steps from Confluence if available."""
        if not self.confluence:
            return ["Confluence not configured — no runbook available."]

        # Check if pipeline has a mapped runbook
        ownership = self.ownership_map.get(failure.pipeline_name)
        if ownership and ownership.runbook_confluence_id:
            try:
                runbook = await self.confluence.get_runbook(ownership.runbook_confluence_id)
                if runbook and runbook.get("steps"):
                    failure.runbook_url = runbook.get("url", "")
                    return runbook["steps"]
            except Exception as e:
                logger.warning("runbook_fetch_failed", error=str(e))

        # Search for relevant runbooks
        try:
            results = await self.confluence.search_runbooks(
                error_context=failure.error_message,
                pipeline_name=failure.pipeline_name,
                limit=3,
            )
            if results:
                all_steps = []
                for r in results:
                    steps = r.get("steps", [])
                    if steps:
                        all_steps.extend(steps[:5])
                return all_steps if all_steps else ["No relevant runbook steps found."]
        except Exception as e:
            logger.warning("runbook_search_failed", error=str(e))

        return ["No runbook found."]

    # ─── Failure Construction ──────────────────

    def _create_failure(
        self,
        failure_id: str,
        raw_entry: Optional[RawLogEntry],
        icm_ticket: Optional[ICMTicket],
    ) -> ParsedFailure:
        """Create a ParsedFailure from either a raw log entry or ICM ticket."""
        if icm_ticket:
            return ParsedFailure(
                failure_id=failure_id,
                pipeline_name=icm_ticket.related_pipeline or "unknown",
                pipeline_type=self._infer_pipeline_type(icm_ticket.related_pipeline or ""),
                source=AlertSource.ICM,
                timestamp=icm_ticket.created_at,
                severity=icm_ticket.severity,
                error_class=icm_ticket.root_cause or "Unknown",
                error_message=icm_ticket.description,
                root_cause_summary=icm_ticket.root_cause or "Pending analysis",
                log_snippet=icm_ticket.description[:2000],
                affected_components=icm_ticket.tags,
            )

        if raw_entry:
            return ParsedFailure(
                failure_id=failure_id,
                pipeline_name=raw_entry.pipeline_name or "unknown",
                pipeline_type=self._infer_pipeline_type(raw_entry.pipeline_name or ""),
                source=raw_entry.source,
                timestamp=raw_entry.timestamp,
                severity=self._infer_severity(raw_entry),
                error_class=raw_entry.metadata.get("exception_class", "Unknown"),
                error_message=raw_entry.message,
                root_cause_summary="Pending analysis",
                log_snippet=raw_entry.message[:2000],
                job_id=raw_entry.job_id,
                cluster_id=raw_entry.cluster_id,
            )

        # Empty failure for manual entry
        return ParsedFailure(
            failure_id=failure_id,
            pipeline_name="manual_entry",
            pipeline_type=PipelineType.SPARK_BATCH,
            source=AlertSource.MANUAL,
            timestamp=datetime.utcnow(),
            severity=Severity.MEDIUM,
            error_class="Unknown",
            error_message="Manually entered alert",
            root_cause_summary="Pending analysis",
        )

    @staticmethod
    def _infer_pipeline_type(pipeline_name: str) -> PipelineType:
        """Infer pipeline type from name."""
        name_lower = pipeline_name.lower()
        if "synapse" in name_lower or "pipeline" in name_lower:
            return PipelineType.SYNAPSE_PIPELINE
        if "kusto" in name_lower or "adx" in name_lower:
            if "ingestion" in name_lower:
                return PipelineType.KUSTO_INGESTION
            return PipelineType.KUSTO_QUERY
        if "streaming" in name_lower or "stream" in name_lower:
            return PipelineType.SPARK_STREAMING
        return PipelineType.SPARK_BATCH

    @staticmethod
    def _infer_severity(entry: RawLogEntry) -> Severity:
        """Infer initial severity from log entry."""
        msg = entry.message.lower()
        if any(kw in msg for kw in ["fatal", "critical", "data loss", "corruption"]):
            return Severity.CRITICAL
        if any(kw in msg for kw in ["outofmemory", "oom", "timeout exceeded"]):
            return Severity.HIGH
        if entry.level == "FATAL":
            return Severity.CRITICAL
        if entry.level == "ERROR":
            return Severity.HIGH
        return Severity.MEDIUM

    def _default_actions(self, failure: ParsedFailure) -> list[str]:
        """Generate default recommended actions."""
        actions = []
        if failure.rerun_url:
            actions.append(f"Rerun the pipeline: {failure.rerun_url}")
        actions.extend([
            f"Check logs for pipeline '{failure.pipeline_name}'",
            "Verify upstream data sources are healthy",
            "Check cluster/pool resource utilization",
            "Review recent code or configuration changes",
        ])
        return actions

    # ─── Ownership Loading ────────────────────

    @staticmethod
    def _load_ownership(filepath: str) -> dict[str, PipelineOwnership]:
        """Load pipeline ownership mapping from JSON file."""
        path = Path(filepath)
        if not path.exists():
            logger.warning("ownership_file_not_found", path=filepath)
            return {}

        try:
            with open(path) as f:
                data = json.load(f)

            ownership = {}
            for name, config in data.get("pipelines", {}).items():
                ownership[name] = PipelineOwnership(
                    pipeline_name=name,
                    owner=config.get("owner", "unknown"),
                    oncall_channel=config.get("oncall_channel", "platform-oncall"),
                    escalation_contacts=config.get("escalation_contacts", []),
                    severity_default=Severity(config.get("severity_default", "medium")),
                    tags=config.get("tags", []),
                    runbook_confluence_id=config.get("runbook_confluence_id"),
                )

            logger.info("ownership_loaded", pipeline_count=len(ownership))
            return ownership

        except Exception as e:
            logger.error("ownership_load_failed", error=str(e))
            return {}
