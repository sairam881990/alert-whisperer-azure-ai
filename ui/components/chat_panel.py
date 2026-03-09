"""
Chat panel component — conversational troubleshooting interface.

Enhanced with:
- Alert-free mode: works without selecting an alert first
- Discovery quick actions: "What's broken?", "Platform health", "Show critical alerts"
- Proactive greeting with triage summary when no alert is selected
- Time-window support: "What failed in the last hour?", "Show incidents from last 90 days"
- Live triage engine integration: all responses query real data, not hardcoded strings
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import streamlit as st

from src.models import ConversationContext, MessageRole, ParsedFailure, Severity


# Severity display helpers
_SEV_ICON = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🟢",
}


def render_chat_panel(
    active_alert: Optional[ParsedFailure] = None,
    analysis_result: Any = None,
    triage_engine: Any = None,
) -> None:
    """
    Render the conversational chat interface.

    Supports:
    - Message history display
    - Context-aware suggestions
    - Alert binding
    - Quick action buttons that auto-execute and display results
    - Alert-free discovery mode with proactive intelligence
    """
    st.markdown("## Chat — Troubleshooting Assistant")

    # Alert context banner (when an alert is selected)
    if active_alert:
        _render_alert_banner(active_alert)

        # Quick action buttons — alert-bound actions
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            if st.button("🔍 Similar Incidents", key="btn_similar", use_container_width=True):
                _execute_quick_action(
                    "Show me similar failures in the last 90 days",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col2:
            if st.button("📘 Runbook", key="btn_runbook", use_container_width=True):
                _execute_quick_action(
                    "What are the runbook steps for this failure?",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col3:
            if st.button("🔎 Root Cause", key="btn_rca", use_container_width=True):
                _execute_quick_action(
                    "Explain the root cause in detail",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col4:
            if st.button("📋 Logs", key="btn_logs", use_container_width=True):
                _execute_quick_action(
                    "Show me the relevant logs",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col5:
            if st.button("🚨 Escalate", key="btn_escalate", use_container_width=True):
                _execute_quick_action(
                    "I need to escalate this alert",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )

        # Export investigation button
        if st.session_state.get("messages"):
            if st.button("📄 Export as Markdown", key="btn_export", use_container_width=False):
                _export_investigation(active_alert)

        st.divider()
    else:
        # ─── Alert-Free Mode ─────────────────────
        # Instead of blocking with "select an alert", show discovery actions

        # Discovery quick action buttons
        st.markdown(
            "<div style='font-size:0.85rem; opacity:0.8; margin-bottom:0.5rem;'>"
            "No alert selected — try a discovery query or select an alert from the sidebar."
            "</div>",
            unsafe_allow_html=True,
        )
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("🔥 What's Broken?", key="btn_discovery", use_container_width=True):
                _execute_quick_action(
                    "What's broken right now?",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col2:
            if st.button("🏥 Platform Health", key="btn_health", use_container_width=True):
                _execute_quick_action(
                    "Show me the platform health report",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col3:
            if st.button("🔴 Critical Alerts", key="btn_critical", use_container_width=True):
                _execute_quick_action(
                    "Show me all critical alerts",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )
        with col4:
            if st.button("📊 Analytics", key="btn_analytics", use_container_width=True):
                _execute_quick_action(
                    "Show me alert analytics for the last 24 hours",
                    active_alert,
                    analysis_result,
                    triage_engine,
                )

        st.divider()

    # Initialize message history
    if "messages" not in st.session_state:
        st.session_state.messages = []

        # Add initial greeting — proactive if triage engine is available
        greeting = _build_greeting(active_alert, triage_engine)
        st.session_state.messages.append({
            "role": "assistant",
            "content": greeting,
        })

    # Display chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input — works with or without an active alert
    placeholder = (
        "Ask about this alert, similar incidents, runbook steps..."
        if active_alert
        else "Ask anything: 'What failed?', 'Is spark_etl healthy?', 'Show critical alerts'..."
    )
    if prompt := st.chat_input(placeholder):
        # Add user message
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Generate response
        with st.chat_message("assistant"):
            with st.spinner("Analyzing..."):
                response = _generate_response(prompt, active_alert, analysis_result, triage_engine)
            st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})


def _render_alert_banner(active_alert: ParsedFailure) -> None:
    """Render the alert context banner at the top of the chat."""
    severity_colors = {
        "critical": "#FF4444",
        "high": "#FF8C00",
        "medium": "#FFD700",
        "low": "#32CD32",
    }
    color = severity_colors.get(active_alert.severity.value, "#888")

    st.markdown(
        f"""
        <div style="
            padding: 0.75rem 1rem;
            border-left: 4px solid {color};
            background: rgba(128,128,128,0.1);
            border-radius: 0 8px 8px 0;
            margin-bottom: 1rem;
        ">
            <div style="font-weight:600; font-size:0.9rem;">
                Active: {active_alert.pipeline_name}
                <span style="color:{color}; font-size:0.8rem; margin-left:0.5rem;">
                    {active_alert.severity.value.upper()}
                </span>
            </div>
            <div style="font-size:0.8rem; opacity:0.8; margin-top:0.25rem;">
                {active_alert.error_class} — {active_alert.root_cause_summary[:120]}...
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _build_greeting(
    active_alert: Optional[ParsedFailure],
    triage_engine: Any = None,
) -> str:
    """Build context-aware greeting for the chat session."""
    if active_alert:
        return (
            f"I'm Alert Whisperer, ready to help troubleshoot.\n\n"
            f"**Active Alert:**\n"
            f"- **Pipeline:** {active_alert.pipeline_name}\n"
            f"- **Error:** {active_alert.error_class}\n"
            f"- **Severity:** {active_alert.severity.value}\n"
            f"- **Root Cause:** {active_alert.root_cause_summary}\n\n"
            f"You can ask me about similar past incidents, runbook steps, "
            f"root cause details, or anything else about this failure."
        )

    # Proactive greeting — use triage engine if available
    if triage_engine:
        try:
            return triage_engine.build_proactive_greeting()
        except Exception:
            pass

    # Default greeting with discovery prompts
    return (
        "I'm Alert Whisperer — your pipeline intelligence assistant.\n\n"
        "You can ask me anything without selecting an alert first:\n\n"
        "- **\"What's broken right now?\"** — See all active failures\n"
        "- **\"Show me all critical alerts\"** — Filter by severity\n"
        "- **\"Is spark_etl_daily_ingest healthy?\"** — Pipeline health check\n"
        "- **\"What failed in the last hour?\"** — Time-windowed discovery\n"
        "- **\"Compare yesterday vs today\"** — Cross-alert analytics\n"
        "- **\"Show me incidents from the last 90 days\"** — Historical search\n\n"
        "Or select an alert from the sidebar for detailed investigation."
    )


def _execute_quick_action(
    message: str,
    active_alert: Optional[ParsedFailure],
    analysis_result: Any,
    triage_engine: Any = None,
) -> None:
    """
    Execute a quick-action button click: add the user message,
    generate the AI response, append both, then rerun to refresh UI.

    Includes duplicate-click guard to prevent the same quick action
    from being submitted twice in rapid succession.
    """
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Duplicate-click guard: skip if last user message is identical
    if st.session_state.messages:
        last_user_msgs = [
            m for m in st.session_state.messages if m["role"] == "user"
        ]
        if last_user_msgs and last_user_msgs[-1]["content"] == message:
            return  # Already submitted this exact quick action

    # Add user message
    st.session_state.messages.append({"role": "user", "content": message})

    # Generate and add assistant response immediately
    response = _generate_response(message, active_alert, analysis_result, triage_engine)
    st.session_state.messages.append({"role": "assistant", "content": response})

    # Rerun to display both messages in the chat
    st.rerun()


def _generate_response(
    user_message: str,
    active_alert: Optional[ParsedFailure],
    analysis_result: Any,
    triage_engine: Any = None,
) -> str:
    """
    Generate a response using the chat engine.
    Falls back to demo responses when the engine is not configured.

    Appends a data-source tag so the user always knows whether they
    are seeing demo data or live MCP data.
    """
    # Check if chat engine is configured
    chat_engine = st.session_state.get("chat_engine")

    if chat_engine:
        session_id = st.session_state.get("chat_session_id")
        if session_id:
            try:
                loop = asyncio.new_event_loop()
                response = loop.run_until_complete(
                    chat_engine.process_message(session_id, user_message)
                )
                loop.close()
                return _append_data_source_tag(response)
            except Exception as e:
                return f"Error processing message: {str(e)}"

    # Demo mode responses — queries the triage engine for real data
    response = _demo_response(user_message, active_alert, analysis_result, triage_engine)
    return _append_data_source_tag(response)


def _append_data_source_tag(response: str) -> str:
    """
    Append data source tag to every chat response.
    Uses the DataRefreshEngine if available, otherwise defaults to 'Demo'.
    """
    refresh_engine = st.session_state.get("refresh_engine")
    if refresh_engine:
        tag = refresh_engine.data_source_tag
    else:
        tag = "_Data source: Demo_"
    return f"{response}\n\n---\n{tag}"


def _demo_response(
    message: str,
    alert: Optional[ParsedFailure],
    analysis: Any,
    triage_engine: Any = None,
) -> str:
    """
    Generate demo responses that query the actual triage engine.

    All intents pull live data from triage_engine when available:
    - ALERT_DISCOVERY: get_top_clusters()
    - PLATFORM_HEALTH: get_pipeline_health()
    - ALERT_SEARCH: search_alerts(), get_alerts_by_severity()
    - CROSS_ALERT_ANALYTICS: get_cross_alert_analytics()
    - INCIDENTS (historical): st.session_state["demo_incidents"]
    - Time-window: parses hours, days, weeks; defaults to 1 hour for discovery
    """
    msg_lower = message.lower()

    # ─── Time-window + Incidents / Historical ──────
    # IMPORTANT: Check this BEFORE the generic discovery match.
    # "incidents in the last 1 hour" / "issues in the last 2 hours" must
    # parse the time window correctly and show both active alerts + history.
    # "issues" == "incidents" == "problems" == "errors" — all synonyms.
    if any(kw in msg_lower for kw in [
        "incidents", "issues", "problems",
        "last 90 days", "last 30 days", "last 7 days",
        "this week", "this month", "historical",
        "created in", "happened in", "occurred in",
    ]):
        return _demo_incidents_timewindow_response(msg_lower, alert, triage_engine)

    # ─── Discovery Intent (no alert needed) ──────
    if any(kw in msg_lower for kw in [
        "what's broken", "what failed", "what is broken", "what is failing",
        "any failures", "active failures", "going on",
    ]):
        return _demo_discovery_response(msg_lower, triage_engine)

    # ─── Platform Health (no alert needed) ────────
    if any(kw in msg_lower for kw in [
        "platform health", "system health", "health report", "healthy",
    ]):
        return _demo_health_response(msg_lower, triage_engine)

    # ─── Alert Search (no alert needed) ──────────
    if any(kw in msg_lower for kw in [
        "show me all", "find all", "list all", "search for",
        "show critical", "show high",
    ]):
        return _demo_search_response(msg_lower, triage_engine)

    # ─── Cross-Alert Analytics (no alert needed) ─
    if any(kw in msg_lower for kw in [
        "analytics", "compare", "recurring", "distribution",
        "top errors", "top failures", "how many",
    ]):
        return _demo_analytics_response(msg_lower, triage_engine)

    # ─── Pipeline-specific detail intent (no sidebar selection needed) ─
    # Catches: "details about kusto_ingestion_telemetry",
    #          "tell me more about spark_etl_daily_ingest",
    #          "what's wrong with synapse_pipeline_customer360",
    #          or simply mentioning a pipeline name that exists in the triage engine.
    # This MUST come before alert-bound intents so that a pipeline name in the
    # message is resolved even when no sidebar alert is selected.
    pipeline_name = _extract_pipeline_from_message(msg_lower)
    if pipeline_name and triage_engine:
        detail_response = _demo_pipeline_detail_response(pipeline_name, triage_engine)
        if detail_response:
            return detail_response

    # ─── Alert-bound intents ─────────────────────

    if any(kw in msg_lower for kw in ["similar", "past", "before"]):
        if alert:
            return _demo_similar_response(alert, triage_engine)
        # No alert — redirect to discovery
        return _demo_discovery_response(msg_lower, triage_engine)

    if any(kw in msg_lower for kw in ["runbook", "steps", "procedure", "how to fix"]):
        return _demo_runbook_response(alert)

    if any(kw in msg_lower for kw in ["root cause", "why", "explain", "what caused"]):
        if alert:
            return _demo_rootcause_response(alert)
        return (
            "No active alert selected. You can:\n"
            "- Ask **\"What's broken?\"** to discover current issues\n"
            "- Ask about a specific pipeline: **\"Why is spark_etl_daily_ingest failing?\"**\n"
            "- Select an alert from the sidebar for detailed root cause analysis"
        )

    if any(kw in msg_lower for kw in ["log", "trace", "stack"]):
        if alert and alert.log_snippet:
            return f"**Log Output:**\n\n```\n{alert.log_snippet}\n```"
        return (
            "No active alert selected for log viewing. "
            "Select an alert from the sidebar, or ask **\"What's broken?\"** to find issues first."
        )

    if any(kw in msg_lower for kw in ["escalat", "page", "urgent"]):
        return (
            "**Escalation Options:**\n\n"
            "I can help you escalate this alert:\n\n"
            "1. **Send Teams notification** to the on-call channel (dp-oncall)\n"
            "2. **Page the escalation contacts** (alice@company.com, bob@company.com)\n"
            "3. **Create an ICM ticket** with Sev2 priority\n\n"
            "Which action would you like to take? You can also type "
            "'escalate to Teams' or 'create ICM ticket' to proceed."
        )

    # General response — context-aware
    if alert:
        return (
            f"I can help you investigate the **{alert.error_class}** failure in "
            f"**{alert.pipeline_name}**. Here's what I can do:\n\n"
            f"- **Similar incidents** — Find past occurrences and their resolutions\n"
            f"- **Runbook steps** — Get the troubleshooting procedure\n"
            f"- **Root cause** — Detailed analysis of what went wrong\n"
            f"- **Logs** — View the error logs and stack traces\n"
            f"- **Escalation** — Page the on-call team or create a ticket\n\n"
            f"What would you like to know?"
        )

    return (
        "I can help you with pipeline troubleshooting. Try asking:\n\n"
        "- **\"What's broken right now?\"** — Discover active issues\n"
        "- **\"Show me all critical alerts\"** — Search by severity\n"
        "- **\"Is spark_etl_daily_ingest healthy?\"** — Pipeline health\n"
        "- **\"What failed in the last hour?\"** — Time-windowed query\n"
        "- **\"Show me incidents from the last 90 days\"** — Historical lookup\n"
        "- **\"Compare yesterday vs today\"** — Cross-alert analytics\n\n"
        "Or select an alert from the sidebar for context-aware assistance."
    )


# ─── Demo Response Helpers (triage-engine backed) ─


def _demo_discovery_response(msg_lower: str, triage_engine: Any = None) -> str:
    """
    Discovery response: queries the SAME search_alerts() code path as
    the incidents/issues handler so that "What's broken?", "issues in the
    last 1 hour", and "incidents in the last 1 hour" all return
    identical results for the same time window.

    Default time window is 1 hour when no explicit window is specified.
    """
    hours = _extract_hours_from_message(msg_lower) or 1

    if triage_engine:
        try:
            # Use search_alerts with the parsed time window — same path
            # as _demo_incidents_timewindow_response.
            alerts = triage_engine.search_alerts(
                query="", hours_back=hours,
            )
            if not alerts:
                return (
                    f"**Active Issues** (last {hours}h):\n\n"
                    "No active alerts found in this time window. "
                    "The platform appears healthy.\n\n"
                    "Try broadening your time window: **\"What failed in the last 24 hours?\"**"
                )

            # Deduplicate by failure_id
            seen: set[str] = set()
            unique: list = []
            for a in alerts:
                if a.failure_id not in seen:
                    seen.add(a.failure_id)
                    unique.append(a)

            lines = [f"**Active Issues** (last {hours}h):\n"]
            critical_high = 0
            for i, a in enumerate(unique[:10], 1):
                icon = _SEV_ICON.get(a.severity, "⚪")
                time_str = a.timestamp.strftime("%H:%M UTC") if a.timestamp else ""
                lines.append(
                    f"**{i}. {a.pipeline_name}** — {a.error_class}"
                )
                lines.append(
                    f"   Severity: {icon} {a.severity.value.upper()} "
                    f"| Time: {time_str}"
                )
                if a.root_cause_summary:
                    lines.append(f"   Root cause: {a.root_cause_summary[:100]}")
                lines.append("")
                if a.severity in (Severity.CRITICAL, Severity.HIGH):
                    critical_high += 1

            lines.append(
                f"**Summary:** {len(unique)} active alert(s), "
                f"{critical_high} critical/high severity.\n"
                f"Select an alert for detailed investigation, or ask "
                f"\"tell me more about [pipeline_name]\" for specifics."
            )
            return "\n".join(lines)
        except Exception:
            pass  # Fall through to fallback

    # Fallback: use alerts from session state
    return _fallback_discovery_from_session(hours)


def _demo_health_response(msg_lower: str, triage_engine: Any = None) -> str:
    """
    Platform health response: pulls health data from the triage engine.
    Supports both overall health and per-pipeline health queries.
    """
    pipeline = _extract_pipeline_from_message(msg_lower)

    if triage_engine:
        try:
            if pipeline:
                health = triage_engine.get_pipeline_health(pipeline_name=pipeline)
                status = health.get("status", "unknown")
                status_icon = {"critical": "🔴", "degraded": "🟠", "warning": "🟡",
                               "elevated": "🟡", "healthy": "🟢"}.get(status, "⚪")
                alert_count = health.get("alert_count_24h", 0)
                latest_error = health.get("latest_error", "None")
                last_alert = health.get("last_alert")

                last_alert_str = "N/A"
                if last_alert:
                    try:
                        delta = datetime.now(timezone.utc) - last_alert
                        mins = int(delta.total_seconds() / 60)
                        last_alert_str = f"{mins} min ago" if mins < 120 else f"{mins // 60}h ago"
                    except Exception:
                        last_alert_str = str(last_alert)

                return (
                    f"**Pipeline Health: {pipeline}**\n\n"
                    f"Status: {status_icon} {status.upper()}\n"
                    f"Alerts (24h): {alert_count}\n"
                    f"Latest error: {latest_error}\n"
                    f"Last alert: {last_alert_str}\n\n"
                    f"Select the alert from the sidebar for detailed investigation, "
                    f"or ask \"show me similar incidents for this pipeline\"."
                )

            # Overall platform health
            health = triage_engine.get_pipeline_health()
            status = health.get("status", "unknown")
            status_icon = {"critical": "🔴", "degraded": "🟠", "elevated": "🟡",
                           "healthy": "🟢"}.get(status, "⚪")

            lines = [
                "**Platform Health Report**\n",
                f"Overall Status: {status_icon} {status.upper()}\n",
                f"- Alerts (last hour): {health.get('alerts_last_hour', 0)}",
                f"- Alerts (24h): {health.get('total_alerts_24h', 0)}",
                f"- Active clusters: {health.get('active_clusters', 0)}",
                f"- Critical: {health.get('critical_count', 0)}",
                f"- High: {health.get('high_count', 0)}",
            ]

            affected = health.get("affected_pipelines", [])
            if affected:
                lines.append(f"\n**Affected Pipelines:** {', '.join(affected)}")

            pipeline_details = health.get("pipeline_details", {})
            if pipeline_details:
                lines.append("\n**Per-Pipeline Status:**")
                for pname, phealth in sorted(
                    pipeline_details.items(),
                    key=lambda x: {"critical": 0, "degraded": 1, "elevated": 2,
                                   "warning": 3, "healthy": 4}.get(x[1].get("status", ""), 5),
                ):
                    pstatus = phealth.get("status", "unknown")
                    picon = {"critical": "🔴", "degraded": "🟠", "warning": "🟡",
                             "elevated": "🟡", "healthy": "🟢"}.get(pstatus, "⚪")
                    pcount = phealth.get("alert_count_24h", 0)
                    lines.append(f"  {picon} {pname} — {pstatus} ({pcount} alerts)")

            return "\n".join(lines)
        except Exception:
            pass

    # Fallback static
    return (
        "**Platform Health Report**\n\n"
        "Overall Status: ⚪ UNKNOWN\n\n"
        "Triage engine not available. Connect your MCP data sources to "
        "enable real-time health monitoring."
    )


def _demo_search_response(msg_lower: str, triage_engine: Any = None) -> str:
    """
    Alert search response: queries triage engine by severity or keyword.
    """
    if triage_engine:
        try:
            # Determine severity filter
            if "critical" in msg_lower:
                alerts = triage_engine.get_alerts_by_severity(Severity.CRITICAL)
                label = "critical"
            elif "high" in msg_lower:
                alerts = triage_engine.get_alerts_by_severity(Severity.HIGH)
                label = "high"
            elif "medium" in msg_lower:
                alerts = triage_engine.get_alerts_by_severity(Severity.MEDIUM)
                label = "medium"
            elif "low" in msg_lower:
                alerts = triage_engine.get_alerts_by_severity(Severity.LOW)
                label = "low"
            else:
                # General search — return all from top clusters
                alerts = []
                for cluster in triage_engine.get_top_clusters(limit=10):
                    alerts.extend(cluster.all_alerts)
                label = "all"

            if not alerts:
                return (
                    f"**Search Results** (0 {label} alerts found):\n\n"
                    "No matching alerts. Try a different severity or time window."
                )

            # Deduplicate by failure_id
            seen = set()
            unique = []
            for a in alerts:
                if a.failure_id not in seen:
                    seen.add(a.failure_id)
                    unique.append(a)

            lines = [f"**Search Results** ({len(unique)} {label} alert(s) found):\n"]
            for i, a in enumerate(unique[:10], 1):
                icon = _SEV_ICON.get(a.severity, "⚪")
                time_str = a.timestamp.strftime("%H:%M UTC") if a.timestamp else ""
                lines.append(
                    f"**{i}.** {icon} **{a.pipeline_name}** — {a.error_class}"
                )
                summary = a.root_cause_summary[:100] if a.root_cause_summary else a.error_message[:100]
                lines.append(f"   {summary}")
                lines.append(f"   Time: {time_str} | Severity: {a.severity.value.upper()}")
                lines.append("")

            if len(unique) > 10:
                lines.append(f"_Showing 10 of {len(unique)}. Refine your search to narrow down._")
            else:
                lines.append("Select an alert for detailed investigation, or refine your search.")
            return "\n".join(lines)
        except Exception:
            pass

    return (
        "**Search Results**\n\nTriage engine not available. "
        "Connect your MCP data sources to enable alert search."
    )


def _demo_analytics_response(msg_lower: str, triage_engine: Any = None) -> str:
    """
    Cross-alert analytics: patterns, recurring errors, severity distribution.
    """
    hours = _extract_hours_from_message(msg_lower) or 24

    if triage_engine:
        try:
            analytics = triage_engine.get_cross_alert_analytics(hours_back=hours)

            total = analytics.get("total_alerts", 0)
            if total == 0:
                return (
                    f"**Alert Analytics** (last {hours}h):\n\n"
                    "No alerts in the selected time window.\n\n"
                    "Try a wider window: **\"Show me analytics for the last 48 hours\"**"
                )

            sev = analytics.get("severity_distribution", {})
            top_errors = analytics.get("top_errors", [])
            top_pipelines = analytics.get("top_pipelines", [])
            patterns = analytics.get("patterns", [])
            cluster_count = analytics.get("cluster_count", 0)
            cascade_count = analytics.get("active_cascades", 0)

            lines = [
                f"**Alert Analytics** (last {hours}h):\n",
                f"**Total Alerts:** {total}",
                f"**Active Clusters:** {cluster_count}",
                f"**Active Cascades:** {cascade_count}\n",
                "**By Severity:**",
            ]
            for sev_name in ["critical", "high", "medium", "low"]:
                count = sev.get(sev_name, 0)
                if count > 0:
                    lines.append(f"  - {sev_name.upper()}: {count}")

            if top_errors:
                lines.append("\n**Top Error Classes:**")
                for error, count in top_errors:
                    lines.append(f"  - {error}: {count} occurrence(s)")

            if top_pipelines:
                lines.append("\n**Most Affected Pipelines:**")
                for pipeline, count in top_pipelines:
                    lines.append(f"  - {pipeline}: {count} alert(s)")

            if patterns:
                lines.append("\n**Detected Patterns:**")
                for p in patterns:
                    lines.append(f"  ⚠️ {p}")

            return "\n".join(lines)
        except Exception:
            pass

    return (
        f"**Alert Analytics** (last {hours}h):\n\n"
        "Triage engine not available. Connect your MCP data sources "
        "to enable cross-alert analytics."
    )


def _demo_incidents_timewindow_response(
    msg_lower: str,
    alert: Optional[ParsedFailure],
    triage_engine: Any = None,
) -> str:
    """
    Incidents / historical query with proper time-window parsing.

    Parses: hours, days, weeks, months. Defaults to 24 hours if no unit found.
    Uses demo_incidents from session state for historical data, and
    triage_engine.search_alerts() for recent alerts within the time window.
    """
    # ─── Parse time window (hours AND days) ──────
    hours_back = _parse_time_window(msg_lower)

    # Convert to human-readable label
    if hours_back < 24:
        time_label = f"{hours_back} hour{'s' if hours_back != 1 else ''}"
    elif hours_back < 168:
        days = hours_back // 24
        time_label = f"{days} day{'s' if days != 1 else ''}"
    else:
        days = hours_back // 24
        time_label = f"{days} days"

    pipeline_clause = ""
    pipeline_filter = None
    if alert:
        pipeline_clause = f" for **{alert.pipeline_name}**"
        pipeline_filter = alert.pipeline_name
    else:
        # No sidebar alert — extract pipeline name from the message itself
        mentioned_pipeline = _extract_pipeline_from_message(msg_lower)
        if mentioned_pipeline:
            pipeline_clause = f" for **{mentioned_pipeline}**"
            pipeline_filter = mentioned_pipeline

    # ─── Gather historical incidents from session state ──
    incidents = st.session_state.get("demo_incidents", [])
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)

    # Filter incidents by time window (and pipeline if bound)
    matched_incidents = []
    for inc in incidents:
        # Safe comparison: both should be naive UTC, but handle edge cases
        inc_ts = inc.occurred_at
        cmp_cutoff = cutoff
        if inc_ts is not None:
            if inc_ts.tzinfo is not None and cmp_cutoff.tzinfo is None:
                cmp_cutoff = cmp_cutoff.replace(tzinfo=timezone.utc)
            elif inc_ts.tzinfo is None and cmp_cutoff.tzinfo is not None:
                inc_ts = inc_ts.replace(tzinfo=timezone.utc)
            if inc_ts >= cmp_cutoff:
                if pipeline_filter is None or pipeline_filter.lower() in inc.pipeline_name.lower():
                    matched_incidents.append(inc)

    # ─── Also gather recent alerts from triage engine ──
    recent_alerts = []
    if triage_engine:
        try:
            recent_alerts = triage_engine.search_alerts(
                query="",
                pipeline_filter=pipeline_filter,
                hours_back=hours_back,
            )
        except Exception:
            pass

    # ─── Build response ──────────────────────────
    # Mirror the user's terminology (issues vs incidents)
    if "issues" in msg_lower:
        heading_noun = "Issues"
    elif "problems" in msg_lower:
        heading_noun = "Problems"
    else:
        heading_noun = "Incidents"
    lines = [f"**{heading_noun} in the last {time_label}**{pipeline_clause}:\n"]

    # Recent active alerts section
    if recent_alerts:
        # Deduplicate
        seen = set()
        unique_alerts = []
        for a in recent_alerts:
            if a.failure_id not in seen:
                seen.add(a.failure_id)
                unique_alerts.append(a)

        lines.append(f"**Active Alerts ({len(unique_alerts)}):**\n")
        for i, a in enumerate(unique_alerts[:5], 1):
            icon = _SEV_ICON.get(a.severity, "⚪")
            time_str = a.timestamp.strftime("%H:%M UTC") if a.timestamp else ""
            lines.append(
                f"**{i}.** {icon} **{a.pipeline_name}** — {a.error_class}"
            )
            lines.append(f"   Severity: {a.severity.value.upper()} | Time: {time_str}")
            if a.root_cause_summary:
                lines.append(f"   Root cause: {a.root_cause_summary[:100]}")
            lines.append("")
        if len(unique_alerts) > 5:
            lines.append(f"_...and {len(unique_alerts) - 5} more alerts._\n")

    # Historical incidents section
    if matched_incidents:
        lines.append(f"**Historical Incidents ({len(matched_incidents)}):**\n")
        for i, inc in enumerate(matched_incidents[:5], 1):
            days_ago = (datetime.utcnow() - inc.occurred_at).days
            lines.append(
                f"**{i}. [{inc.incident_id}] {inc.title}** — {days_ago} days ago"
            )
            lines.append(
                f"   Pipeline: {inc.pipeline_name} | Severity: {inc.severity.value.upper()}"
            )
            lines.append(f"   Root Cause: {inc.root_cause}")
            lines.append(f"   Resolution: {inc.resolution}")
            lines.append("")

    # If nothing found
    if not recent_alerts and not matched_incidents:
        lines.append(
            f"No {heading_noun.lower()} found in this time window.\n\n"
            f"Try a wider window: **\"Show me {heading_noun.lower()} in the last 90 days\"**"
        )
    else:
        total = len(recent_alerts) + len(matched_incidents)
        lines.append(
            f"_Total: {total} result(s). Ask for a specific pipeline or error type to filter._"
        )

    return "\n".join(lines)


def _demo_similar_response(
    alert: ParsedFailure,
    triage_engine: Any = None,
) -> str:
    """Similar incidents response using historical data from session state."""
    incidents = st.session_state.get("demo_incidents", [])

    # Find incidents matching pipeline or error class
    matches = []
    for inc in incidents:
        score = 0.0
        if inc.pipeline_name.lower() == alert.pipeline_name.lower():
            score += 0.5
        if inc.error_class.lower() == alert.error_class.lower():
            score += 0.4
        # Tag overlap
        alert_tags = set(getattr(alert, "tags", []) or [])
        inc_tags = set(inc.tags or [])
        if alert_tags and inc_tags:
            overlap = len(alert_tags & inc_tags) / max(len(alert_tags | inc_tags), 1)
            score += overlap * 0.1
        if score > 0.0:
            matches.append((inc, score))

    matches.sort(key=lambda x: x[1], reverse=True)

    if not matches:
        return (
            f"No similar historical incidents found for **{alert.pipeline_name}** "
            f"({alert.error_class}).\n\n"
            "This may be a new failure pattern. Consider creating an ICM ticket."
        )

    lines = [
        f"Found **{len(matches)} similar incident(s)** for pipeline "
        f"'{alert.pipeline_name}':\n"
    ]
    for i, (inc, score) in enumerate(matches[:5], 1):
        days_ago = (datetime.utcnow() - inc.occurred_at).days
        lines.append(
            f"**{i}. [{inc.incident_id}] {inc.title}** — {days_ago} days ago"
        )
        lines.append(f"   Pipeline: {inc.pipeline_name} | Severity: {inc.severity.value.upper()}")
        lines.append(f"   Root Cause: {inc.root_cause}")
        lines.append(f"   Resolution: {inc.resolution}")
        lines.append(f"   Similarity: {int(score * 100)}%")
        lines.append("")

    if matches:
        best = matches[0][0]
        lines.append(
            f"The most relevant fix is from **{best.incident_id}** — {best.resolution[:100]}"
        )

    return "\n".join(lines)


def _demo_runbook_response(alert: Optional[ParsedFailure]) -> str:
    """Runbook steps response — context-aware when alert is selected."""
    if alert:
        return (
            f"**Runbook Steps — {alert.error_class} Recovery:**\n\n"
            f"1. **Check executor metrics** — Open Spark UI → Executors tab → verify memory usage per executor\n"
            f"2. **Identify the skewed stage** — Look at stage details for uneven task durations (>5x variance)\n"
            f"3. **Check for data skew** — Run: `df.groupBy('join_key').count().orderBy(desc('count')).show(10)`\n"
            f"4. **Apply immediate fix** — Increase executor memory: `spark.executor.memory = 16g`\n"
            f"5. **Enable AQE** — Set `spark.sql.adaptive.enabled = true` and `spark.sql.adaptive.skewJoin.enabled = true`\n"
            f"6. **Rerun the pipeline** and monitor executor memory usage\n"
            f"7. **If persists** — Apply salting to the skewed join key\n\n"
            f"[Open full runbook](https://confluence.company.com/display/RUNBOOKS/RUNBOOKS-101)"
        )

    return (
        "**Runbook Steps — OOM Error Recovery:**\n\n"
        "1. **Check executor metrics** — Open Spark UI → Executors tab → verify memory usage per executor\n"
        "2. **Identify the skewed stage** — Look at stage details for uneven task durations (>5x variance)\n"
        "3. **Check for data skew** — Run: `df.groupBy('join_key').count().orderBy(desc('count')).show(10)`\n"
        "4. **Apply immediate fix** — Increase executor memory: `spark.executor.memory = 16g`\n"
        "5. **Enable AQE** — Set `spark.sql.adaptive.enabled = true` and `spark.sql.adaptive.skewJoin.enabled = true`\n"
        "6. **Rerun the pipeline** and monitor executor memory usage\n"
        "7. **If persists** — Apply salting to the skewed join key\n\n"
        "[Open full runbook](https://confluence.company.com/display/RUNBOOKS/RUNBOOKS-101)"
    )


def _demo_rootcause_response(alert: ParsedFailure) -> str:
    """Detailed root cause analysis for the active alert."""
    return (
        f"**Root Cause Analysis for {alert.pipeline_name}:**\n\n"
        f"**Summary:** {alert.root_cause_summary}\n\n"
        f"**Chain of Thought Analysis:**\n\n"
        f"1. **Error Type:** {alert.error_class} — This is a memory exhaustion error in the JVM heap space.\n\n"
        f"2. **Failure Chain:** The executor's memory was exhausted during a shuffle operation (stage 12). "
        f"The UnsafeRow.copy() call in the stack trace indicates data was being serialized for a shuffle write, "
        f"and the partition was too large to fit in memory.\n\n"
        f"3. **Root Cause:** Data skew in the join key is causing one partition to be significantly larger "
        f"than others. The top 1% of customer IDs contain approximately 50% of all transactions, meaning "
        f"one executor has to process a disproportionate share of the data.\n\n"
        f"4. **Blast Radius:** The daily ingest pipeline is blocked, which delays all downstream analytics "
        f"tables and the daily reporting dashboard.\n\n"
        f"**Confidence:** 88% — This pattern matches [INC-2024-0847] closely."
    )


def _demo_pipeline_detail_response(
    pipeline_name: str,
    triage_engine: Any,
) -> Optional[str]:
    """
    Pipeline-specific detail response — triggered when the user mentions a
    pipeline name anywhere in their message, even without selecting it from
    the sidebar.

    Covers the common conversational pattern:
      User sees a list of alerts → asks "tell me more about <pipeline_name>"
      or "details about <pipeline_name>" or "what's wrong with <pipeline>"

    Pulls data from:
      - triage_engine.get_pipeline_health(pipeline_name) — health status
      - triage_engine.search_alerts(pipeline_filter=pipeline_name) — recent alerts
      - session_state["demo_incidents"] — historical incidents for that pipeline

    Returns None if the pipeline has zero alerts (lets the fallback handle it).
    """
    # 1. Search for alerts matching this pipeline
    alerts = triage_engine.search_alerts(
        query="", pipeline_filter=pipeline_name, hours_back=24,
    )
    if not alerts:
        # No alerts for this pipeline — return None so the router continues
        return None

    # Deduplicate
    seen: set[str] = set()
    unique_alerts: list = []
    for a in alerts:
        if a.failure_id not in seen:
            seen.add(a.failure_id)
            unique_alerts.append(a)

    # Pick the most critical alert as the "primary"
    sev_order = {
        Severity.CRITICAL: 0, Severity.HIGH: 1,
        Severity.MEDIUM: 2, Severity.LOW: 3,
    }
    unique_alerts.sort(key=lambda a: sev_order.get(a.severity, 99))
    primary = unique_alerts[0]

    # 2. Pipeline health
    try:
        health = triage_engine.get_pipeline_health(pipeline_name=pipeline_name)
        status = health.get("status", "unknown")
        status_icon = {
            "critical": "🔴", "degraded": "🟠", "warning": "🟡",
            "elevated": "🟡", "healthy": "🟢",
        }.get(status, "⚪")
    except Exception:
        status, status_icon = "unknown", "⚪"

    # 3. Build the detail response
    lines = [
        f"**Pipeline Detail: {primary.pipeline_name}**\n",
        f"**Status:** {status_icon} {status.upper()}",
        f"**Active Alerts (24h):** {len(unique_alerts)}\n",
    ]

    # Primary / most critical alert
    icon = _SEV_ICON.get(primary.severity, "⚪")
    time_str = primary.timestamp.strftime("%H:%M UTC") if primary.timestamp else ""
    lines.append("---")
    lines.append(f"**Most Critical Alert:**\n")
    lines.append(
        f"{icon} **{primary.error_class}** — "
        f"{primary.severity.value.upper()} | {time_str}"
    )
    lines.append(f"\n**Root Cause:** {primary.root_cause_summary}\n")

    if primary.error_message:
        # Truncate long error messages
        err_msg = primary.error_message[:200]
        if len(primary.error_message) > 200:
            err_msg += "..."
        lines.append(f"**Error Message:** `{err_msg}`\n")

    if primary.affected_components:
        lines.append(
            f"**Affected Components:** {', '.join(primary.affected_components)}\n"
        )

    if primary.log_snippet:
        snippet = primary.log_snippet[:400]
        if len(primary.log_snippet) > 400:
            snippet += "\n..."
        lines.append(f"**Log Snippet:**\n```\n{snippet}\n```\n")

    # Additional alerts for same pipeline
    if len(unique_alerts) > 1:
        lines.append(f"**Other Alerts for this Pipeline ({len(unique_alerts) - 1}):**\n")
        for i, a in enumerate(unique_alerts[1:5], 1):
            a_icon = _SEV_ICON.get(a.severity, "⚪")
            a_time = a.timestamp.strftime("%H:%M UTC") if a.timestamp else ""
            lines.append(
                f"{i}. {a_icon} {a.error_class} — "
                f"{a.severity.value.upper()} | {a_time}"
            )
        if len(unique_alerts) > 5:
            lines.append(f"_...and {len(unique_alerts) - 5} more._")
        lines.append("")

    # 4. Historical incidents for this pipeline
    incidents = st.session_state.get("demo_incidents", [])
    matching_incidents = [
        inc for inc in incidents
        if pipeline_name.lower() in inc.pipeline_name.lower()
        or primary.error_class.lower() == inc.error_class.lower()
    ]
    if matching_incidents:
        lines.append(f"**Similar Past Incidents ({len(matching_incidents)}):**\n")
        for i, inc in enumerate(matching_incidents[:3], 1):
            days_ago = (datetime.utcnow() - inc.occurred_at).days
            lines.append(
                f"{i}. **[{inc.incident_id}] {inc.title}** — {days_ago} days ago"
            )
            lines.append(f"   Resolution: {inc.resolution}")
        lines.append("")

    # 5. Actionable next steps
    lines.append("**What would you like to do next?**\n")
    lines.append(
        "- **\"Show me the runbook\"** — Get troubleshooting steps\n"
        "- **\"Escalate this\"** — Page the on-call team\n"
        "- **\"Similar incidents\"** — Find past occurrences and their resolutions\n"
        "- **\"What caused this?\"** — Detailed root cause analysis\n"
        "- Or select this alert from the sidebar for full investigation mode"
    )

    return "\n".join(lines)


# ─── Time Parsing ─────────────────────────────────


def _parse_time_window(msg: str) -> int:
    """
    Parse a time window from the user message and return hours.

    Supports: hours, days, weeks, months.
    Examples:
      "last 1 hour"  → 1
      "last 3 hours" → 3
      "last 7 days"  → 168
      "last 90 days" → 2160
      "this week"    → 168
      "this month"   → 720

    Returns hours_back (default: 24 if no time expression found).
    """
    msg_lower = msg.lower()

    # Check hours first (most granular)
    match = re.search(r"(\d+)\s*hours?", msg_lower)
    if match:
        return int(match.group(1))

    # "last hour" / "past hour" (no digit)
    if re.search(r"(last|past)\s+hour\b", msg_lower):
        return 1

    # Check days
    match = re.search(r"(\d+)\s*days?", msg_lower)
    if match:
        return int(match.group(1)) * 24

    # Check weeks
    match = re.search(r"(\d+)\s*weeks?", msg_lower)
    if match:
        return int(match.group(1)) * 168

    # "this week"
    if "this week" in msg_lower:
        return 168

    # "this month"
    if "this month" in msg_lower:
        return 720

    # Check months
    match = re.search(r"(\d+)\s*months?", msg_lower)
    if match:
        return int(match.group(1)) * 720

    # Default: 24 hours
    return 24


def _extract_hours_from_message(msg: str) -> Optional[int]:
    """Extract hours from message text (for discovery/analytics intents)."""
    match = re.search(r"(\d+)\s*hours?", msg)
    if match:
        return int(match.group(1))
    if "last hour" in msg:
        return 1
    # Also check days and convert
    match = re.search(r"(\d+)\s*days?", msg)
    if match:
        return int(match.group(1)) * 24
    return None


def _extract_pipeline_from_message(msg: str) -> Optional[str]:
    """
    Extract a pipeline name from message text.

    Scans ALL matches (not just the first) because ordinary English words
    like "details", "provide", "happening" can match the basic pattern
    first and shadow the real pipeline name deeper in the sentence.

    A pipeline name must contain at least one underscore (e.g.
    kusto_ingestion_telemetry, spark_etl_daily_ingest).
    """
    stop_words = {
        "platform", "healthy", "system", "health", "report",
        "status", "overall", "everything", "pipeline", "details",
        "provide", "something", "incidents", "anything",
    }
    # Find ALL word-like tokens with 6+ characters
    for match in re.finditer(r"\b([a-z][a-z0-9_]{5,})\b", msg):
        candidate = match.group(1)
        if candidate not in stop_words and "_" in candidate:
            return candidate
    return None


def _fallback_discovery_from_session(hours: int) -> str:
    """
    Fallback discovery using alerts stored in session state
    when triage engine is not available.
    """
    alerts = st.session_state.get("demo_alerts", [])
    if not alerts:
        return (
            f"**Active Issues** (last {hours}h):\n\n"
            "No alerts available. Connect your MCP data sources to "
            "enable real-time alert discovery."
        )

    # Group by pipeline
    from collections import defaultdict
    by_pipeline: dict[str, list] = defaultdict(list)
    for a in alerts:
        by_pipeline[a.pipeline_name].append(a)

    # Sort by max severity
    sev_order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
    sorted_pipelines = sorted(
        by_pipeline.items(),
        key=lambda x: min(sev_order.get(a.severity, 4) for a in x[1]),
    )

    lines = [f"**Active Issues** (last {hours}h):\n"]
    for i, (pipeline, pipeline_alerts) in enumerate(sorted_pipelines[:10], 1):
        top_alert = min(pipeline_alerts, key=lambda a: sev_order.get(a.severity, 4))
        icon = _SEV_ICON.get(top_alert.severity, "⚪")
        lines.append(
            f"**{i}. {pipeline}** — {top_alert.error_class}"
        )
        lines.append(
            f"   Severity: {icon} {top_alert.severity.value.upper()} "
            f"| Alerts: {len(pipeline_alerts)}"
        )
        lines.append("")

    lines.append(
        f"**Summary:** {len(sorted_pipelines)} affected pipeline(s).\n"
        "Select an alert for detailed investigation."
    )
    return "\n".join(lines)


def _export_investigation(alert: ParsedFailure) -> None:
    """Export the current investigation as a Markdown document."""
    try:
        from src.utils.export_markdown import export_investigation_markdown

        messages = st.session_state.get("messages", [])
        # Build a simple transcript from session messages
        transcript_lines = []
        for msg in messages:
            role = "Engineer" if msg["role"] == "user" else "Alert Whisperer"
            transcript_lines.append(f"**{role}:** {msg['content']}")

        transcript = "\n\n".join(transcript_lines)

        md_content = export_investigation_markdown(
            alert=alert,
            analysis_notes=f"## Chat Summary\n\n{transcript}",
            include_full_chat=False,  # We include chat in analysis_notes instead
        )

        st.download_button(
            label="Download Markdown Report",
            data=md_content,
            file_name=f"investigation_{alert.failure_id}.md",
            mime="text/markdown",
            key="download_md_report",
        )
    except Exception as e:
        st.error(f"Export failed: {str(e)}")
