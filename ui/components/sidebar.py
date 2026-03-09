"""
Sidebar component — alert feed, filters, search, and navigation.

Enhanced with:
- Search box for filtering alerts by keyword
- Severity-sorted grouped view (Critical first)
- Cluster badges showing dedup count
- Alert count indicators per severity
- Triage engine integration for cluster display
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.engine.data_refresh import DataRefreshEngine, DataSourceMode
from src.models import AlertFeedItem, Severity


SEVERITY_COLORS = {
    Severity.CRITICAL: "#FF4444",
    Severity.HIGH: "#FF8C00",
    Severity.MEDIUM: "#FFD700",
    Severity.LOW: "#32CD32",
}

SEVERITY_ICONS = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🟢",
}

SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}


def render_sidebar(
    alert_feed: list[AlertFeedItem],
    triage_engine: Any = None,
) -> Optional[str]:
    """
    Render the sidebar with alert feed, search, filters, and cluster view.

    Enhanced:
    - Search box at the top for instant keyword filtering
    - Alerts sorted by severity (critical first) within each group
    - Cluster badges when triage engine provides grouping
    - Severity count indicators in the header

    Returns:
        Selected alert failure_id or None
    """
    with st.sidebar:
        st.markdown(
            """
            <div style="text-align:center; padding: 0.5rem 0 1rem 0;">
                <h2 style="margin:0; font-size:1.4rem;">🔔 Alert Whisperer</h2>
                <p style="margin:0; font-size:0.8rem; opacity:0.7;">Real-Time Pipeline Intelligence</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.divider()

        # Navigation
        page = st.radio(
            "Navigate",
            ["Dashboard", "Alert Feed", "Chat", "Knowledge Base", "RAG Observability", "Settings"],
            index=0,
            key="nav_radio",
            label_visibility="collapsed",
        )
        st.session_state["current_page"] = page

        st.divider()

        # ─── Search Box ──────────────────────────
        alert_search = st.text_input(
            "Search alerts",
            placeholder="Pipeline, error, keyword...",
            key="sidebar_alert_search",
            label_visibility="collapsed",
        )

        # ─── Filters ─────────────────────────────
        st.markdown("#### Filters")
        col1, col2 = st.columns(2)
        with col1:
            severity_filter = st.multiselect(
                "Severity",
                options=[s.value for s in Severity],
                default=[s.value for s in Severity],
                key="severity_filter",
            )
        with col2:
            source_filter = st.selectbox(
                "Source",
                options=["All", "Spark", "Synapse", "Kusto", "ICM"],
                key="source_filter",
            )

        st.divider()

        # ─── Severity Summary Badges ─────────────
        _render_severity_summary(alert_feed)

        # ─── Triage Clusters (if available) ──────
        if triage_engine:
            _render_triage_clusters(triage_engine)
            st.divider()

        # ─── Alert Feed ──────────────────────────
        st.markdown("#### Recent Alerts")

        # Apply filters
        filtered = alert_feed

        # Text search
        if alert_search:
            search_lower = alert_search.lower()
            filtered = [
                a for a in filtered
                if search_lower in a.failure.pipeline_name.lower()
                or search_lower in a.failure.error_class.lower()
                or search_lower in a.failure.error_message.lower()
                or search_lower in a.failure.root_cause_summary.lower()
            ]

        # Severity filter
        if severity_filter:
            filtered = [a for a in filtered if a.failure.severity.value in severity_filter]

        # Source filter
        if source_filter != "All":
            filtered = [
                a for a in filtered
                if source_filter.lower() in a.failure.source.value.lower()
                or source_filter.lower() in a.failure.pipeline_type.value.lower()
            ]

        # Sort by severity (critical first), then by timestamp (newest first)
        filtered.sort(
            key=lambda a: (
                SEVERITY_ORDER.get(a.failure.severity, 99),
                -a.failure.timestamp.timestamp(),
            )
        )

        selected_id = None

        if not filtered:
            if alert_search:
                st.info(f"No alerts matching '{alert_search}'.")
            else:
                st.info("No alerts matching filters.")
        else:
            # Group by severity for visual clarity
            current_severity = None
            for item in filtered:
                alert = item.failure
                icon = SEVERITY_ICONS.get(alert.severity, "⚪")
                color = SEVERITY_COLORS.get(alert.severity, "#888")

                # Severity group header
                if alert.severity != current_severity:
                    current_severity = alert.severity
                    sev_count = sum(
                        1 for a in filtered if a.failure.severity == current_severity
                    )
                    st.markdown(
                        f"<div style='font-size:0.75rem; font-weight:600; color:{color}; "
                        f"padding: 0.3rem 0; margin-top:0.3rem;'>"
                        f"{icon} {current_severity.value.upper()} ({sev_count})"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                # Time display
                time_str = alert.timestamp.strftime("%H:%M UTC")

                # Cluster badge (if triage engine grouped this alert)
                cluster_badge = ""
                if triage_engine:
                    cluster_info = _get_cluster_info(triage_engine, alert.failure_id)
                    if cluster_info and cluster_info.get("count", 1) > 1:
                        cluster_badge = (
                            f" <span style='background:{color}22; color:{color}; "
                            f"padding:1px 5px; border-radius:8px; font-size:0.65rem;'>"
                            f"×{cluster_info['count']}"
                            f"{'⛓️' if cluster_info.get('is_cascade') else ''}"
                            f"</span>"
                        )

                with st.container():
                    if st.button(
                        f"{icon} {alert.pipeline_name}",
                        key=f"alert_{alert.failure_id}",
                        use_container_width=True,
                        type="secondary",
                    ):
                        selected_id = alert.failure_id
                        st.session_state["selected_alert_id"] = alert.failure_id

                    st.markdown(
                        f"<div style='font-size:0.75rem; color:#888; margin-top:-0.5rem; padding-left:0.5rem;'>"
                        f"{alert.error_class} · {time_str}"
                        f"{cluster_badge}"
                        f"{'  📘' if item.has_runbook else ''}"
                        f"{'  🔗 ' + str(item.similar_count) if item.similar_count else ''}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

        st.divider()

        # ─── Data Source & Refresh ────────────
        _render_data_source_section()

        st.divider()

        # Connection status
        st.markdown("#### Connections")
        connections = st.session_state.get("connections", {})
        for name, status in connections.items():
            icon = "✅" if status else "❌"
            st.markdown(f"<span style='font-size:0.8rem;'>{icon} {name}</span>", unsafe_allow_html=True)

    return selected_id or st.session_state.get("selected_alert_id")


def _render_data_source_section() -> None:
    """
    Render the data source mode indicator, last-refresh timestamp,
    and manual refresh button in the sidebar.
    """
    refresh_engine: DataRefreshEngine | None = st.session_state.get("refresh_engine")
    if not refresh_engine:
        return

    # Data source mode badge
    st.markdown(
        f"<div style='font-size:0.8rem; font-weight:600; padding:0.3rem 0;'>"
        f"{refresh_engine.status_summary}</div>",
        unsafe_allow_html=True,
    )

    # Last refresh timestamp
    if refresh_engine.last_refresh_ts:
        ts_str = refresh_engine.last_refresh_ts.strftime("%H:%M:%S UTC")
        st.markdown(
            f"<div style='font-size:0.7rem; opacity:0.7;'>"
            f"Last refresh: {ts_str}</div>",
            unsafe_allow_html=True,
        )

    # Manual refresh button (only shown in Live/Mixed mode)
    if refresh_engine.mode != DataSourceMode.DEMO:
        if st.button("🔄 Refresh Now", key="btn_manual_refresh", use_container_width=True):
            new_alerts = refresh_engine.force_refresh()
            if new_alerts:
                st.session_state["demo_alerts"] = (
                    st.session_state.get("demo_alerts", []) + new_alerts
                )
                st.toast(f"{len(new_alerts)} new alert(s) ingested")
            else:
                st.toast("No new alerts")
            st.rerun()


def _render_severity_summary(alert_feed: list[AlertFeedItem]) -> None:
    """Render compact severity count badges."""
    counts = {sev: 0 for sev in Severity}
    for item in alert_feed:
        counts[item.failure.severity] = counts.get(item.failure.severity, 0) + 1

    badges = []
    for sev in [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]:
        count = counts.get(sev, 0)
        if count > 0:
            color = SEVERITY_COLORS[sev]
            icon = SEVERITY_ICONS[sev]
            badges.append(
                f"<span style='background:{color}22; color:{color}; "
                f"padding:2px 8px; border-radius:10px; font-size:0.75rem; "
                f"margin-right:4px;'>"
                f"{icon} {count}</span>"
            )

    if badges:
        st.markdown(
            f"<div style='text-align:center; padding:0.25rem 0;'>{''.join(badges)}</div>",
            unsafe_allow_html=True,
        )


def _render_triage_clusters(triage_engine: Any) -> None:
    """Render top triage clusters when available."""
    try:
        critical_clusters = triage_engine.get_critical_clusters()
        if not critical_clusters:
            return

        st.markdown(
            "<div style='font-size:0.8rem; font-weight:600; padding:0.3rem 0;'>"
            "⚡ Active Clusters</div>",
            unsafe_allow_html=True,
        )

        for cluster in critical_clusters[:3]:
            cascade_tag = " ⛓️" if cluster.is_cascade else ""
            color = SEVERITY_COLORS.get(cluster.composite_severity, "#888")
            st.markdown(
                f"<div style='font-size:0.72rem; padding:0.2rem 0.4rem; margin:2px 0; "
                f"border-left:3px solid {color}; background:rgba(128,128,128,0.06); "
                f"border-radius:0 4px 4px 0;'>"
                f"<span style='color:{color};'>"
                f"{cluster.primary_alert.pipeline_name}</span>"
                f" ({cluster.alert_count} alerts{cascade_tag})"
                f"</div>",
                unsafe_allow_html=True,
            )
    except Exception:
        pass  # Gracefully degrade if triage engine API changes


def _get_cluster_info(triage_engine: Any, failure_id: str) -> Optional[dict]:
    """Look up cluster info for a specific alert."""
    try:
        for cluster in triage_engine.get_top_clusters(limit=20):
            for alert in cluster.all_alerts:
                if alert.failure_id == failure_id:
                    return {
                        "count": cluster.alert_count,
                        "is_cascade": cluster.is_cascade,
                        "cluster_id": cluster.cluster_id,
                    }
    except Exception:
        pass
    return None
