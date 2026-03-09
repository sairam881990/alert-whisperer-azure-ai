"""
Settings page — connection management, data refresh config, and system info.

Includes:
- MCP server connection forms + test buttons
- Data refresh interval selector (30s / 1m / 5m / Manual)
- Auto-refresh toggle
- Connector status dashboard with last-poll timestamps
- LLM configuration
- Pipeline ownership routing
"""

from __future__ import annotations

import streamlit as st

from src.engine.data_refresh import DataRefreshEngine, DataSourceMode, RefreshInterval


def render_settings() -> None:
    """Render the settings and configuration page."""

    st.markdown("## Settings")

    # ─── Data Refresh Configuration ──────────
    refresh_engine: DataRefreshEngine | None = st.session_state.get("refresh_engine")
    if refresh_engine:
        _render_refresh_settings(refresh_engine)
        st.divider()

    # ─── MCP Server Connections ─────────────
    st.markdown("### MCP Server Connections")
    st.markdown("Configure and test connections to data sources.")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### Kusto / ADX")
        kusto_url = st.text_input("MCP Server URL", value="http://localhost:3001", key="set_kusto_url")
        kusto_db = st.text_input("Database", value="", key="set_kusto_db", placeholder="YourDatabase")
        if st.button("Test Connection", key="btn_test_kusto"):
            st.info("Connection test: Demo mode — not connected")

        st.divider()

        st.markdown("#### ICM")
        icm_url = st.text_input("MCP Server URL", value="http://localhost:3003", key="set_icm_url")
        if st.button("Test Connection", key="btn_test_icm"):
            st.info("Connection test: Demo mode — not connected")

    with col2:
        st.markdown("#### Confluence")
        conf_url = st.text_input("MCP Server URL", value="http://localhost:3002", key="set_conf_url")
        conf_base = st.text_input("Base URL", value="", key="set_conf_base", placeholder="https://company.atlassian.net/wiki")
        if st.button("Test Connection", key="btn_test_confluence"):
            st.info("Connection test: Demo mode — not connected")

        st.divider()

        st.markdown("#### Log Analytics")
        la_url = st.text_input("MCP Server URL", value="http://localhost:3004", key="set_la_url")
        la_workspace = st.text_input("Workspace ID", value="", key="set_la_workspace")
        if st.button("Test Connection", key="btn_test_la"):
            st.info("Connection test: Demo mode — not connected")

    st.divider()

    # LLM Configuration
    st.markdown("### LLM Configuration")

    col1, col2 = st.columns(2)
    with col1:
        llm_provider = st.selectbox(
            "Provider",
            options=["azure_openai", "openai"],
            key="set_llm_provider",
        )
        llm_model = st.text_input("Model / Deployment", value="gpt-4o", key="set_llm_model")
        llm_temp = st.slider("Temperature", 0.0, 1.0, 0.1, key="set_llm_temp")

    with col2:
        llm_endpoint = st.text_input("API Endpoint", value="", key="set_llm_endpoint", type="password")
        llm_key = st.text_input("API Key", value="", key="set_llm_key", type="password")
        llm_max_tokens = st.number_input("Max Tokens", value=4096, min_value=256, max_value=128000, key="set_llm_tokens")

    st.divider()

    # Alert Routing
    st.markdown("### Alert Routing")

    st.markdown("#### Pipeline Ownership")
    st.markdown("Configure which team owns each pipeline for automatic alert routing.")

    ownership_data = {
        "Pipeline": [
            "spark_etl_daily_ingest",
            "synapse_pipeline_customer360",
            "kusto_ingestion_telemetry",
            "spark_streaming_clickstream",
            "synapse_dw_refresh",
        ],
        "Owner": [
            "data-platform-team",
            "analytics-team",
            "telemetry-team",
            "data-platform-team",
            "data-warehouse-team",
        ],
        "On-Call Channel": [
            "dp-oncall",
            "analytics-oncall",
            "telemetry-oncall",
            "dp-oncall",
            "dw-oncall",
        ],
        "Default Severity": [
            "high",
            "critical",
            "medium",
            "critical",
            "high",
        ],
    }
    st.dataframe(ownership_data, use_container_width=True)

    st.divider()

    # Teams Integration
    st.markdown("### Microsoft Teams")
    teams_webhook = st.text_input(
        "Incoming Webhook URL",
        value="",
        key="set_teams_webhook",
        type="password",
        placeholder="https://outlook.office.com/webhook/...",
    )
    teams_test = st.button("Send Test Notification", key="btn_test_teams")
    if teams_test:
        st.info("Test notification sent (demo mode)")

    st.divider()

    # System Info
    st.markdown("### System Information")
    st.json({
        "version": "1.0.0",
        "environment": "development",
        "python_version": "3.10+",
        "vector_store": "ChromaDB (local)",
        "llm_provider": llm_provider,
        "mcp_protocol_version": "2024-11-05",
    })


def _render_refresh_settings(engine: DataRefreshEngine) -> None:
    """
    Render the Data Refresh configuration section.

    Shows:
    - Current data source mode (Demo / Live / Mixed)
    - Refresh interval selector
    - Auto-refresh toggle
    - Last refresh timestamp
    - Per-connector status table
    """
    st.markdown("### Data Refresh")

    # Mode badge
    mode_badge = {
        DataSourceMode.DEMO: ("📋 Demo Data", "info"),
        DataSourceMode.LIVE: ("🟢 Live (MCP Connected)", "success"),
        DataSourceMode.MIXED: ("🟡 Mixed (Partial MCP)", "warning"),
    }
    badge_text, _ = mode_badge.get(engine.mode, ("⚪ Unknown", "info"))
    st.markdown(f"**Data Source Mode:** {badge_text}")

    if engine.mode == DataSourceMode.DEMO:
        st.caption(
            "Running on demo data. Connect your MCP servers above to "
            "enable live data with automatic refresh."
        )
    else:
        st.caption(
            "Live data is refreshed automatically based on the interval below. "
            "Each Streamlit interaction checks if a refresh is due."
        )

    col1, col2, col3 = st.columns(3)

    with col1:
        # Refresh interval selector
        interval_options = {
            "30 seconds (real-time)": RefreshInterval.REALTIME,
            "1 minute (recommended)": RefreshInterval.STANDARD,
            "5 minutes (low frequency)": RefreshInterval.LOW,
            "Manual only": RefreshInterval.MANUAL,
        }
        current_label = next(
            (lbl for lbl, val in interval_options.items() if val == engine.refresh_interval),
            "1 minute (recommended)",
        )
        selected_label = st.selectbox(
            "Refresh Interval",
            options=list(interval_options.keys()),
            index=list(interval_options.keys()).index(current_label),
            key="set_refresh_interval",
        )
        new_interval = interval_options[selected_label]
        if new_interval != engine.refresh_interval:
            engine.refresh_interval = new_interval
            st.toast(f"Refresh interval set to {selected_label}")

    with col2:
        # Last refresh timestamp
        if engine.last_refresh_ts:
            st.metric(
                "Last Refresh",
                engine.last_refresh_ts.strftime("%H:%M:%S UTC"),
            )
        else:
            st.metric("Last Refresh", "Never")

    with col3:
        # Stats
        st.metric("Total Refreshes", engine.total_refreshes)
        st.metric("Alerts Ingested", engine.total_alerts_ingested)

    # Per-connector status
    st.markdown("#### Connector Status")
    for name, cs in engine.connectors.items():
        st.markdown(
            f"{cs.status_icon} **{name}** — {cs.status_label}"
            f"{'  |  Polls: ' + str(cs.total_polls) + '  |  Alerts: ' + str(cs.alerts_fetched) if cs.connected else ''}"
        )
