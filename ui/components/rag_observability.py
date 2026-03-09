"""
RAG Strategy Observability Dashboard.

Displays metrics and visualizations for each RAG retrieval technique:
- CRAG correction rates and relevance scores
- HyDE template usage distribution
- FLARE trigger frequency and augmentation rates
- GraphRAG blast radius queries
- Agentic RAG source routing decisions
- BM25 vs Vector score distributions

Why observability for Spark/Synapse/Kusto troubleshooting RAG:
- Without visibility, the team can't tell if CRAG is filtering out too
  aggressively (removing good Confluence runbooks) or too loosely
  (letting irrelevant ICM incidents through)
- FLARE trigger rate tells you if prompts need tuning — too many triggers
  means initial retrieval is poor; too few means FLARE isn't helping
- HyDE template distribution shows which error types are most common,
  informing where to invest in better runbook coverage
- BM25 vs Vector score comparison reveals whether hybrid search is
  actually helping or if one modality dominates
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st


def render_rag_observability(rag_metrics: dict[str, Any] | None = None) -> None:
    """
    Render the RAG strategy observability dashboard.

    Args:
        rag_metrics: Dict of RAG strategy metrics. If None, uses demo data.
    """
    st.markdown("## RAG Strategy Observability")
    st.markdown("Monitor retrieval strategy performance and effectiveness.")

    # Use demo metrics if none provided
    if rag_metrics is None:
        rag_metrics = _get_demo_metrics()

    # ─── Strategy Usage KPIs ─────────────────────
    st.markdown("### Strategy Usage (Last 24h)")
    cols = st.columns(6)

    strategies = [
        ("CRAG", rag_metrics.get("crag_invocations", 0), "corrections applied"),
        ("HyDE", rag_metrics.get("hyde_invocations", 0), "docs generated"),
        ("FLARE", rag_metrics.get("flare_triggers", 0), "augmentations"),
        ("GraphRAG", rag_metrics.get("graphrag_queries", 0), "graph traversals"),
        ("Agentic", rag_metrics.get("agentic_plans", 0), "plans executed"),
        ("BM25 Hybrid", rag_metrics.get("hybrid_searches", 0), "hybrid queries"),
    ]

    for col, (name, count, label) in zip(cols, strategies):
        with col:
            st.metric(label=name, value=count, help=label)

    st.divider()

    # ─── CRAG Relevance Distribution ─────────────
    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        st.markdown("### CRAG Relevance Grading")
        crag_data = rag_metrics.get("crag_distribution", {})
        fig_crag = go.Figure(data=[go.Pie(
            labels=["Correct", "Ambiguous", "Incorrect"],
            values=[
                crag_data.get("correct", 65),
                crag_data.get("ambiguous", 22),
                crag_data.get("incorrect", 13),
            ],
            hole=0.4,
            marker_colors=["#32CD32", "#FFD700", "#FF4444"],
        )])
        fig_crag.update_layout(
            height=280,
            margin=dict(l=20, r=20, t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_crag, use_container_width=True)

    with chart_col2:
        st.markdown("### FLARE Trigger Analysis")
        flare_data = rag_metrics.get("flare_analysis", {})
        fig_flare = go.Figure(data=[go.Bar(
            x=["Triggered", "Augmented", "Skipped (threshold)"],
            y=[
                flare_data.get("triggered", 28),
                flare_data.get("augmented", 18),
                flare_data.get("skipped", 45),
            ],
            marker_color=["#20808D", "#32CD32", "#888"],
            text=[
                flare_data.get("triggered", 28),
                flare_data.get("augmented", 18),
                flare_data.get("skipped", 45),
            ],
            textposition="auto",
        )])
        fig_flare.update_layout(
            height=280,
            margin=dict(l=20, r=20, t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig_flare, use_container_width=True)

    # ─── HyDE Template Distribution ──────────────
    st.markdown("### HyDE Template Usage")
    hyde_data = rag_metrics.get("hyde_templates", {})
    templates = list(hyde_data.keys()) if hyde_data else ["spark_oom", "spark_shuffle", "synapse_timeout", "kusto_ingestion", "generic"]
    counts = list(hyde_data.values()) if hyde_data else [34, 12, 21, 18, 15]

    fig_hyde = go.Figure(data=[go.Bar(
        y=templates,
        x=counts,
        orientation="h",
        marker_color="#20808D",
        text=counts,
        textposition="auto",
    )])
    fig_hyde.update_layout(
        height=220,
        margin=dict(l=20, r=20, t=10, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
    )
    st.plotly_chart(fig_hyde, use_container_width=True)

    # ─── Agentic RAG Source Routing ──────────────
    st.markdown("### Agentic RAG — Source Routing Decisions")
    routing_col1, routing_col2 = st.columns(2)

    with routing_col1:
        routing_data = rag_metrics.get("agentic_routing", {})
        source_names = list(routing_data.keys()) if routing_data else ["confluence", "icm", "log_analytics", "graph"]
        source_counts = list(routing_data.values()) if routing_data else [42, 38, 25, 15]

        fig_routing = go.Figure(data=[go.Pie(
            labels=[s.replace("_", " ").title() for s in source_names],
            values=source_counts,
            hole=0.4,
            marker_colors=["#20808D", "#A84B2F", "#1B474D", "#BCE2E7"],
        )])
        fig_routing.update_layout(
            height=250,
            margin=dict(l=20, r=20, t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_routing, use_container_width=True)

    with routing_col2:
        st.markdown("#### Search Mode Performance")
        perf_data = rag_metrics.get("search_mode_performance", {})
        modes = list(perf_data.keys()) if perf_data else ["Vector Only", "BM25 Only", "Hybrid"]
        avg_scores = list(perf_data.values()) if perf_data else [0.72, 0.58, 0.81]

        fig_perf = go.Figure(data=[go.Bar(
            x=modes,
            y=avg_scores,
            marker_color=["#A84B2F", "#1B474D", "#20808D"],
            text=[f"{s:.0%}" for s in avg_scores],
            textposition="auto",
        )])
        fig_perf.update_layout(
            height=250,
            margin=dict(l=20, r=20, t=10, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(title="Avg Relevance Score", showgrid=True, gridcolor="rgba(128,128,128,0.2)"),
        )
        st.plotly_chart(fig_perf, use_container_width=True)

    # ─── DSPy Technique Selection ────────────────
    st.markdown("### DSPy — Technique Selection by Error Class")
    dspy_data = rag_metrics.get("dspy_selections", [])
    if not dspy_data:
        dspy_data = [
            {"error_class": "OutOfMemoryError", "technique": "few_shot", "avg_score": 0.85, "count": 24},
            {"error_class": "TimeoutException", "technique": "cot", "avg_score": 0.79, "count": 18},
            {"error_class": "ConnectionFailure", "technique": "react", "avg_score": 0.74, "count": 12},
            {"error_class": "SchemaError", "technique": "cot", "avg_score": 0.82, "count": 9},
            {"error_class": "ConfigurationError", "technique": "step_back", "avg_score": 0.77, "count": 7},
        ]

    for item in dspy_data:
        col_a, col_b, col_c, col_d = st.columns([3, 2, 2, 1])
        with col_a:
            st.text(item["error_class"])
        with col_b:
            st.text(f"Technique: {item['technique']}")
        with col_c:
            st.progress(item["avg_score"], text=f"Score: {item['avg_score']:.0%}")
        with col_d:
            st.text(f"n={item['count']}")


def _get_demo_metrics() -> dict[str, Any]:
    """Generate demo metrics for the observability dashboard."""
    return {
        "crag_invocations": 142,
        "hyde_invocations": 98,
        "flare_triggers": 28,
        "graphrag_queries": 34,
        "agentic_plans": 56,
        "hybrid_searches": 187,
        "crag_distribution": {"correct": 65, "ambiguous": 22, "incorrect": 13},
        "flare_analysis": {"triggered": 28, "augmented": 18, "skipped": 45},
        "hyde_templates": {
            "spark_oom": 34,
            "spark_shuffle": 12,
            "synapse_timeout": 21,
            "kusto_ingestion": 18,
            "generic": 15,
        },
        "agentic_routing": {
            "confluence": 42,
            "icm": 38,
            "log_analytics": 25,
            "graph": 15,
        },
        "search_mode_performance": {
            "Vector Only": 0.72,
            "BM25 Only": 0.58,
            "Hybrid": 0.81,
        },
    }
