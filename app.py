"""
Alert Whisperer — Streamlit Application Entry Point.

Real-Time Failure Detection, Routing & Conversational Troubleshooting
for Spark, Synapse, and Kusto pipeline operations.

Enhanced with:
- AlertTriageEngine: aggregation layer between MCP data and UI
- Alert-free chat mode: discovery, health checks, search, analytics
- Proactive surfacing of critical clusters in chat greeting
"""

from __future__ import annotations

import streamlit as st

# Page config must be first Streamlit command
st.set_page_config(
    page_title="Alert Whisperer",
    page_icon="🔔",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "About": "Alert Whisperer v1.0 — GenAI-powered pipeline troubleshooting",
    },
)

from src.engine.alert_dedup import AlertDeduplicator
from src.engine.alert_triage import AlertTriageEngine
from src.engine.data_refresh import DataRefreshEngine, DataSourceMode, RefreshInterval
from src.models import ParsedFailure
from src.utils.demo_data import (
    generate_demo_alerts,
    generate_demo_feed,
    generate_demo_incidents,
    generate_demo_metrics,
)
from ui.components.alert_detail import render_alert_detail
from ui.components.chat_panel import render_chat_panel
from ui.components.dashboard import render_dashboard
from ui.components.knowledge_base import render_knowledge_base
from ui.components.rag_observability import render_rag_observability
from ui.components.settings_page import render_settings
from ui.components.sidebar import render_sidebar


# ─────────────────────────────────────────────────
# Custom CSS
# ─────────────────────────────────────────────────

st.markdown(
    """
    <style>
    /* Global styling */
    .stApp {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    /* Sidebar styling */
    section[data-testid="stSidebar"] {
        background-color: #0E1117;
        border-right: 1px solid rgba(128,128,128,0.2);
    }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background: rgba(128,128,128,0.08);
        padding: 0.75rem;
        border-radius: 8px;
    }

    /* Chat messages */
    .stChatMessage {
        border-radius: 12px;
    }

    /* Buttons */
    .stButton > button {
        border-radius: 6px;
        font-size: 0.85rem;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.5rem;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 6px 6px 0 0;
        padding: 0.5rem 1rem;
    }

    /* Expanders */
    .streamlit-expanderHeader {
        font-size: 0.95rem;
        font-weight: 500;
    }

    /* Code blocks */
    .stCodeBlock {
        border-radius: 8px;
    }

    /* Dividers */
    hr {
        margin: 0.75rem 0;
        border-color: rgba(128,128,128,0.2);
    }

    /* Hide default Streamlit elements */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────
# Triage Engine Initialization
# ─────────────────────────────────────────────────

def _init_triage_engine(alerts: list[ParsedFailure]) -> AlertTriageEngine:
    """
    Initialize the triage engine with deduplicator and ingest demo alerts.

    In production, this would connect to the live AlertProcessor output.
    For demo mode, we seed it with the demo alerts so the chat and sidebar
    can display triage clusters, severity ranking, and health status.
    """
    dedup = AlertDeduplicator(time_window_minutes=60)
    triage = AlertTriageEngine(deduplicator=dedup)
    triage.ingest_alerts(alerts)
    return triage


# ─────────────────────────────────────────────────
# Session State Initialization
# ─────────────────────────────────────────────────

def init_session_state() -> None:
    """Initialize all session state variables."""
    defaults = {
        "current_page": "Dashboard",
        "selected_alert_id": None,
        "messages": [],
        "demo_alerts": generate_demo_alerts(),
        "demo_feed": generate_demo_feed(),
        "demo_metrics": generate_demo_metrics(),
        "demo_incidents": generate_demo_incidents(),
        "connections": {
            "Kusto MCP": False,
            "Confluence MCP": False,
            "ICM MCP": False,
            "Log Analytics MCP": False,
            "Azure OpenAI": False,
        },
        "chat_engine": None,
        "chat_session_id": None,
        "kb_stats": {
            "total_documents": 247,
            "by_source": {"confluence": 89, "icm": 112, "log_analytics": 46},
        },
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # Initialize triage engine (seeded with demo alerts)
    if "triage_engine" not in st.session_state:
        st.session_state["triage_engine"] = _init_triage_engine(
            st.session_state["demo_alerts"]
        )

    # Initialize data refresh engine
    if "refresh_engine" not in st.session_state:
        st.session_state["refresh_engine"] = DataRefreshEngine(
            triage_engine=st.session_state["triage_engine"]
        )


init_session_state()

# ─────────────────────────────────────────────────
# Data Refresh — check on every rerun
# ─────────────────────────────────────────────────

refresh_engine: DataRefreshEngine = st.session_state["refresh_engine"]
new_alerts = refresh_engine.maybe_refresh()
if new_alerts:
    # New live alerts were ingested — force sidebar feed refresh
    st.session_state["demo_alerts"] = (
        st.session_state["demo_alerts"] + new_alerts
    )
    st.toast(f"🔄 {len(new_alerts)} new alert(s) ingested from MCP", icon="✅")


# ─────────────────────────────────────────────────
# Sidebar & Navigation
# ─────────────────────────────────────────────────

selected_alert_id = render_sidebar(
    st.session_state.demo_feed,
    triage_engine=st.session_state.get("triage_engine"),
)

# Resolve selected alert
selected_alert: ParsedFailure | None = None
if selected_alert_id:
    for alert in st.session_state.demo_alerts:
        if alert.failure_id == selected_alert_id:
            selected_alert = alert
            break


# ─────────────────────────────────────────────────
# Main Content Routing
# ─────────────────────────────────────────────────

current_page = st.session_state.get("current_page", "Dashboard")

if current_page == "Dashboard":
    render_dashboard(st.session_state.demo_metrics)

    # If an alert is selected, show detail below dashboard
    if selected_alert:
        st.divider()
        # Find matching incidents for this alert
        matching_incidents = [
            inc for inc in st.session_state.demo_incidents
            if inc.pipeline_name == selected_alert.pipeline_name
            or inc.error_class == selected_alert.error_class
        ]
        render_alert_detail(
            alert=selected_alert,
            similar_incidents=matching_incidents,
        )

elif current_page == "Alert Feed":
    st.markdown("## Alert Feed")
    st.markdown("All recent alerts with filtering and search.")

    # Search bar
    search = st.text_input("Search alerts", placeholder="Pipeline name, error class, or keyword...", key="feed_search")

    # Alerts table
    feed = st.session_state.demo_feed
    if search:
        feed = [
            item for item in feed
            if search.lower() in item.failure.pipeline_name.lower()
            or search.lower() in item.failure.error_class.lower()
            or search.lower() in item.failure.error_message.lower()
        ]

    for item in feed:
        alert = item.failure
        severity_icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        icon = severity_icons.get(alert.severity.value, "⚪")

        with st.expander(
            f"{icon} {alert.pipeline_name} — {alert.error_class} "
            f"({alert.severity.value.upper()}) — "
            f"{alert.timestamp.strftime('%H:%M UTC')}",
        ):
            render_alert_detail(
                alert=alert,
                similar_incidents=[
                    inc for inc in st.session_state.demo_incidents
                    if inc.pipeline_name == alert.pipeline_name
                    or inc.error_class == alert.error_class
                ],
            )

elif current_page == "Chat":
    render_chat_panel(
        active_alert=selected_alert,
        triage_engine=st.session_state.get("triage_engine"),
    )

elif current_page == "Knowledge Base":
    render_knowledge_base()

elif current_page == "RAG Observability":
    render_rag_observability()

elif current_page == "Settings":
    render_settings()

else:
    st.error(f"Unknown page: {current_page}")
