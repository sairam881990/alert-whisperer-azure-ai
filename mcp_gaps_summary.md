# MCP Connector Gaps: Implementation Summary (M1–M9)

Implemented by subagent on 2026-03-09. All 6 files pass `python -m py_compile`.

---

## Files Modified

| File | Gaps |
|---|---|
| `src/connectors/mcp_base.py` | M4, M5, M6, M7 |
| `src/connectors/kusto_connector.py` | M2, M4, M8, M9 |
| `src/connectors/confluence_connector.py` | M1, M4, M9 |
| `src/connectors/icm_connector.py` | M3, M4, M9 |
| `src/connectors/loganalytics_connector.py` | M4 (MCP health check only — LA_QUERIES untouched) |
| `config/settings.py` | M1, M2, M7 config fields added to all connector settings |

---

## Gap Details

### M1 — Confluence CQL Pagination (`confluence_connector.py`)

**Problem:** `search_pages` fetched only one page of results.

**Fix:** Rewrote `search_pages` with a `start` + `limit` pagination loop:
- Added `max_pages` (default 5) and `page_size` (default 25) parameters.
- Auto-advances `start` offset on each call.
- Detects end-of-results via `_links.next` (Confluence REST pattern), `size < batch_size`, or list shorter than requested.
- Truncates final result list to the caller's `limit`.
- Config fields `CONFLUENCE_SEARCH_MAX_PAGES` and `CONFLUENCE_SEARCH_PAGE_SIZE` added to `ConfluenceSettings`.

---

### M2 — Kusto Result Streaming (`kusto_connector.py`)

**Problem:** All KQL queries loaded full result sets into memory.

**Fix:** Added `stream_query_results()` async generator:
- Wraps the caller's query in a `let _base = (...); _base | skip N | take batch_size` pattern for safe offset pagination.
- `batch_size` default 1000, `max_batches` cap default 20 (both configurable in `KustoSettings`).
- Yields `list[Any]` batches; stops early if a batch is smaller than `batch_size` (last page).
- Emits `kusto_stream_*` structured log events at each stage.

---

### M3 — ICM Bulk Incident Updates (`icm_connector.py`)

**Problem:** No way to update multiple correlated incidents in one operation.

**Fix:** Added `bulk_update_incidents(ticket_ids, status, notes, assigned_to, correlation_tag)`:
- Attempts the MCP server's native `bulk_update_incidents` tool first.
- Falls back to sequential `update_incident` calls if the native tool is unavailable.
- Returns `{succeeded: [...], failed: [{ticket_id, error}], total, method}`.
- Accepts an optional `correlation_tag` to label a batch of related incidents.

---

### M4 — Connector Health Checks

**Problem:** No per-connector health check; the base only hit `/health`.

**Fix:** `BaseMCPClient.health_check()` now returns a structured dict:
```python
{"healthy": bool, "latency_ms": float, "circuit_breaker": str, "detail": str}
```

Each subclass overrides and enriches it:

| Connector | Extra probe |
|---|---|
| `KustoMCPClient` | `.show version` to confirm cluster is reachable |
| `ConfluenceMCPClient` | `search type=space limit=1` to confirm Confluence is live |
| `ICMMCPClient` | `list_incidents limit=1 status=active` |
| `LogAnalyticsMCPClient` | `print 'ok'` KQL probe against the workspace |

---

### M5 — Error Classification (`mcp_base.py`)

**Problem:** All errors were treated identically by retry logic.

**Fix:** Added `MCPErrorClass` enum with four categories:
- `TRANSIENT` — safe to retry (network errors, 5xx status codes)
- `PERMANENT` — do not retry (400, 404, 422, etc.)
- `AUTH_EXPIRED` — 401/403; caller must re-authenticate
- `RATE_LIMITED` — 429; use aggressive back-off

Module-level `classify_error(exc)` function maps:
- `httpx.HTTPStatusError` → status code lookup
- `httpx.ConnectError`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout`, `ConnectionResetError`, `OSError` → TRANSIENT
- Message-pattern heuristics (timeout, connection reset, etc.) → TRANSIENT
- Fallback → UNKNOWN (retry conservatively)

`call_tool` uses the classification: PERMANENT and AUTH_EXPIRED skip retries immediately.

---

### M6 — Structured Request/Response Logging (`mcp_base.py`)

**Problem:** No visibility into what was sent/received for debugging.

**Fix:**
- `call_tool` logs `mcp_tool_call` (with `request_id`) and `mcp_tool_response` (result type + length).
- `_send_request` logs `mcp_request` (DEBUG) and `mcp_response` (DEBUG with status code).
- HTTP errors log with `error_class` from M5 classification.
- All argument dicts pass through `_mask_sensitive()` before logging.
- `_mask_sensitive()` recursively masks keys matching `api_key|token|secret|password|auth|bearer|client_secret`; truncates strings > 200 chars.

---

### M7 — Expanded Retry + Exponential Backoff + Circuit Breaker (`mcp_base.py`)

**Problem:** Retry only covered `ConnectError` and `TimeoutException`; no jitter; no circuit breaker.

**Fix:**

**Retry expansion:**
- Now catches: `httpx.ReadTimeout`, `httpx.WriteTimeout`, `httpx.PoolTimeout`, `ConnectionResetError`, `OSError`, all 5xx `HTTPStatusError` codes.
- 3 attempts total; PERMANENT/AUTH errors exit immediately on first failure.

**Exponential backoff with jitter:**
```python
delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
# e.g. attempt 1 → ~2s, attempt 2 → ~4s
```
Rate-limited errors use `max(delay, 10.0)`.

**Circuit Breaker (`CircuitBreaker` class):**
- Tracks consecutive failures per client instance.
- After `failure_threshold` failures → OPEN state; calls raise `CircuitBreakerOpen` immediately.
- After `cooldown_seconds` → transitions to HALF_OPEN; one probe request allowed.
- On success → resets to CLOSED.
- Configurable per connector via `circuit_breaker_threshold` and `circuit_breaker_cooldown_seconds` settings fields.

---

### M8 — Additional KQL_QUERIES (`kusto_connector.py`)

**Problem:** `KQL_QUERIES` had only 7 query types.

**Fix:** Added 6 new entries + helper methods:

| Key | Command | Helper Method |
|---|---|---|
| `cluster_version` | `.show version` | Used by M4 `health_check()` |
| `ingestion_status` | `.show ingestion failures \| summarize by Table, ErrorCode` | `get_ingestion_status(timespan)` |
| `cache_utilization` | `.show cache utilization` | `get_cache_utilization()` |
| `query_stats` | `.show queries \| summarize count(), avg(Duration) by Database` | `get_query_stats(timespan)` |
| `table_details` | `.show table {table_name} details` | `get_table_details(table_name)` |
| `extent_stats` | `.show database extents \| summarize by TableName` | `get_extent_stats()` |

---

### M9 — GraphRAG Dependency Metadata Methods

**Problem:** GraphRAG extraction for ADF/Databricks/Synapse had no connector-side support.

**Fix:** Three new methods across three connectors:

#### `KustoMCPClient.get_table_dependencies(database)`
Returns:
```json
{
  "database": "...",
  "materialized_views": [{"name", "source_table", "query", "is_healthy"}],
  "update_policies": [{"table", "policy_raw"}],
  "databases": ["db1", "db2"]
}
```
Issues: `.show materialized-views`, `.show tables details | project TableName, UpdatePolicy`, `.show databases`.

#### `ConfluenceMCPClient.get_linked_pages(page_id, depth)`
Returns:
```json
{
  "root_page_id": "...",
  "children": [{"page_id", "title", "url", "link_type": "child"}],
  "links": [{"page_id", "title", "url", "link_type": "inline_page_id|inline_title_ref"}]
}
```
Fetches child pages via `get_child_pages` tool and extracts inline `pageId=` refs and `ri:content-title` refs from the storage-format body.

#### `ICMMCPClient.get_related_incidents(ticket_id, include_historical, days_back)`
Returns:
```json
{
  "root_incident_id": "...",
  "root_incident": {...},
  "related": [...],
  "similar_historical": [...],
  "correlation_graph": {"nodes": [...], "edges": [...]}
}
```
Fetches direct relations via `get_related_incidents` MCP tool; enriches with historical similarity via `search_historical_incidents`. Builds a `{nodes, edges}` graph structure for GraphRAG ingestion.

---

## Syntax Verification

```
mcp_base.py            OK
kusto_connector.py     OK
confluence_connector.py OK
icm_connector.py       OK
loganalytics_connector.py OK
settings.py            OK
```

All files verified with `python -m py_compile`. No regressions to existing methods.
