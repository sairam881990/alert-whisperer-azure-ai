# Round 2 Gap Inventory — Comprehensive Summary

## Overview
- **Total gaps evaluated**: 55
- **Implemented**: 41 (75%)
- **Skipped with justification**: 14 (25%)
- **Files modified**: 9 Python files
- **Lines added**: ~1,029 net new lines (19,489 → 20,518)
- **All 41 Python files pass syntax checks**

---

## Implemented Fixes (41)

### Chat Engine — 16 fixes in `src/engine/chat_engine.py`

| ID | Gap | Fix Applied |
|----|-----|-------------|
| CE1 | CoVE not on all responses | `_maybe_apply_cove()` async method + configurable `cove_enabled_intents` set |
| CE2 | String-based uncertainty | 9 compiled regex patterns (`UNCERTAINTY_PATTERNS`) replace substring matching |
| CE3 | Temperature per intent | `INTENT_TEMPERATURE_MAP` dict (0.05 for ROOT_CAUSE → 0.3 for GENERAL) |
| CE4 | No response cache | `ResponseCache` class with SHA-256 keying, configurable TTL (default 300s), LRU eviction (max 200) |
| CE6 | Single-intent only | `_detect_multi_intent()` with 6 split patterns; processes each sub-query separately |
| CE7 | No session cleanup | `SESSION_TTL_SECONDS=3600`, `MAX_SESSIONS=100` + `_cleanup_stale_sessions()` called per request |
| CE8 | Unvalidated confidence | `MIN_ROUTING_CONFIDENCE=0.3`; falls back to GENERAL_QA if below threshold |
| CE11 | Hardcoded context window | `CONTEXT_WINDOW_SIZE=20` class constant replaces all hardcoded values |
| CE13 | Generic error messages | `_classify_error()` with 4 categories: timeout, connection, rate-limit, auth + catchall |
| CE15 | Unverified similar incidents | `_verify_similar_incidents()` keyword-overlap Self-RAG filter (≥15% threshold) |
| CE16 | Stale runbook not detected | `RUNBOOK_STALENESS_DAYS=90` + `_check_runbook_staleness()` with ⚠️ warning |
| CE17 | No session reset command | `/new`, `/reset`, `/clear`, `/export`, `/help` command handling |
| CE19 | No rerun confirmation | `_format_rerun_action()` with 🔄 + ⚠️ confirmation block |
| CE21 | FLARE raw append | `_deduplicate_flare_context()` sentence-level dedup before append |
| CE22 | No follow-up suggestions | `FOLLOW_UP_MAP` per intent + `_generate_follow_ups()` appended to all responses |
| CE23 | Fake streaming | `stream_response_tokens()` + `_llm_stream()` async generators for real token-level streaming |
| CE24 | Duplicated routing logic | `_route_intent()` unified method; all paths call single source of truth |

### Alert Triage — 7 fixes in `src/engine/alert_triage.py` + `alert_dedup.py`

| ID | Gap | Fix Applied |
|----|-----|-------------|
| AT1 | Unbounded alert list | `MAX_ALERT_HISTORY=10000` + `_enforce_alert_window()` trims oldest on overflow |
| AT2 | Linear scan search | 3 indexes: `_pipeline_index`, `_error_class_index`, `_severity_index` (defaultdict) |
| AT3 | 24h count never resets | `_compute_24h_count()` with actual time-windowed counting using indexes |
| AT5 | Hardcoded cascade window | `cascade_window_multiplier` parameter (default 2.0) on AlertDeduplicator |
| AT7 | No suppression rules | `add_suppression_rule()` + `_is_suppressed()` check before triage processing |
| AT9 | Severity never downgrades | `_decay_cluster_severity()` with configurable decay window (60 min default) |
| AT10 | No acknowledgment tracking | `acknowledged/acknowledged_by/acknowledged_at` fields + `acknowledge_cluster()` + `get_unacknowledged_clusters()` |

### Data Refresh — 7 fixes in `src/engine/data_refresh.py`

| ID | Gap | Fix Applied |
|----|-----|-------------|
| DR1 | new_event_loop() each call | `_get_or_create_event_loop()` with proper get/create/set pattern |
| DR2 | timespan_hours=0 bug | `max(1, timespan_hours)` guard in all division paths |
| DR3 | Non-deterministic hash() | `_deterministic_hash()` using `hashlib.sha256` |
| DR4 | No data source tag | `metadata["data_source"]` set to `"live_mcp"` or `"demo"` on each ParsedFailure |
| DR5 | Sequential MCP polling | `_poll_all_sources_parallel()` using `asyncio.gather(*tasks, return_exceptions=True)` |
| DR6 | No delta dedup | `_seen_alert_hashes` set + `_alert_fingerprint()` + `_deduplicate_alerts()` |
| DR8 | No re-index trigger | `_force_confluence_reindex` flag + `trigger_confluence_reindex()` public method |

### Prompt Engine — 5 fixes in `src/prompts/prompt_engine.py` + `templates.py`

| ID | Gap | Fix Applied |
|----|-----|-------------|
| PE1 | Regex parse failure | `_parse_llm_output_safe()` with 3-pattern fallback per field; raw output fallback |
| PE2 | No LLM retry | `_call_llm_with_retry()` with exponential backoff (base 1s, max 10s, 3 attempts) |
| PE3 | DSPy 5-sample threshold | `DSPY_MIN_SAMPLES=20` class constant; configurable via settings |
| PE5 | Hardcoded cache TTL | `_prompt_cache_ttl` reads from `settings.llm.prompt_cache_ttl_seconds` |
| PE6 | No "I don't know" instruction | Appended to all 3 system prompts in templates.py |

### Other — 6 fixes across multiple files

| ID | Gap | Fix Applied | File |
|----|-----|-------------|------|
| O1 | Full stack traces in prompts | `_truncate_stack_trace()`: keeps first/last 5 lines + "Caused by" chains | prompt_engine.py |
| O2 | No timestamp cursor | `_high_watermarks` dict + `_get_watermark()` / `_update_watermark()` | data_refresh.py |
| O4 | No staleness indicator | `STALENESS_THRESHOLDS` + `get_staleness_label()` (fresh/aging/stale/expired) | vector_store.py |
| O5 | No ICM ticket linking | `correlate_icm_tickets()` matches by pipeline name or error class | alert_triage.py |
| O6 | No dedup stats in dashboard | `_render_dedup_metrics()` with 4 st.metric widgets | dashboard.py |
| O7 | No config hot-reload | `get_settings()` checks `.env` mtime for auto-reload | settings.py |

### Settings — 12 new config fields in `config/settings.py`

| Setting Class | Fields Added |
|--------------|-------------|
| `ChatEngineSettings` (CHAT_*) | response_cache_ttl_seconds, response_cache_max_size, session_ttl_seconds, max_sessions, min_routing_confidence, context_window_size, cove_enabled |
| `AlertTriageSettings` (ALERT_TRIAGE_*) | max_alert_history, cascade_window_multiplier, severity_decay_minutes |
| `DataRefreshSettings` (DATA_REFRESH_*) | max_seen_alerts, parallel_polling |
| `LLMSettings` additions | dspy_min_optimization_samples, prompt_cache_ttl_seconds |

---

## Skipped Items with Justifications (14)

| ID | Gap | Reason Not Implemented |
|----|-----|----------------------|
| CE5 | Chat history token budget | **Already implemented in Round 1** (P11) — `conversation_token_budget` in settings + `_trim_history_to_budget()` in chat_engine.py |
| CE9 | Missing COMPARE_ALERTS intent | **Already handled** — CROSS_ALERT_ANALYTICS intent covers comparisons; prompt handles "compare X vs Y" |
| CE10 | No fallback intent | **Already handled** — `_classify_intent()` returns GENERAL as default with confidence 0.5 |
| CE12 | In-memory session store | **Intentional for v1** — Streamlit is single-process; Redis/DB is a v2 deployment concern. Session TTL+max (CE7) provides sufficient memory bounds. |
| CE14 | No structured tool-call schema | **Unnecessary** — MCP calls already use typed MCPRequest/MCPResponse Pydantic models. Adding another schema layer provides no value. |
| CE18 | Escalation can't execute | **By design** — UI shows escalation intent but actual Teams webhook push requires live connector. Button correctly logs the escalation request. Production deployment hooks into real Teams webhook. |
| CE20 | No conversation export | **Already implemented** — `export_markdown.py` provides full conversation export to Markdown |
| AT4 | Empty blast radius w/o GraphRAG | **Adequate fallback** — Uses `affected_components` from alert model, producing reasonable results. GraphRAG is the production path. |
| AT6 | Hardcoded 4 demo pipelines | **Intentional** — Demo data is static for development; production mode pulls from MCP connectors dynamically |
| AT8 | No cluster merge logic | **Would reduce accuracy** — Dedup groups handle this via `_compute_dedup_key`; forcing merges across different error classes would reduce triage precision |
| DR7 | No circuit breaker for refresh | **Already implemented in Round 1** (M7) — Circuit breakers exist on all 4 MCP connectors with configurable thresholds and cooldowns |
| PE4 | No prompt version diffing | **Over-engineering** — Template versioning (P10) already provides version field and audit trail. A full diff engine exceeds current scale requirements. |
| O3 | Batch size auto-detection | **Already configurable** — `indexing_batch_size`, `indexing_max_batches`, `volume_warn_threshold` in LogAnalyticsSettings provide full control |

---

## Files Modified

| File | Before | After | Delta |
|------|--------|-------|-------|
| `src/engine/chat_engine.py` | 1,323 | 1,789 | +466 |
| `src/engine/alert_triage.py` | 581 | 736 | +155 |
| `src/engine/alert_dedup.py` | 380 | 383 | +3 |
| `src/engine/data_refresh.py` | 480 | 629 | +149 |
| `src/prompts/prompt_engine.py` | 1,054 | 1,175 | +121 |
| `src/prompts/templates.py` | 1,073 | 1,088 | +15 |
| `src/rag/vector_store.py` | 1,754 | 1,776 | +22 |
| `config/settings.py` | 294 | 371 | +77 |
| `ui/components/dashboard.py` | 148 | ~165 | +17 |
| **Total** | **7,087** | **8,112** | **+1,025** |

## Project Totals

| Metric | Round 1 End | Round 2 End |
|--------|-------------|-------------|
| Python files | 41 | 41 |
| Total lines | 19,489 | 20,518 |
| Config fields | ~50 | ~64 |
| Docs | 8 | 10 |

---

## Cumulative Feature Summary (Round 1 + Round 2)

### RAG Pipeline (16 techniques)
Query Rewriting, RAG Fusion (configurable k), Self-RAG, Cross-Encoder Reranking (dedicated + LLM), Semantic Chunking, CRAG, HyDE, FLARE (with dedup), Agentic RAG, GraphRAG, Topic Tree, Parent-Child Chunking, Azure AI Search (vector + keyword + semantic), Hybrid Search, Document Staleness Tracking, KB Re-index Trigger

### Chat Engine (24 features)
Multi-intent detection, Response caching (LRU + TTL), Session management (TTL + max limit), Per-intent temperature, CoVE verification (configurable intents), Regex uncertainty detection, Confidence threshold validation, Real token-level streaming, Unified routing method, Follow-up suggestions, Command handling (/new /reset /help /export), Rerun confirmation, Stale runbook detection, Self-RAG incident verification, User-friendly error messages, FLARE dedup, Active prompting, Conversational troubleshooting, Alert-free discovery mode, Pipeline detail routing, Cross-alert analytics, Escalation support, Configurable context window, Conversation token budget

### Alert Triage (13 features)
Deduplication with cascade detection, Rolling alert window, Indexed search (3 indexes), Time-windowed 24h counts, Alert suppression rules, Severity time-decay, Cluster acknowledgment, ICM ticket correlation, Configurable cascade window, Proactive greeting, Blast radius computation, Auto-assignment, Pipeline health tracking

### Data Refresh (10 features)
Parallel MCP polling, Delta deduplication, Timestamp cursors, Deterministic hashing, Data source tagging, Event loop safety, Division-by-zero guards, Confluence re-index trigger, Circuit breakers (inherited), Hot-reload config

### Prompt Engine (10 features)
LLM retry with backoff, Parse fallback, Stack trace truncation, "I don't know" instruction, Configurable DSPy threshold, Configurable cache TTL, Template versioning, Few-shot examples, Auto technique selection, Conversation token budget
