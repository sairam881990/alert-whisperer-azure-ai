"""
Data Refresh Engine — manages the transition from demo data to live MCP data.

This module handles:
1. Polling MCP servers on a configurable interval
2. Feeding new alerts into the AlertTriageEngine
3. Tracking data source status (demo vs live)
4. Providing last-refresh timestamps for the UI

Architecture:
-----------
Demo Mode (MCP not connected):
  - App starts → generate_demo_alerts() seeds the triage engine once
  - Data is static; no refresh cycle runs
  - Chat responses are labeled "[Demo Data]"

Live Mode (MCP connected):
  - User connects MCP servers via Settings page
  - DataRefreshEngine.start() begins a polling loop
  - Each poll cycle:
      1. Queries each connected MCP server for new alerts since last poll
      2. Parses raw responses into ParsedFailure objects via AlertProcessor
      3. Feeds new alerts into the triage engine via ingest_alert()
      4. Updates last_refresh_ts and counters
  - Streamlit's st.fragment or st.rerun() triggers UI updates
  - Chat responses are labeled "[Live — last refresh: 02:15 UTC]"

Refresh intervals are configurable:
  - 30 seconds (real-time monitoring)
  - 1 minute (default, recommended)
  - 5 minutes (low-frequency)
  - Manual only (user clicks refresh button)

Why this design:
  Streamlit reruns the entire script on every interaction. We can't run
  a persistent background thread. Instead, we use Streamlit's native
  st.fragment (Streamlit >=1.33) for auto-refresh, or a time-check
  pattern in session_state for older versions. Each script rerun checks
  if enough time has elapsed since the last poll and triggers a refresh
  if needed. This is the standard Streamlit pattern for periodic data
  updates without external schedulers.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

import structlog

from src.models import ParsedFailure

logger = structlog.get_logger(__name__)


class DataSourceMode(str, Enum):
    """Current data source mode."""
    DEMO = "demo"
    LIVE = "live"
    MIXED = "mixed"  # Some connectors live, some not


class RefreshInterval(str, Enum):
    """Configurable refresh intervals."""
    REALTIME = "30s"
    STANDARD = "1m"
    LOW = "5m"
    MANUAL = "manual"

    @property
    def seconds(self) -> Optional[int]:
        mapping = {"30s": 30, "1m": 60, "5m": 300, "manual": None}
        return mapping.get(self.value)


class ConnectorStatus:
    """Status of a single MCP connector."""

    def __init__(self, name: str, connector: Any = None):
        self.name = name
        self.connector = connector
        self.connected: bool = False
        self.last_poll_ts: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.alerts_fetched: int = 0
        self.total_polls: int = 0

        # D9: per-connector query latency tracking
        # All latency values are stored in milliseconds.
        self._latency_samples: list[float] = []  # rolling window of poll durations
        self._latency_window: int = 100          # keep last N samples
        self.last_latency_ms: Optional[float] = None
        self.average_latency_ms: Optional[float] = None
        self.p95_latency_ms: Optional[float] = None
        self.min_latency_ms: Optional[float] = None
        self.max_latency_ms: Optional[float] = None

    def record_poll_latency(self, duration_ms: float) -> None:
        """
        Record the wall-clock duration of a single poll cycle.

        Maintains a rolling window of ``_latency_window`` samples and
        recomputes aggregate statistics (average, p95, min, max) after
        every observation.

        Args:
            duration_ms: Elapsed time in milliseconds for the poll.
        """
        self._latency_samples.append(duration_ms)
        # Trim to rolling window
        if len(self._latency_samples) > self._latency_window:
            self._latency_samples = self._latency_samples[-self._latency_window:]

        self.last_latency_ms = duration_ms
        n = len(self._latency_samples)
        self.average_latency_ms = sum(self._latency_samples) / n
        self.min_latency_ms = min(self._latency_samples)
        self.max_latency_ms = max(self._latency_samples)

        # p95 via nearest-rank method
        sorted_samples = sorted(self._latency_samples)
        p95_idx = max(0, int(0.95 * n) - 1)
        self.p95_latency_ms = sorted_samples[p95_idx]

    @property
    def latency_summary(self) -> str:
        """Human-readable latency string for UI display."""
        if self.average_latency_ms is None:
            return "no data"
        return (
            f"avg={self.average_latency_ms:.0f}ms "
            f"p95={self.p95_latency_ms:.0f}ms "
            f"last={self.last_latency_ms:.0f}ms"
        )

    @property
    def status_icon(self) -> str:
        if not self.connector:
            return "⚪"  # Not configured
        if self.connected:
            return "✅"
        return "❌"

    @property
    def status_label(self) -> str:
        if not self.connector:
            return "Not configured"
        if self.connected:
            elapsed = ""
            if self.last_poll_ts:
                delta = datetime.now(timezone.utc) - self.last_poll_ts
                secs = int(delta.total_seconds())
                if secs < 60:
                    elapsed = f" ({secs}s ago)"
                elif secs < 3600:
                    elapsed = f" ({secs // 60}m ago)"
                else:
                    elapsed = f" ({secs // 3600}h ago)"
            return f"Connected{elapsed}"
        if self.last_error:
            return f"Error: {self.last_error[:50]}"
        return "Disconnected"


class DataRefreshEngine:
    """
    Manages data refresh from MCP servers and tracks data source status.

    Usage in app.py:
        # Initialize once in session_state
        if "refresh_engine" not in st.session_state:
            st.session_state.refresh_engine = DataRefreshEngine(
                triage_engine=st.session_state.triage_engine
            )

        # On each Streamlit rerun, check if refresh is due
        st.session_state.refresh_engine.maybe_refresh()
    """

    def __init__(self, triage_engine: Any):
        self.triage_engine = triage_engine
        self.mode: DataSourceMode = DataSourceMode.DEMO
        self.refresh_interval: RefreshInterval = RefreshInterval.STANDARD
        self.last_refresh_ts: Optional[datetime] = None
        self.total_refreshes: int = 0
        self.total_alerts_ingested: int = 0

        # Per-connector status tracking
        self.connectors: dict[str, ConnectorStatus] = {
            "Kusto MCP": ConnectorStatus("Kusto MCP"),
            "Log Analytics MCP": ConnectorStatus("Log Analytics MCP"),
            "Confluence MCP": ConnectorStatus("Confluence MCP"),
            "ICM MCP": ConnectorStatus("ICM MCP"),
        }

    def register_connector(self, name: str, connector: Any) -> None:
        """Register a live MCP connector, replacing demo for that source."""
        if name in self.connectors:
            self.connectors[name].connector = connector
            self.connectors[name].connected = True
            self._update_mode()
            logger.info("connector_registered", name=name)

    def disconnect_connector(self, name: str) -> None:
        """Disconnect a connector, falling back to demo for that source."""
        if name in self.connectors:
            self.connectors[name].connector = None
            self.connectors[name].connected = False
            self.connectors[name].last_error = None
            self._update_mode()
            logger.info("connector_disconnected", name=name)

    def _update_mode(self) -> None:
        """Recalculate the data source mode based on connector states."""
        connected = [c for c in self.connectors.values() if c.connected]
        if not connected:
            self.mode = DataSourceMode.DEMO
        elif len(connected) == len(self.connectors):
            self.mode = DataSourceMode.LIVE
        else:
            self.mode = DataSourceMode.MIXED

    @property
    def is_refresh_due(self) -> bool:
        """Check if enough time has elapsed for the next refresh cycle."""
        if self.mode == DataSourceMode.DEMO:
            return False  # No polling in demo mode
        if self.refresh_interval == RefreshInterval.MANUAL:
            return False  # Manual refresh only
        interval_secs = self.refresh_interval.seconds
        if interval_secs is None:
            return False
        if self.last_refresh_ts is None:
            return True  # Never refreshed yet
        elapsed = (datetime.now(timezone.utc) - self.last_refresh_ts).total_seconds()
        return elapsed >= interval_secs

    def maybe_refresh(self) -> list[ParsedFailure]:
        """
        Check if refresh is due and execute if so.

        Called on every Streamlit rerun. Returns list of newly ingested alerts.
        This is the primary integration point for app.py.
        """
        if not self.is_refresh_due:
            return []
        return self.force_refresh()

    def force_refresh(self) -> list[ParsedFailure]:
        """
        Force an immediate refresh from all connected MCP servers.

        Called by the manual refresh button or by maybe_refresh().
        Returns list of newly ingested alerts.
        """
        if self.mode == DataSourceMode.DEMO:
            return []

        new_alerts: list[ParsedFailure] = []
        for name, status in self.connectors.items():
            if not status.connected or not status.connector:
                continue
            try:
                # D9: measure wall-clock time for this poll cycle
                _poll_start = time.monotonic()
                alerts = self._poll_connector(name, status)
                _poll_elapsed_ms = (time.monotonic() - _poll_start) * 1000.0

                new_alerts.extend(alerts)
                status.last_poll_ts = datetime.now(timezone.utc)
                status.alerts_fetched += len(alerts)
                status.total_polls += 1
                status.last_error = None

                # D9: record latency sample
                status.record_poll_latency(_poll_elapsed_ms)
                logger.debug(
                    "connector_poll_latency",
                    name=name,
                    latency_ms=round(_poll_elapsed_ms, 1),
                    avg_ms=round(status.average_latency_ms or 0, 1),
                    p95_ms=round(status.p95_latency_ms or 0, 1),
                )
            except Exception as e:
                status.last_error = str(e)
                logger.error("connector_poll_failed", name=name, error=str(e))

        # Feed into triage engine
        if new_alerts:
            self.triage_engine.ingest_alerts(new_alerts)
            self.total_alerts_ingested += len(new_alerts)

        self.last_refresh_ts = datetime.now(timezone.utc)
        self.total_refreshes += 1

        logger.info(
            "refresh_complete",
            new_alerts=len(new_alerts),
            total_refreshes=self.total_refreshes,
        )
        return new_alerts

    def _poll_connector(
        self, name: str, status: ConnectorStatus
    ) -> list[ParsedFailure]:
        """
        Poll a single MCP connector for new alerts.

        Each connector has its own query method. We run async connectors
        synchronously here since Streamlit doesn't natively support async.
        """
        connector = status.connector
        since = status.last_poll_ts or (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )

        try:
            loop = asyncio.new_event_loop()

            if name == "Kusto MCP":
                # Query Kusto for ingestion failures
                result = loop.run_until_complete(
                    connector.get_ingestion_failures(
                        since=since.isoformat(),
                        limit=100,
                    )
                )
            elif name == "Log Analytics MCP":
                # Query Log Analytics for Spark/pipeline errors
                result = loop.run_until_complete(
                    connector.query_failures(
                        timespan_hours=max(
                            1,
                            int(
                                (datetime.now(timezone.utc) - since).total_seconds()
                                / 3600
                            ),
                        ),
                        severity_filter="warning,error,critical",
                    )
                )
            elif name == "ICM MCP":
                # Query ICM for new/updated incidents
                result = loop.run_until_complete(
                    connector.get_active_incidents(
                        since=since.isoformat(),
                    )
                )
            elif name == "Confluence MCP":
                # Confluence doesn't produce alerts — skip polling
                loop.close()
                return []
            else:
                loop.close()
                return []

            loop.close()

            # Parse results into ParsedFailure objects
            if isinstance(result, list):
                return [
                    self._parse_mcp_result(item, name) for item in result
                    if item is not None
                ]
            return []

        except Exception as e:
            logger.error("poll_connector_error", name=name, error=str(e))
            raise

    def _parse_mcp_result(self, item: Any, source_name: str) -> ParsedFailure:
        """
        Parse an MCP result into a ParsedFailure.

        In production, this delegates to AlertProcessor.process_alert().
        For now, handles the common fields from each MCP source.
        """
        from src.models import AlertSource, PipelineType, Severity

        # Default mapping
        if isinstance(item, ParsedFailure):
            return item

        if isinstance(item, dict):
            return ParsedFailure(
                failure_id=item.get("id", f"mcp-{hash(str(item)) & 0xFFFFFF:06x}"),
                pipeline_name=item.get("pipeline_name", item.get("name", "unknown")),
                pipeline_type=PipelineType(
                    item.get("pipeline_type", "spark_batch")
                ),
                source=AlertSource(
                    item.get("source", "log_analytics")
                ),
                timestamp=datetime.fromisoformat(
                    item.get("timestamp", datetime.now(timezone.utc).isoformat())
                ),
                severity=Severity(item.get("severity", "medium")),
                error_class=item.get("error_class", "Unknown"),
                error_message=item.get("error_message", ""),
                root_cause_summary=item.get("root_cause", ""),
                affected_components=item.get("affected_components", []),
                log_snippet=item.get("log_snippet", ""),
            )

        # Fallback: can't parse
        raise ValueError(f"Cannot parse MCP result from {source_name}: {type(item)}")

    # ─── Status & Display Helpers ─────────────────

    @property
    def status_summary(self) -> str:
        """Human-readable status for the sidebar."""
        if self.mode == DataSourceMode.DEMO:
            return "📋 Demo Data"
        connected_count = sum(1 for c in self.connectors.values() if c.connected)
        total = len(self.connectors)
        if self.mode == DataSourceMode.LIVE:
            label = "🟢 Live"
        else:
            label = "🟡 Mixed"
        refresh_str = ""
        if self.last_refresh_ts:
            delta = datetime.now(timezone.utc) - self.last_refresh_ts
            secs = int(delta.total_seconds())
            if secs < 60:
                refresh_str = f" · {secs}s ago"
            elif secs < 3600:
                refresh_str = f" · {secs // 60}m ago"
        return f"{label} ({connected_count}/{total}){refresh_str}"

    @property
    def data_source_tag(self) -> str:
        """
        Short tag to append to chat responses indicating data source.
        Shows users whether they're seeing demo or live data.
        """
        if self.mode == DataSourceMode.DEMO:
            return "_Data source: Demo_"
        if self.last_refresh_ts:
            ts = self.last_refresh_ts.strftime("%H:%M UTC")
            if self.mode == DataSourceMode.LIVE:
                return f"_Data source: Live — last refresh {ts}_"
            return f"_Data source: Mixed (some MCP connected) — last refresh {ts}_"
        if self.mode == DataSourceMode.LIVE:
            return "_Data source: Live_"
        return "_Data source: Mixed (some MCP connected)_"

    def get_refresh_stats(self) -> dict[str, Any]:
        """Get stats for the settings/dashboard page."""
        return {
            "mode": self.mode.value,
            "refresh_interval": self.refresh_interval.value,
            "total_refreshes": self.total_refreshes,
            "total_alerts_ingested": self.total_alerts_ingested,
            "last_refresh": (
                self.last_refresh_ts.strftime("%Y-%m-%d %H:%M:%S UTC")
                if self.last_refresh_ts
                else "Never"
            ),
            "connectors": {
                name: {
                    "connected": cs.connected,
                    "status": cs.status_label,
                    "alerts_fetched": cs.alerts_fetched,
                    "total_polls": cs.total_polls,
                    # D9: latency stats
                    "last_latency_ms": round(cs.last_latency_ms, 1) if cs.last_latency_ms is not None else None,
                    "average_latency_ms": round(cs.average_latency_ms, 1) if cs.average_latency_ms is not None else None,
                    "p95_latency_ms": round(cs.p95_latency_ms, 1) if cs.p95_latency_ms is not None else None,
                    "min_latency_ms": round(cs.min_latency_ms, 1) if cs.min_latency_ms is not None else None,
                    "max_latency_ms": round(cs.max_latency_ms, 1) if cs.max_latency_ms is not None else None,
                    "latency_summary": cs.latency_summary,
                }
                for name, cs in self.connectors.items()
            },
        }
