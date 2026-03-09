# Round 2 Gap Triage — Full Classification

## Legend
- **IMPLEMENT** — Valid gap, will fix
- **SKIP (justified)** — Valid observation but already handled or not needed; justification provided
- **ALREADY HANDLED** — Code already addresses this

---

## Chat Engine Gaps (CE1–CE24)

| ID | Gap | Verdict | Justification |
|----|-----|---------|---------------|
| CE1 | CoVE not on all responses | IMPLEMENT | CoVE only runs for ROOT_CAUSE / TROUBLESHOOT intents; should be available as a configurable wrapper for any response |
| CE2 | String-based uncertainty ("I think") | IMPLEMENT | Uncertainty is detected via substring matches; add regex + configurable patterns list |
| CE3 | Temperature per intent | IMPLEMENT | All intents use settings.llm.temperature; high-precision intents (ROOT_CAUSE) should use lower temp |
| CE4 | Response cache (same question → same answer) | IMPLEMENT | No LRU/TTL cache on LLM responses; add hash-based response cache with TTL |
| CE5 | Chat history token budget not enforced globally | SKIP | Already implemented in Round 1 as P11 — `conversation_token_budget` in settings + `_trim_history_to_budget()` in chat_engine.py |
| CE6 | Multi-intent detection | IMPLEMENT | Router picks single best intent; add multi-intent split logic |
| CE7 | No session cleanup / TTL | IMPLEMENT | In-memory sessions grow unbounded; add TTL + max-session limit |
| CE8 | Confidence threshold unvalidated | IMPLEMENT | Route confidence isn't validated against a minimum before acting on routing |
| CE9 | Missing intent: COMPARE_ALERTS | SKIP | Already handled — CROSS_ALERT_ANALYTICS intent covers comparisons, and the prompt handles "compare X vs Y" queries |
| CE10 | No fallback intent | SKIP | Already handled — `_classify_intent()` returns GENERAL as default fallback with confidence 0.5 |
| CE11 | Fixed 10-message context window | IMPLEMENT | `get_recent_messages(n=20)` exists but chat_engine uses hardcoded 10 in some paths; unify to configurable |
| CE12 | In-memory session store | SKIP | Intentional for v1/Streamlit single-process model; Redis/DB is a v2 deployment concern, not a code gap |
| CE13 | No user-facing error detail | IMPLEMENT | Exceptions return generic "An error occurred"; add error classification with user-friendly messages |
| CE14 | No structured tool-call schema | SKIP | MCP calls use typed MCPRequest/MCPResponse models; adding another schema layer adds no value |
| CE15 | Similar incidents unverified | IMPLEMENT | RAG-retrieved incidents aren't re-scored for relevance before display; add Self-RAG pass |
| CE16 | Stale runbook detection | IMPLEMENT | Runbook content is used without checking last-modified date; add staleness warning |
| CE17 | No explicit "new session" command | IMPLEMENT | Users can't reset context mid-conversation; add /new or /reset command handling |
| CE18 | Escalation button can't execute | SKIP | By design — UI shows escalation intent but actual Teams webhook push requires live connector; the button correctly logs the escalation request |
| CE19 | No rerun confirmation | IMPLEMENT | Rerun URL presented without confirmation step; add "Are you sure?" guard |
| CE20 | No conversation export | SKIP | Already implemented — export_markdown.py provides full conversation export |
| CE21 | FLARE raw append | IMPLEMENT | FLARE refinement appends raw retrieval text without dedup or summarization |
| CE22 | No proactive follow-up suggestions | IMPLEMENT | After answering, engine doesn't suggest next logical questions |
| CE23 | Fake streaming (chunked text) | IMPLEMENT | `stream_response()` simulates streaming by splitting text; implement real token-level streaming |
| CE24 | Shared routing method | IMPLEMENT | Intent classification logic duplicated between classify and route; extract to shared method |

**CE Summary: 16 IMPLEMENT, 8 SKIP**

---

## Alert Triage Gaps (AT1–AT10)

| ID | Gap | Verdict | Justification |
|----|-----|---------|---------------|
| AT1 | Unbounded `_all_alerts` list | IMPLEMENT | `_all_alerts` grows without bound; add rolling window with max size |
| AT2 | Linear scan in search_alerts | IMPLEMENT | `search_alerts()` iterates all alerts; add index by pipeline + error_class |
| AT3 | 24h alert count never resets | IMPLEMENT | `alert_count_24h` in `_update_health()` only increments; needs time-windowed counting |
| AT4 | Empty blast radius when no GraphRAG | SKIP | Fallback uses `affected_components` from alert model — produces reasonable results. GraphRAG is the production path. |
| AT5 | Cascade detection window hardcoded | IMPLEMENT | `self.time_window * 2` is hardcoded; make configurable |
| AT6 | Hardcoded 4 demo pipelines | SKIP | Demo data is intentionally static for development; production mode pulls from MCP connectors |
| AT7 | No alert suppression rules | IMPLEMENT | No way to suppress known-noisy alerts; add suppression config |
| AT8 | No cluster merge logic | SKIP | Dedup groups handle this via `_compute_dedup_key`; forcing cluster merges across different error classes would reduce triage accuracy |
| AT9 | Severity upgrade only, never downgrade | IMPLEMENT | Once severity is elevated in a cluster, it never decreases even if new alerts are lower; add time-decay |
| AT10 | No alert acknowledgment tracking | IMPLEMENT | No way to mark a cluster as "acknowledged" to separate from new alerts |

**AT Summary: 7 IMPLEMENT, 3 SKIP**

---

## Dashboard / Data Refresh Gaps (DR1–DR8)

| ID | Gap | Verdict | Justification |
|----|-----|---------|---------------|
| DR1 | `asyncio.new_event_loop()` in sync context | IMPLEMENT | Creates new event loop each call; use `asyncio.get_event_loop()` with fallback |
| DR2 | `timespan_hours=0` bug | IMPLEMENT | `_build_kusto_query` divides by timespan_hours without zero check |
| DR3 | `hash()` non-deterministic | IMPLEMENT | Python `hash()` is randomized per process; use hashlib for dedup keys |
| DR4 | No data_source_tag on responses | IMPLEMENT | Responses don't indicate whether data came from demo or live MCP |
| DR5 | Sequential MCP polling | IMPLEMENT | All 4 MCP sources polled sequentially; use asyncio.gather for parallel |
| DR6 | No delta dedup | IMPLEMENT | Re-ingesting same alerts on refresh creates duplicates; add seen-set |
| DR7 | No circuit breaker for refresh | SKIP | Already implemented in Round 1 as M7 — circuit breakers exist on all MCP connectors with configurable thresholds |
| DR8 | No Confluence re-index trigger | IMPLEMENT | No way to trigger KB re-index after Confluence content changes |

**DR Summary: 7 IMPLEMENT, 1 SKIP**

---

## Prompt Engine Gaps (PE1–PE6)

| ID | Gap | Verdict | Justification |
|----|-----|---------|---------------|
| PE1 | Regex parse failure on unexpected LLM output | IMPLEMENT | Template parsing uses rigid regex; add graceful fallback |
| PE2 | No LLM retry with backoff | IMPLEMENT | Single LLM call with no retry; add tenacity-style retry |
| PE3 | DSPy 5-sample threshold too low | IMPLEMENT | Optimizer activates at 5 samples; increase to configurable minimum (default 20) |
| PE4 | No prompt version diffing | SKIP | Template versioning was added in Round 1 (P10) with version field; a full diff engine is over-engineering for the current scale |
| PE5 | Cache TTL not configurable | IMPLEMENT | Prompt cache has hardcoded 1h TTL; make configurable via settings |
| PE6 | No "I don't know" instruction | IMPLEMENT | System prompts don't explicitly instruct LLM to say "I don't know" when unsure |

**PE Summary: 5 IMPLEMENT, 1 SKIP**

---

## Other / Scored Items

| ID | Gap | Verdict | Justification |
|----|-----|---------|---------------|
| O1 | Stack trace control in prompts | IMPLEMENT | Full stack traces passed to LLM; add truncation + relevance extraction |
| O2 | Timestamp cursor for incremental refresh | IMPLEMENT | Refresh always queries from start; add high-watermark cursor |
| O3 | Batch size auto-detection | SKIP | Already configurable via `indexing_batch_size` / `indexing_max_batches` / `volume_warn_threshold` in settings |
| O4 | D10 staleness indicator on KB docs | IMPLEMENT | No staleness flag on knowledge base documents |
| O5 | M10 ICM ticket linking to alerts | IMPLEMENT | No automatic correlation between ICM tickets and alert clusters |
| O6 | D11 alert dedup stats in dashboard | IMPLEMENT | Dashboard doesn't show dedup effectiveness metrics |
| O7 | P5 config reload without restart | IMPLEMENT | Settings loaded once at startup; add hot-reload capability |

**Other Summary: 6 IMPLEMENT, 1 SKIP**

---

## Grand Total

| Category | Implement | Skip | Total |
|----------|-----------|------|-------|
| Chat Engine (CE) | 16 | 8 | 24 |
| Alert Triage (AT) | 7 | 3 | 10 |
| Dashboard/Refresh (DR) | 7 | 1 | 8 |
| Prompt Engine (PE) | 5 | 1 | 6 |
| Other (O) | 6 | 1 | 7 |
| **TOTAL** | **41** | **14** | **55** |
