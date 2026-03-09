"""Knowledge Base management page — indexing, stats, and search."""

from __future__ import annotations

import streamlit as st


def render_knowledge_base() -> None:
    """Render the knowledge base management page."""

    st.markdown("## Knowledge Base")
    st.markdown("Manage the RAG vector store used for similarity matching and context retrieval.")

    # Stats
    col1, col2, col3, col4 = st.columns(4)

    stats = st.session_state.get("kb_stats", {
        "total_documents": 0,
        "by_source": {"confluence": 0, "icm": 0, "log_analytics": 0},
    })

    with col1:
        st.metric("Total Documents", stats.get("total_documents", 0))
    with col2:
        st.metric("Confluence Pages", stats.get("by_source", {}).get("confluence", 0))
    with col3:
        st.metric("ICM Incidents", stats.get("by_source", {}).get("icm", 0))
    with col4:
        st.metric("Log Entries", stats.get("by_source", {}).get("log_analytics", 0))

    st.divider()

    # Indexing controls
    st.markdown("### Index Data Sources")
    st.markdown("Pull data from connected sources into the vector store for similarity search.")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**Confluence Runbooks**")
        confluence_spaces = st.text_input(
            "Space Keys",
            value="RUNBOOKS, OPS, DE",
            key="idx_confluence_spaces",
        )
        if st.button("Index Confluence", key="btn_idx_confluence", use_container_width=True):
            with st.spinner("Indexing Confluence pages..."):
                st.success("Confluence indexing complete (demo mode)")

    with col2:
        st.markdown("**ICM Incidents**")
        icm_days = st.number_input("Days Back", value=180, min_value=7, max_value=365, key="idx_icm_days")
        if st.button("Index ICM", key="btn_idx_icm", use_container_width=True):
            with st.spinner("Indexing ICM incidents..."):
                st.success("ICM indexing complete (demo mode)")

    with col3:
        st.markdown("**Log Analytics**")
        log_days = st.number_input("Days Back", value=30, min_value=1, max_value=90, key="idx_log_days")
        if st.button("Index Logs", key="btn_idx_logs", use_container_width=True):
            with st.spinner("Indexing log entries..."):
                st.success("Log indexing complete (demo mode)")

    st.divider()

    # Search test
    st.markdown("### Test Retrieval")
    st.markdown("Search the knowledge base to verify indexing quality.")

    search_query = st.text_input(
        "Search Query",
        placeholder="e.g., OutOfMemoryError in Spark ETL",
        key="kb_search_query",
    )

    col1, col2 = st.columns([3, 1])
    with col1:
        source_filter = st.selectbox(
            "Source Filter",
            options=["All", "confluence", "icm", "log_analytics"],
            key="kb_source_filter",
        )
    with col2:
        top_k = st.number_input("Top K", value=5, min_value=1, max_value=20, key="kb_top_k")

    if st.button("Search", key="btn_kb_search", type="primary"):
        if search_query:
            with st.spinner("Searching knowledge base..."):
                st.markdown("#### Results")
                # Demo results
                st.markdown(
                    """
                    **1. [Confluence] OOM Recovery Runbook** — Score: 0.91
                    > Steps to diagnose and resolve OutOfMemoryError in Spark jobs.
                    > Key steps: Check executor metrics, identify skewed stage, apply salting...

                    **2. [ICM] INC-2024-0847 — Spark OOM during daily customer join** — Score: 0.88
                    > Root Cause: Data skew in customer_id column.
                    > Resolution: Added salting, increased executor memory to 16GB.

                    **3. [Log Analytics] spark_etl_daily_ingest ERROR** — Score: 0.76
                    > java.lang.OutOfMemoryError: Java heap space
                    > Stage 12, Task 47.3, UnsafeRow.copy
                    """
                )
        else:
            st.warning("Enter a search query.")

    st.divider()

    # Topic Tree visualization
    st.markdown("### Topic Tree")
    st.markdown("Hierarchical knowledge organization for targeted retrieval.")

    with st.expander("Data Pipeline Errors", expanded=True):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.markdown("**Spark Errors**")
            st.markdown("- OutOfMemory")
            st.markdown("- Shuffle Failures")
            st.markdown("- Executor Lost")
            st.markdown("- Schema Mismatch")
            st.markdown("- Timeouts")
        with col2:
            st.markdown("**Synapse Errors**")
            st.markdown("- Pipeline Timeout")
            st.markdown("- Copy Activity Failures")
            st.markdown("- SQL Pool Errors")
            st.markdown("- Auth Failures")
        with col3:
            st.markdown("**Kusto Errors**")
            st.markdown("- Ingestion Failures")
            st.markdown("- Query Failures")
            st.markdown("- Cluster Issues")
        with col4:
            st.markdown("**Infrastructure**")
            st.markdown("- Network Connectivity")
            st.markdown("- Storage Issues")
            st.markdown("- Auth & Secrets")
