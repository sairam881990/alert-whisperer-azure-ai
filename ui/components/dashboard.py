"""Dashboard page component — metrics, charts, and alert overview."""

from __future__ import annotations

from typing import Any

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.models import DashboardMetrics


def render_dashboard(metrics: DashboardMetrics) -> None:
    """Render the main dashboard with metrics and visualizations."""

    st.markdown("## Operations Dashboard")
    st.markdown("Real-time overview of pipeline health and alert status.")

    # ─── KPI Cards ──────────────────────────────
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            label="Alerts (24h)",
            value=metrics.total_alerts_24h,
            delta=f"-{metrics.auto_resolved_count} auto-resolved",
            delta_color="normal",
        )

    with col2:
        st.metric(
            label="Critical",
            value=metrics.critical_count,
            delta="Needs attention" if metrics.critical_count > 0 else "Clear",
            delta_color="inverse" if metrics.critical_count > 0 else "normal",
        )

    with col3:
        st.metric(
            label="Avg TTD",
            value=f"{metrics.avg_ttd_minutes:.1f} min",
            help="Average Time to Detect",
        )

    with col4:
        st.metric(
            label="Avg TTM",
            value=f"{metrics.avg_ttm_minutes:.1f} min",
            help="Average Time to Mitigate",
        )

    st.divider()

    # ─── Charts ─────────────────────────────────
    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.markdown("#### Alerts by Severity")
        severity_data = {
            "Severity": ["Critical", "High", "Medium", "Low"],
            "Count": [metrics.critical_count, metrics.high_count, metrics.medium_count, metrics.low_count],
        }
        colors = ["#FF4444", "#FF8C00", "#FFD700", "#32CD32"]

        fig_severity = go.Figure(
            data=[go.Bar(
                x=severity_data["Severity"],
                y=severity_data["Count"],
                marker_color=colors,
                text=severity_data["Count"],
                textposition="auto",
            )]
        )
        fig_severity.update_layout(
            height=300,
            margin=dict(l=20, r=20, t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig_severity, use_container_width=True)

    with chart_col2:
        st.markdown("#### Top Error Classes")
        if metrics.top_error_classes:
            error_names = [e["error_class"] for e in metrics.top_error_classes[:6]]
            error_counts = [e["count"] for e in metrics.top_error_classes[:6]]

            fig_errors = go.Figure(
                data=[go.Pie(
                    labels=error_names,
                    values=error_counts,
                    hole=0.4,
                    marker_colors=["#20808D", "#A84B2F", "#1B474D", "#BCE2E7", "#944454", "#FFC553"],
                )]
            )
            fig_errors.update_layout(
                height=300,
                margin=dict(l=20, r=20, t=20, b=20),
                paper_bgcolor="rgba(0,0,0,0)",
                showlegend=True,
                legend=dict(font=dict(size=10)),
            )
            st.plotly_chart(fig_errors, use_container_width=True)

    # ─── Hourly Trend ───────────────────────────
    st.markdown("#### Alert Trend (24h)")
    if metrics.hourly_trend:
        hours = [h["hour"] for h in metrics.hourly_trend]
        counts = [h["count"] for h in metrics.hourly_trend]

        fig_trend = go.Figure(
            data=[go.Scatter(
                x=hours,
                y=counts,
                mode="lines+markers",
                fill="tozeroy",
                line=dict(color="#20808D", width=2),
                marker=dict(size=4),
            )]
        )
        fig_trend.update_layout(
            height=250,
            margin=dict(l=20, r=20, t=10, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=False, tickangle=45),
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig_trend, use_container_width=True)

    # ─── Alerts by Pipeline ─────────────────────
    st.markdown("#### Alerts by Pipeline")
    if metrics.alerts_by_pipeline:
        pipelines = [p["pipeline"] for p in metrics.alerts_by_pipeline]
        counts = [p["count"] for p in metrics.alerts_by_pipeline]

        fig_pipeline = go.Figure(
            data=[go.Bar(
                y=pipelines,
                x=counts,
                orientation="h",
                marker_color="#20808D",
                text=counts,
                textposition="auto",
            )]
        )
        fig_pipeline.update_layout(
            height=250,
            margin=dict(l=20, r=20, t=10, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig_pipeline, use_container_width=True)
