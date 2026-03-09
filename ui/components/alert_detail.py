"""Alert detail panel — deep-dive view for a selected alert."""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from src.models import AnalysisResult, HistoricalIncident, ParsedFailure, RoutingDecision, Severity


SEVERITY_COLORS = {
    Severity.CRITICAL: "#FF4444",
    Severity.HIGH: "#FF8C00",
    Severity.MEDIUM: "#FFD700",
    Severity.LOW: "#32CD32",
}


def render_alert_detail(
    alert: ParsedFailure,
    analysis: Optional[AnalysisResult] = None,
    similar_incidents: Optional[list[HistoricalIncident]] = None,
    routing: Optional[RoutingDecision] = None,
) -> None:
    """Render the detailed view for a selected alert."""

    color = SEVERITY_COLORS.get(alert.severity, "#888")

    # Header
    st.markdown(
        f"""
        <div style="
            padding: 1rem;
            border-left: 5px solid {color};
            background: rgba(128,128,128,0.08);
            border-radius: 0 8px 8px 0;
            margin-bottom: 1rem;
        ">
            <h3 style="margin:0 0 0.25rem 0;">{alert.pipeline_name}</h3>
            <div style="display:flex; gap:1rem; align-items:center;">
                <span style="
                    background:{color};
                    color:white;
                    padding:0.15rem 0.5rem;
                    border-radius:4px;
                    font-size:0.75rem;
                    font-weight:600;
                ">{alert.severity.value.upper()}</span>
                <span style="font-size:0.85rem; opacity:0.8;">{alert.error_class}</span>
                <span style="font-size:0.8rem; opacity:0.6;">{alert.timestamp.strftime('%Y-%m-%d %H:%M UTC')}</span>
                <span style="font-size:0.8rem; opacity:0.6;">{alert.pipeline_type.value}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Tabs
    tab_rca, tab_similar, tab_routing, tab_logs, tab_actions = st.tabs([
        "Root Cause",
        "Similar Incidents",
        "Routing",
        "Logs",
        "Actions",
    ])

    with tab_rca:
        _render_root_cause(alert, analysis)

    with tab_similar:
        _render_similar_incidents(similar_incidents or (analysis.similar_incidents if analysis else []))

    with tab_routing:
        _render_routing(routing or (analysis.routing if analysis else None), alert)

    with tab_logs:
        _render_logs(alert)

    with tab_actions:
        _render_actions(alert, analysis)


def _render_root_cause(alert: ParsedFailure, analysis: Optional[AnalysisResult]) -> None:
    """Render root cause analysis tab."""
    st.markdown("### Root Cause Analysis")

    st.markdown(f"**Summary:** {alert.root_cause_summary}")

    if analysis:
        st.divider()
        st.markdown("#### Detailed Analysis")
        st.markdown(analysis.root_cause_analysis)

        if analysis.reasoning_trace:
            with st.expander("Reasoning Trace"):
                for step in analysis.reasoning_trace:
                    st.markdown(f"- {step}")

        st.markdown(f"**Confidence:** {analysis.confidence:.0%}")

    st.divider()
    st.markdown("#### Affected Components")
    if alert.affected_components:
        for comp in alert.affected_components:
            st.markdown(f"- {comp}")
    else:
        st.info("No affected components identified yet.")


def _render_similar_incidents(incidents: list[HistoricalIncident]) -> None:
    """Render similar incidents tab."""
    st.markdown("### Similar Historical Incidents")

    if not incidents:
        st.info("No similar incidents found. The knowledge base may need to be indexed.")
        return

    for inc in incidents:
        similarity_pct = f"{inc.similarity_score:.0%}"
        with st.expander(f"[{inc.incident_id}] {inc.title} — {similarity_pct} match"):
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Pipeline:** {inc.pipeline_name}")
                st.markdown(f"**Error Class:** {inc.error_class}")
                st.markdown(f"**Severity:** {inc.severity.value}")
            with col2:
                st.markdown(f"**Occurred:** {inc.occurred_at.strftime('%Y-%m-%d')}")
                if inc.resolved_at:
                    delta = inc.resolved_at - inc.occurred_at
                    st.markdown(f"**Resolved in:** {delta.total_seconds() / 60:.0f} minutes")
                if inc.tags:
                    st.markdown(f"**Tags:** {', '.join(inc.tags)}")

            st.markdown("---")
            st.markdown(f"**Root Cause:** {inc.root_cause}")
            st.markdown(f"**Resolution:** {inc.resolution}")


def _render_routing(routing: Optional[RoutingDecision], alert: ParsedFailure) -> None:
    """Render routing information tab."""
    st.markdown("### Alert Routing")

    if not routing:
        st.info("Routing information not available.")
        return

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Target Channel:** #{routing.target_channel}")
        st.markdown(f"**Severity:** {routing.severity.value.upper()}")
        st.markdown(f"**Auto-Escalate:** {'Yes' if routing.auto_escalate else 'No'}")
        st.markdown(f"**Confidence:** {routing.confidence:.0%}")

    with col2:
        st.markdown("**Escalation Contacts:**")
        for contact in routing.target_contacts:
            st.markdown(f"- {contact}")

    st.divider()
    st.markdown(f"**Routing Reason:** {routing.routing_reason}")


def _render_logs(alert: ParsedFailure) -> None:
    """Render logs tab."""
    st.markdown("### Log Output")

    if alert.log_snippet:
        st.code(alert.log_snippet, language="log")
    else:
        st.info("No log snippet available for this alert.")

    # Quick links
    links = []
    if alert.log_url:
        links.append(f"[Open Full Logs]({alert.log_url})")
    if alert.workspace_url:
        links.append(f"[Open Workspace]({alert.workspace_url})")
    if alert.runbook_url:
        links.append(f"[Open Runbook]({alert.runbook_url})")

    if links:
        st.markdown(" | ".join(links))


def _render_actions(alert: ParsedFailure, analysis: Optional[AnalysisResult]) -> None:
    """Render actions tab."""
    st.markdown("### Recommended Actions")

    if analysis and analysis.recommended_actions:
        for i, action in enumerate(analysis.recommended_actions, 1):
            st.markdown(f"{i}. {action}")
    else:
        st.markdown("1. Check logs for the root error")
        st.markdown("2. Verify upstream data sources are healthy")
        st.markdown("3. Check cluster/pool resource utilization")
        st.markdown("4. Review recent code or configuration changes")

    st.divider()

    # Runbook steps
    st.markdown("### Runbook Steps")
    if analysis and analysis.runbook_steps:
        for i, step in enumerate(analysis.runbook_steps, 1):
            st.markdown(f"{i}. {step}")
    else:
        st.info("No runbook steps available. Search the knowledge base for procedures.")

    st.divider()

    # Quick action buttons
    st.markdown("### Quick Actions")
    col1, col2, col3 = st.columns(3)

    with col1:
        if alert.rerun_url:
            st.link_button("🔄 Rerun Pipeline", alert.rerun_url, use_container_width=True)
        else:
            st.button("🔄 Rerun Pipeline", disabled=True, use_container_width=True, help="No rerun URL configured")

    with col2:
        if alert.log_url:
            st.link_button("📋 Open Logs", alert.log_url, use_container_width=True)
        else:
            st.button("📋 Open Logs", disabled=True, use_container_width=True, help="No log URL configured")

    with col3:
        if alert.runbook_url:
            st.link_button("📘 Open Runbook", alert.runbook_url, use_container_width=True)
        else:
            st.button("📘 Open Runbook", disabled=True, use_container_width=True, help="No runbook URL configured")
