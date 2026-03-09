"""
Alert Triage Engine — Aggregation layer between AlertProcessor and Chat UI.

Solves the "MCP Data Volume" problem: raw alert streams produce 100-350 alerts/day.
Without aggregation, the sidebar is unusable and engineers can't find what matters.

The triage engine provides:
- Alert clustering (cascade/OOM → downstream blocked → stale data chains)
- Composite severity scoring (not just individual alert severity)
- Business impact ranking via GraphRAG blast radius
- Auto-assignment to on-call based on pipeline ownership
- Proactive surfacing of top 3-5 critical clusters

Why this matters for Spark/Synapse/Kusto troubleshooting:
- A single Spark OOM produces 5-15 alerts: executor failures, driver abort,
  orchestrator timeout, downstream stale data warnings, dashboard SLA breach
- Without clustering, the support engineer sees 15 separate alerts
- With triage, they see "1 cluster: OOM cascade affecting 4 pipelines (Sev: Critical)"
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from src.engine.alert_dedup import AlertDeduplicator, AlertGroup, CascadeCluster
from src.models import ParsedFailure, Severity

logger = structlog.get_logger(__name__)


def _ts_gte(ts: Optional[datetime], cutoff: datetime) -> bool:
    """Safe timestamp >= cutoff comparison handling naive vs aware datetimes."""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return ts >= cutoff


# ─────────────────────────────────────────────────
# Triage Configuration
# ─────────────────────────────────────────────────

SEVERITY_WEIGHT = {
    Severity.CRITICAL: 100,
    Severity.HIGH: 60,
    Severity.MEDIUM: 30,
    Severity.LOW: 10,
}

# Error patterns that indicate cascading failures
CASCADE_SIGNATURES = {
    "OutOfMemoryError": ["downstream_blocked", "stale_data", "sla_breach"],
    "TimeoutException": ["downstream_timeout", "retry_exhaustion"],
    "Permanent_MappingNotFound": ["ingestion_drop", "data_gap", "dashboard_stale"],
    "StreamingQueryException": ["stream_terminated", "backpressure"],
    "UserErrorInvalidFolderPath": ["data_not_landed", "dependency_wait"],
    "SqlPoolPaused": ["dw_unavailable"],
}


class TriagedCluster:
    """A triaged cluster of related alerts with composite scoring."""

    def __init__(
        self,
        cluster_id: str,
        primary_alert: ParsedFailure,
        cascade: Optional[CascadeCluster] = None,
    ):
        self.cluster_id = cluster_id
        self.primary_alert = primary_alert
        self.all_alerts: list[ParsedFailure] = [primary_alert]
        self.cascade = cascade

        # Scoring
        self.composite_severity: Severity = primary_alert.severity
        self.business_impact_score: float = 0.0
        self.blast_radius_pipelines: list[str] = []
        self.blast_radius_components: list[str] = []

        # Assignment
        self.assigned_to: Optional[str] = None
        self.assigned_channel: Optional[str] = None

        # Timestamps
        self.first_seen: datetime = primary_alert.timestamp
        self.last_updated: datetime = primary_alert.timestamp

    def add_alert(self, alert: ParsedFailure) -> None:
        """Add an alert to this triaged cluster."""
        self.all_alerts.append(alert)
        if alert.timestamp > self.last_updated:
            self.last_updated = alert.timestamp
        if alert.timestamp < self.first_seen:
            self.first_seen = alert.timestamp
        self._recompute_severity()

    @property
    def alert_count(self) -> int:
        return len(self.all_alerts)

    @property
    def is_cascade(self) -> bool:
        return self.cascade is not None and len(self.cascade.chain) > 1

    @property
    def severity_rank(self) -> int:
        """Numeric rank for sorting (higher = more critical)."""
        base = SEVERITY_WEIGHT.get(self.composite_severity, 0)
        # Bonus for cascades
        cascade_bonus = 20 if self.is_cascade else 0
        # Bonus for blast radius
        blast_bonus = min(len(self.blast_radius_pipelines) * 5, 30)
        # Bonus for volume
        volume_bonus = min(self.alert_count * 2, 20)
        return base + cascade_bonus + blast_bonus + volume_bonus

    def _recompute_severity(self) -> None:
        """Upgrade composite severity based on cluster composition."""
        severity_order = {
            Severity.LOW: 0,
            Severity.MEDIUM: 1,
            Severity.HIGH: 2,
            Severity.CRITICAL: 3,
        }
        max_sev = max(
            (severity_order.get(a.severity, 0) for a in self.all_alerts),
            default=0,
        )
        # Escalate if cascade or high volume
        if self.alert_count >= 5 and max_sev < 3:
            max_sev = min(max_sev + 1, 3)
        if self.is_cascade and max_sev < 3:
            max_sev = min(max_sev + 1, 3)

        reverse_map = {v: k for k, v in severity_order.items()}
        self.composite_severity = reverse_map.get(max_sev, Severity.MEDIUM)

    def summary(self) -> str:
        """Human-readable cluster summary for proactive surfacing."""
        parts = [f"**{self.primary_alert.pipeline_name}** — {self.primary_alert.error_class}"]
        parts.append(f"Severity: {self.composite_severity.value.upper()}")
        parts.append(f"Alerts: {self.alert_count}")
        if self.is_cascade:
            chain_names = [step.pipeline_name for step in self.cascade.chain]
            parts.append(f"Cascade: {' → '.join(chain_names)}")
        if self.blast_radius_pipelines:
            parts.append(f"Blast radius: {len(self.blast_radius_pipelines)} pipelines affected")
        return " | ".join(parts)

    def detailed_summary(self) -> str:
        """Multi-line detailed summary for chat responses."""
        lines = [
            f"**Cluster: {self.primary_alert.pipeline_name}**",
            f"  - Error: {self.primary_alert.error_class}",
            f"  - Severity: {self.composite_severity.value.upper()}",
            f"  - Alerts in cluster: {self.alert_count}",
            f"  - First seen: {self.first_seen.strftime('%H:%M UTC')}",
        ]
        if self.is_cascade:
            chain_names = [step.pipeline_name for step in self.cascade.chain]
            lines.append(f"  - Cascade chain: {' → '.join(chain_names)}")
        if self.blast_radius_pipelines:
            lines.append(f"  - Affected pipelines: {', '.join(self.blast_radius_pipelines[:5])}")
        if self.blast_radius_components:
            lines.append(f"  - Affected components: {', '.join(self.blast_radius_components[:5])}")
        if self.assigned_channel:
            lines.append(f"  - Assigned to: #{self.assigned_channel}")
        return "\n".join(lines)


class AlertTriageEngine:
    """
    Central triage engine — sits between AlertProcessor output and the Chat UI.

    Responsibilities:
    1. Cluster related alerts (exact dedup + cascade detection)
    2. Score clusters by business impact
    3. Rank and prioritize for the support team
    4. Provide search/filter/discovery APIs for the Chat module
    5. Surface top critical clusters proactively
    """

    def __init__(
        self,
        deduplicator: AlertDeduplicator,
        graph_rag: Any = None,  # GraphRAGEngine instance if available
        ownership_map: Optional[dict] = None,
    ):
        self.deduplicator = deduplicator
        self.graph_rag = graph_rag
        self.ownership_map = ownership_map or {}
        self._triaged_clusters: dict[str, TriagedCluster] = {}
        self._all_alerts: list[ParsedFailure] = []
        self._pipeline_health: dict[str, dict[str, Any]] = {}

    def ingest_alert(self, alert: ParsedFailure) -> TriagedCluster:
        """
        Ingest a single alert through the triage pipeline.

        1. Deduplicate via AlertDeduplicator (gets group + cascade info)
        2. Map to a TriagedCluster
        3. Score and rank
        4. Update pipeline health state
        """
        self._all_alerts.append(alert)

        # Step 1: Deduplicate and detect cascades
        group = self.deduplicator.process_alert(alert)
        cascade = self.deduplicator.detect_cascade(alert)

        # Step 2: Map to triaged cluster
        cluster_id = group.group_id
        if cascade and cascade.root_cause_alert:
            # Use cascade root as the cluster anchor
            root_key = f"cascade-{cascade.root_cause_alert.failure_id}"
            cluster_id = root_key

        if cluster_id in self._triaged_clusters:
            cluster = self._triaged_clusters[cluster_id]
            cluster.add_alert(alert)
            if cascade and not cluster.cascade:
                cluster.cascade = cascade
        else:
            cluster = TriagedCluster(
                cluster_id=cluster_id,
                primary_alert=group.primary_alert,
                cascade=cascade,
            )
            # Add all alerts from the dedup group
            for related in group.related_alerts:
                if related.failure_id != group.primary_alert.failure_id:
                    cluster.add_alert(related)
            self._triaged_clusters[cluster_id] = cluster

        # Step 3: Compute blast radius if GraphRAG is available
        self._compute_blast_radius(cluster)

        # Step 4: Auto-assign based on ownership
        self._auto_assign(cluster)

        # Step 5: Update pipeline health
        self._update_health(alert)

        logger.info(
            "alert_triaged",
            cluster_id=cluster_id,
            alert_count=cluster.alert_count,
            severity=cluster.composite_severity.value,
            is_cascade=cluster.is_cascade,
        )
        return cluster

    def ingest_alerts(self, alerts: list[ParsedFailure]) -> list[TriagedCluster]:
        """Batch ingest alerts and return all affected clusters."""
        seen_clusters: dict[str, TriagedCluster] = {}
        for alert in alerts:
            cluster = self.ingest_alert(alert)
            seen_clusters[cluster.cluster_id] = cluster
        return list(seen_clusters.values())

    # ─── Query APIs (used by ChatEngine) ─────────

    def get_top_clusters(self, limit: int = 5) -> list[TriagedCluster]:
        """Get top N clusters ranked by severity + business impact."""
        clusters = list(self._triaged_clusters.values())
        clusters.sort(key=lambda c: c.severity_rank, reverse=True)
        return clusters[:limit]

    def get_critical_clusters(self) -> list[TriagedCluster]:
        """Get all clusters with composite severity CRITICAL or HIGH."""
        return [
            c for c in self._triaged_clusters.values()
            if c.composite_severity in (Severity.CRITICAL, Severity.HIGH)
        ]

    def search_alerts(
        self,
        query: str,
        severity_filter: Optional[list[Severity]] = None,
        pipeline_filter: Optional[str] = None,
        hours_back: int = 24,
    ) -> list[ParsedFailure]:
        """
        Search alerts by keyword, severity, and pipeline.
        Enables the ALERT_SEARCH intent in ChatEngine.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        results = []

        query_lower = query.lower()
        for alert in self._all_alerts:
            # Handle both naive and aware timestamps from different data sources
            alert_ts = alert.timestamp
            if alert_ts is not None:
                if alert_ts.tzinfo is None:
                    alert_ts = alert_ts.replace(tzinfo=timezone.utc)
                if alert_ts < cutoff:
                    continue
            else:
                continue
            if severity_filter and alert.severity not in severity_filter:
                continue
            if pipeline_filter and pipeline_filter.lower() not in alert.pipeline_name.lower():
                continue

            # Keyword match across multiple fields
            searchable = " ".join([
                alert.pipeline_name,
                alert.error_class,
                alert.error_message,
                alert.root_cause_summary,
            ]).lower()

            if query_lower in searchable:
                results.append(alert)

        return results

    def get_alerts_by_severity(
        self, severity: Severity, hours_back: int = 24
    ) -> list[ParsedFailure]:
        """Get all alerts at a specific severity level."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        results = []
        for a in self._all_alerts:
            if a.severity != severity:
                continue
            ts = a.timestamp
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                results.append(a)
        return results

    def get_alerts_by_pipeline(
        self, pipeline_name: str, hours_back: int = 24
    ) -> list[ParsedFailure]:
        """Get all alerts for a specific pipeline."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        name_lower = pipeline_name.lower()
        results = []
        for a in self._all_alerts:
            if name_lower not in a.pipeline_name.lower():
                continue
            ts = a.timestamp
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                results.append(a)
        return results

    def get_pipeline_health(self, pipeline_name: Optional[str] = None) -> dict[str, Any]:
        """
        Get pipeline health status.
        If pipeline_name is given, return specific health; otherwise return overall.
        """
        if pipeline_name:
            return self._pipeline_health.get(pipeline_name, {
                "status": "unknown",
                "last_alert": None,
                "alert_count_24h": 0,
                "latest_error": None,
            })

        # Overall platform health
        total = len(self._all_alerts)
        now = datetime.now(timezone.utc)
        recent_cutoff = now - timedelta(hours=1)
        recent = [a for a in self._all_alerts if _ts_gte(a.timestamp, recent_cutoff)]

        critical_count = sum(1 for a in recent if a.severity == Severity.CRITICAL)
        high_count = sum(1 for a in recent if a.severity == Severity.HIGH)

        if critical_count > 0:
            status = "critical"
        elif high_count > 0:
            status = "degraded"
        elif len(recent) > 10:
            status = "elevated"
        else:
            status = "healthy"

        # Unique pipelines affected
        affected_pipelines = list({a.pipeline_name for a in recent})

        return {
            "status": status,
            "total_alerts_24h": total,
            "alerts_last_hour": len(recent),
            "critical_count": critical_count,
            "high_count": high_count,
            "active_clusters": len(self._triaged_clusters),
            "affected_pipelines": affected_pipelines,
            "pipeline_details": dict(self._pipeline_health),
        }

    def get_cross_alert_analytics(self, hours_back: int = 24) -> dict[str, Any]:
        """
        Cross-alert analytics: patterns, recurring errors, severity trends.
        Enables the CROSS_ALERT_ANALYTICS intent.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        recent = [a for a in self._all_alerts if _ts_gte(a.timestamp, cutoff)]

        if not recent:
            return {"summary": "No alerts in the selected time window.", "patterns": []}

        # Error class distribution
        error_counts: dict[str, int] = defaultdict(int)
        pipeline_counts: dict[str, int] = defaultdict(int)
        severity_counts: dict[str, int] = defaultdict(int)
        for a in recent:
            error_counts[a.error_class] += 1
            pipeline_counts[a.pipeline_name] += 1
            severity_counts[a.severity.value] += 1

        # Top error classes
        top_errors = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_pipelines = sorted(pipeline_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        # Detect patterns
        patterns = []
        for error, count in top_errors:
            if count >= 3:
                patterns.append(f"Recurring: {error} appeared {count} times in the last {hours_back}h")

        # Active cascades
        cascade_count = sum(1 for c in self._triaged_clusters.values() if c.is_cascade)
        if cascade_count:
            patterns.append(f"{cascade_count} active cascade failure chain(s) detected")

        return {
            "total_alerts": len(recent),
            "severity_distribution": dict(severity_counts),
            "top_errors": top_errors,
            "top_pipelines": top_pipelines,
            "active_cascades": cascade_count,
            "patterns": patterns,
            "cluster_count": len(self._triaged_clusters),
        }

    def build_proactive_greeting(self) -> str:
        """
        Build a proactive greeting with top critical clusters.
        Used when user enters chat without selecting an alert.
        """
        top_clusters = self.get_top_clusters(limit=5)
        health = self.get_pipeline_health()

        if not top_clusters:
            return (
                "I'm Alert Whisperer — your pipeline intelligence assistant.\n\n"
                "**Platform Status:** All systems appear healthy. No active alerts.\n\n"
                "You can ask me:\n"
                "- \"What's the platform health?\"\n"
                "- \"Show me all critical alerts\"\n"
                "- \"Is spark_etl_daily_ingest healthy?\"\n"
                "- \"What failed in the last hour?\"\n"
                "- Or select an alert from the sidebar for detailed investigation."
            )

        status_icon = {
            "critical": "🔴",
            "degraded": "🟠",
            "elevated": "🟡",
            "healthy": "🟢",
        }.get(health.get("status", "healthy"), "⚪")

        lines = [
            "I'm Alert Whisperer — your pipeline intelligence assistant.\n",
            f"**Platform Status:** {status_icon} {health.get('status', 'unknown').upper()} "
            f"— {health.get('alerts_last_hour', 0)} alerts in the last hour, "
            f"{health.get('active_clusters', 0)} active clusters\n",
        ]

        # Top critical clusters
        critical = [c for c in top_clusters if c.composite_severity in (Severity.CRITICAL, Severity.HIGH)]
        if critical:
            lines.append(f"**Top {len(critical)} Issues Requiring Attention:**\n")
            for i, cluster in enumerate(critical[:3], 1):
                cascade_tag = " ⛓️ CASCADE" if cluster.is_cascade else ""
                lines.append(
                    f"{i}. **{cluster.primary_alert.pipeline_name}** — "
                    f"{cluster.primary_alert.error_class} "
                    f"({cluster.composite_severity.value.upper()}{cascade_tag}, "
                    f"{cluster.alert_count} alerts)"
                )
                if cluster.blast_radius_pipelines:
                    lines.append(
                        f"   Blast radius: {', '.join(cluster.blast_radius_pipelines[:3])}"
                    )
            lines.append("")

        lines.append(
            "You can ask me about any of these, or try:\n"
            "- \"What's broken right now?\"\n"
            "- \"Show me critical alerts\"\n"
            "- \"Is [pipeline_name] healthy?\"\n"
            "- \"Compare yesterday vs today\"\n"
            "- Or select an alert from the sidebar for detailed investigation."
        )

        return "\n".join(lines)

    # ─── Internal ─────────────────────────────────

    def _compute_blast_radius(self, cluster: TriagedCluster) -> None:
        """Compute blast radius using GraphRAG if available."""
        if not self.graph_rag:
            # Fallback: use affected_components from the primary alert
            cluster.blast_radius_components = list(
                cluster.primary_alert.affected_components
            )
            # Remove self from blast radius
            cluster.blast_radius_pipelines = [
                c for c in cluster.blast_radius_components
                if c != cluster.primary_alert.pipeline_name
            ]
            return

        try:
            blast = self.graph_rag.trace_blast_radius(
                cluster.primary_alert.pipeline_name, depth=2
            )
            cluster.blast_radius_pipelines = blast.get("affected_pipelines", [])
            cluster.blast_radius_components = blast.get("affected_components", [])
            cluster.business_impact_score = float(
                blast.get("impact_score", len(cluster.blast_radius_pipelines) * 10)
            )
        except Exception as e:
            logger.warning("blast_radius_failed", error=str(e))

    def _auto_assign(self, cluster: TriagedCluster) -> None:
        """Auto-assign cluster to on-call channel based on ownership."""
        pipeline = cluster.primary_alert.pipeline_name
        ownership = self.ownership_map.get(pipeline)
        if ownership:
            cluster.assigned_channel = getattr(ownership, "oncall_channel", None)
            cluster.assigned_to = getattr(ownership, "owner", None)

    def _update_health(self, alert: ParsedFailure) -> None:
        """Update per-pipeline health tracking."""
        name = alert.pipeline_name
        if name not in self._pipeline_health:
            self._pipeline_health[name] = {
                "status": "healthy",
                "last_alert": None,
                "alert_count_24h": 0,
                "latest_error": None,
                "alerts": [],
            }
        health = self._pipeline_health[name]
        health["last_alert"] = alert.timestamp
        health["alert_count_24h"] = health.get("alert_count_24h", 0) + 1
        health["latest_error"] = alert.error_class
        health["alerts"] = health.get("alerts", [])
        health["alerts"].append(alert)

        # Determine status
        if alert.severity == Severity.CRITICAL:
            health["status"] = "critical"
        elif alert.severity == Severity.HIGH:
            health["status"] = "degraded"
        elif health.get("alert_count_24h", 0) >= 5:
            health["status"] = "elevated"
        else:
            health["status"] = "warning"
