"""
Alert Deduplication Engine.

Groups related alerts to reduce noise for the support team.
Uses a combination of:
- Pipeline name + error class matching (exact dedup)
- Time-window grouping (alerts within N minutes are likely related)
- Error message similarity (fuzzy dedup for variant messages)
- Cascade detection (OOM → downstream blocked → stale data chains)
- Cluster-level grouping across related pipelines

Why deduplication for Spark/Synapse/Kusto troubleshooting:
- A single Spark OOM failure can generate 10+ alerts: one per executor,
  one from the driver, one from the pipeline orchestrator, and one from
  the downstream monitoring — dedup collapses these into one investigation
- Kusto ingestion retries produce multiple near-identical alerts that
  differ only by timestamp and batch ID
- Synapse pipeline failures cascade: the activity fails, then the pipeline
  fails, then the trigger fails — all for the same root cause
- Flaky tests and intermittent failures produce recurring alerts that
  should be grouped as "ongoing issue" rather than N separate incidents

Enhanced with:
- CascadeCluster: detects chains of causally-related failures across pipelines
- Cluster-level grouping: groups alerts that share a dependency graph neighborhood
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from src.models import ParsedFailure, Severity

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────
# Known Cascade Patterns
# ─────────────────────────────────────────────────

# Maps an error class to the downstream error patterns it typically causes.
# Used by detect_cascade() to chain causally-related failures.
KNOWN_CASCADE_PATTERNS: dict[str, list[str]] = {
    "OutOfMemoryError": [
        "downstream_blocked",
        "stale_data",
        "TaskKilledException",
        "ExecutorLostFailure",
    ],
    "TimeoutException": [
        "downstream_timeout",
        "RetryExhausted",
        "StreamingQueryException",
    ],
    "Permanent_MappingNotFound": [
        "ingestion_gap",
        "IngestionIncomplete",
        "DashboardStale",
    ],
    "StreamingQueryException": [
        "ConsumerLag",
        "BackpressureExceeded",
    ],
    "UserErrorInvalidFolderPath": [
        "DataNotAvailable",
        "DependencyNotMet",
    ],
}

# Maps a pipeline to its known downstream dependencies.
# In production, this would come from GraphRAG or a metadata catalog.
KNOWN_DEPENDENCIES: dict[str, list[str]] = {
    "spark_etl_daily_ingest": [
        "customer_analytics_dashboard",
        "daily_reporting",
        "synapse_pipeline_customer360",
    ],
    "kusto_ingestion_telemetry": [
        "real_time_dashboards",
        "alerting_system",
        "sla_monitoring",
    ],
    "synapse_pipeline_customer360": [
        "customer_360_dashboard",
    ],
    "spark_streaming_clickstream": [
        "real_time_clickstream_analytics",
    ],
}


class CascadeStep:
    """A single step in a cascade chain."""

    def __init__(
        self,
        pipeline_name: str,
        error_class: str,
        alert: Optional[ParsedFailure] = None,
        relationship: str = "caused_by",
    ):
        self.pipeline_name = pipeline_name
        self.error_class = error_class
        self.alert = alert
        self.relationship = relationship

    def __repr__(self) -> str:
        return f"CascadeStep({self.pipeline_name}: {self.error_class})"


class CascadeCluster:
    """
    A cluster of causally-related alerts forming a cascade chain.

    Example chain:
        spark_etl_daily_ingest (OOM) → customer_analytics_dashboard (stale data)
        → daily_reporting (SLA breach)
    """

    def __init__(self, root_cause_alert: ParsedFailure):
        self.root_cause_alert = root_cause_alert
        self.chain: list[CascadeStep] = [
            CascadeStep(
                pipeline_name=root_cause_alert.pipeline_name,
                error_class=root_cause_alert.error_class,
                alert=root_cause_alert,
                relationship="root_cause",
            )
        ]
        self.affected_pipelines: set[str] = {root_cause_alert.pipeline_name}

    def add_step(self, step: CascadeStep) -> None:
        """Add a downstream step to the cascade chain."""
        self.chain.append(step)
        self.affected_pipelines.add(step.pipeline_name)

    @property
    def depth(self) -> int:
        return len(self.chain)

    def summary(self) -> str:
        """Human-readable cascade summary."""
        names = [s.pipeline_name for s in self.chain]
        return f"Cascade: {' → '.join(names)} ({self.depth} steps)"


class AlertDeduplicator:
    """
    Groups and deduplicates alerts based on configurable rules.

    Enhanced with:
    - Cascade detection: chains causally-related failures
    - Cluster grouping: groups alerts sharing a dependency neighborhood
    """

    def __init__(
        self,
        time_window_minutes: int = 30,
        similarity_threshold: float = 0.7,
        cascade_window_multiplier: float = 2.0,
    ):
        self.time_window = timedelta(minutes=time_window_minutes)
        self.similarity_threshold = similarity_threshold
        # AT5: Configurable cascade window multiplier
        self.cascade_window_multiplier: float = cascade_window_multiplier
        self._dedup_groups: dict[str, AlertGroup] = {}
        self._cascade_clusters: list[CascadeCluster] = []
        self._alert_index: dict[str, ParsedFailure] = {}  # failure_id -> alert

    def process_alert(self, alert: ParsedFailure) -> AlertGroup:
        """
        Process an incoming alert and return its dedup group.

        If the alert matches an existing group, it's added to that group.
        Otherwise, a new group is created.
        """
        self._alert_index[alert.failure_id] = alert

        # Generate dedup key: pipeline + error_class combination
        dedup_key = self._compute_dedup_key(alert)

        # Check for existing group within time window
        if dedup_key in self._dedup_groups:
            group = self._dedup_groups[dedup_key]
            time_delta = abs(
                (alert.timestamp - group.latest_timestamp).total_seconds()
            )
            if time_delta <= self.time_window.total_seconds():
                group.add_alert(alert)
                logger.info(
                    "alert_deduplicated",
                    group_id=group.group_id,
                    alert_count=group.alert_count,
                    pipeline=alert.pipeline_name,
                )
                return group

        # Create new group
        group = AlertGroup(
            group_id=dedup_key,
            primary_alert=alert,
        )
        self._dedup_groups[dedup_key] = group
        logger.info(
            "alert_new_group",
            group_id=group.group_id,
            pipeline=alert.pipeline_name,
            error_class=alert.error_class,
        )
        return group

    def detect_cascade(self, alert: ParsedFailure) -> Optional[CascadeCluster]:
        """
        Detect if the given alert is part of a cascade chain.

        Checks:
        1. Is this alert a known root-cause error type?
        2. Are there recent downstream alerts that match cascade patterns?
        3. Are there existing cascades this alert extends?

        Returns CascadeCluster if a cascade is detected, None otherwise.
        """
        # Check if this alert's error class triggers known cascades
        downstream_patterns = KNOWN_CASCADE_PATTERNS.get(alert.error_class, [])
        downstream_deps = KNOWN_DEPENDENCIES.get(alert.pipeline_name, [])

        if not downstream_patterns and not downstream_deps:
            # Check if this alert is a downstream effect of an existing cascade
            return self._find_parent_cascade(alert)

        # Build cascade from this alert as root
        cascade = CascadeCluster(root_cause_alert=alert)

        # Check recent alerts for downstream effects
        now = datetime.now(timezone.utc)
        # AT5: Use configurable cascade window multiplier
        window = self.time_window * self.cascade_window_multiplier  # Wider window for cascades

        for other_alert in self._alert_index.values():
            if other_alert.failure_id == alert.failure_id:
                continue
            if abs((other_alert.timestamp - alert.timestamp).total_seconds()) > window.total_seconds():
                continue

            # Check if downstream pipeline
            is_downstream = other_alert.pipeline_name in downstream_deps
            # Check if error pattern matches cascade
            is_cascade_error = any(
                pat.lower() in other_alert.error_class.lower()
                or pat.lower() in other_alert.error_message.lower()
                for pat in downstream_patterns
            )

            if is_downstream or is_cascade_error:
                cascade.add_step(
                    CascadeStep(
                        pipeline_name=other_alert.pipeline_name,
                        error_class=other_alert.error_class,
                        alert=other_alert,
                        relationship="downstream_effect",
                    )
                )

        if cascade.depth > 1:
            self._cascade_clusters.append(cascade)
            logger.info(
                "cascade_detected",
                root_pipeline=alert.pipeline_name,
                depth=cascade.depth,
                affected=list(cascade.affected_pipelines),
            )
            return cascade

        return None

    def _find_parent_cascade(self, alert: ParsedFailure) -> Optional[CascadeCluster]:
        """Check if this alert extends an existing cascade."""
        for cascade in self._cascade_clusters:
            root_error = cascade.root_cause_alert.error_class
            downstream_patterns = KNOWN_CASCADE_PATTERNS.get(root_error, [])
            downstream_deps = KNOWN_DEPENDENCIES.get(
                cascade.root_cause_alert.pipeline_name, []
            )

            is_downstream = alert.pipeline_name in downstream_deps
            is_cascade_error = any(
                pat.lower() in alert.error_class.lower()
                or pat.lower() in alert.error_message.lower()
                for pat in downstream_patterns
            )

            if is_downstream or is_cascade_error:
                cascade.add_step(
                    CascadeStep(
                        pipeline_name=alert.pipeline_name,
                        error_class=alert.error_class,
                        alert=alert,
                        relationship="downstream_effect",
                    )
                )
                return cascade

        return None

    def get_active_groups(self) -> list["AlertGroup"]:
        """Return all dedup groups that are still within the time window."""
        now = datetime.now(timezone.utc)
        active = []
        for group in self._dedup_groups.values():
            if (now - group.latest_timestamp).total_seconds() <= self.time_window.total_seconds() * 2:
                active.append(group)
        return sorted(active, key=lambda g: g.latest_timestamp, reverse=True)

    def get_active_cascades(self) -> list[CascadeCluster]:
        """Return all active cascade clusters."""
        return list(self._cascade_clusters)

    def _compute_dedup_key(self, alert: ParsedFailure) -> str:
        """Compute a dedup key from pipeline name and error class."""
        raw = f"{alert.pipeline_name}::{alert.error_class}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def get_stats(self) -> dict[str, Any]:
        """Return deduplication statistics."""
        total_alerts = sum(g.alert_count for g in self._dedup_groups.values())
        return {
            "total_groups": len(self._dedup_groups),
            "total_alerts": total_alerts,
            "dedup_ratio": round(
                1 - len(self._dedup_groups) / max(total_alerts, 1), 2
            ),
            "active_cascades": len(self._cascade_clusters),
            "total_indexed": len(self._alert_index),
        }


class AlertGroup:
    """A group of deduplicated alerts sharing the same root issue."""

    def __init__(self, group_id: str, primary_alert: ParsedFailure):
        self.group_id = group_id
        self.primary_alert = primary_alert
        self.related_alerts: list[ParsedFailure] = []
        self.first_seen = primary_alert.timestamp
        self.latest_timestamp = primary_alert.timestamp

    def add_alert(self, alert: ParsedFailure) -> None:
        """Add a related alert to this group."""
        self.related_alerts.append(alert)
        if alert.timestamp > self.latest_timestamp:
            self.latest_timestamp = alert.timestamp
        # Upgrade severity if the new alert is more severe
        severity_order = {
            Severity.LOW: 0,
            Severity.MEDIUM: 1,
            Severity.HIGH: 2,
            Severity.CRITICAL: 3,
        }
        if severity_order.get(alert.severity, 0) > severity_order.get(
            self.primary_alert.severity, 0
        ):
            self.primary_alert = alert

    @property
    def alert_count(self) -> int:
        return 1 + len(self.related_alerts)

    @property
    def is_recurring(self) -> bool:
        return self.alert_count >= 3

    def summary(self) -> str:
        """Generate a human-readable group summary."""
        return (
            f"{self.primary_alert.pipeline_name}: {self.primary_alert.error_class} "
            f"({self.alert_count} alerts, {self.primary_alert.severity.value})"
        )
