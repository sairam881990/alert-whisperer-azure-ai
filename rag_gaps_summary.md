# RAG System Gaps Implementation Summary

**Project:** Alert Whisperer  
**Scope:** RAG System Gaps R1–R17  
**Date:** 2026-03-09  
**Status:** All 17 gaps implemented and syntax-verified

---

## Verification Results

All modified files pass Python AST syntax verification:

```
src/rag/advanced_techniques.py  → OK
src/rag/retriever.py            → OK
src/rag/vector_store.py         → OK
config/settings.py              → OK
```

---

## Implementations by File

### `src/rag/advanced_techniques.py`

#### R1 — Deterministic LLM Rewriting Temperature
**Problem:** `QueryRewriter._llm_rewrite()` used `temperature=0.3`, producing non-deterministic rewrites that made caching and reproducibility unreliable.  
**Fix:** Changed `temperature=0.3` → `temperature=0.0` in the LLM call inside `_llm_rewrite()`.  
**Impact:** Query rewrites are now deterministic; identical inputs always produce identical outputs, improving cache hit rates.

---

#### R2 — Reduced RAG Fusion Variant Temperature
**Problem:** `RAGFusion._llm_generate_variants()` used `temperature=0.7`, generating overly creative/noisy query variants that hurt precision.  
**Fix:** Changed `temperature=0.7` → `temperature=0.3` in the LLM call inside `_llm_generate_variants()`.  
**Impact:** Generated variants remain diverse but are more grounded and semantically coherent with the original query.

---

#### R3 — Full Synonym Expansion (Remove Early Break)
**Problem:** `QueryRewriter._expand_synonyms()` had a `break` statement inside its synonym-matching loop, causing it to stop after the first matching synonym group and silently skip all subsequent matches.  
**Fix:** Removed the `break` statement so the loop continues through all synonym groups for each query token.  
**Impact:** Queries containing multiple domain-specific terms (e.g., "Spark executor OOM in Synapse") now have all relevant synonyms expanded, not just the first matched term.

---

#### R4 — Configurable RRF K Parameter
**Problem:** `RAGFusion.fuse_results()` hardcoded `RRF_K = 60` as a class constant with no instance-level override, making it impossible to tune per-deployment without code changes.  
**Fix:**  
- Added `rrf_k: int = 60` parameter to `RAGFusion.__init__()`, stored as `self.rrf_k`.  
- Updated `fuse_results()` to use `self.rrf_k` instead of the class constant.  
- Added `asyncio` to imports (required by R5, added in the same file).  
- Added `rag_fusion_k: int = Field(default=60, ...)` to `RAGSettings` in `config/settings.py`.  
**Impact:** RRF k can now be tuned per environment (larger k reduces score sensitivity to rank position for very large result sets).

---

#### R5 — Per-Chunk LLM Reranking Timeout
**Problem:** `CrossEncoderReranker._llm_rerank()` had no timeout on individual LLM calls. A single slow/hung call would block the entire reranking pipeline indefinitely.  
**Fix:**  
- Added `latency_budget_ms: int = 3000` parameter to `CrossEncoderReranker.__init__()`, stored as `self.latency_budget_ms`.  
- Extracted new `_llm_rerank_single(query, chunk)` async method that wraps the LLM call with `asyncio.wait_for(timeout=self.latency_budget_ms / 1000.0)`.  
- On `asyncio.TimeoutError`, logs a warning and returns `None` for that chunk (chunk is excluded from reranked results).  
- Updated `_llm_rerank()` to call `_llm_rerank_single()` per chunk.  
- Added `cross_encoder_latency_budget_ms: int = Field(default=3000, ...)` to `RAGSettings`.  
**Impact:** Reranking now has bounded worst-case latency. Slow LLM responses for individual chunks are skipped rather than stalling the pipeline.

---

#### R7 — Source-Specific Confidence Thresholds in SelfRAG
**Problem:** `SelfRAG` applied a single global `confidence_threshold` to all retrieved chunks regardless of source type (Confluence runbooks vs. ICM incidents have inherently different score distributions and reliability characteristics).  
**Fix:**  
- Added `DEFAULT_SOURCE_THRESHOLDS: dict[str, float] = {"confluence": 0.4, "icm": 0.7}` class variable.  
- Added `source_thresholds: Optional[dict[str, float]] = None` parameter to `__init__()`, defaulting to `DEFAULT_SOURCE_THRESHOLDS`.  
- Added `get_threshold_for_source(source_type: str) -> float` method — returns the source-specific threshold or falls back to `self.confidence_threshold`.  
- Added `filter_chunks_by_source_threshold(chunks) -> list` method — filters a chunk list using per-source thresholds based on each chunk's `metadata.get("source_type")`.  
- Added `self_rag_runbook_threshold: float = Field(default=0.4, ...)` and `self_rag_incident_threshold: float = Field(default=0.7, ...)` to `RAGSettings`.  
**Impact:** Confluence runbooks (typically lower cosine scores due to verbose prose) are retained at a lower threshold; ICM incidents require higher confidence before being used for grounding.

---

### `src/rag/retriever.py`

#### R6 — Expanded HyDE Hypothesis Templates
**Problem:** `HyDEGenerator.HYPOTHESIS_TEMPLATES` only covered generic/error/timeout/memory/performance query types, missing several common alert categories in the Alert Whisperer domain.  
**Fix:** Added five new template entries to `HYPOTHESIS_TEMPLATES`:
- `auth_permission` — authentication failures, permission denied, access control errors
- `network_connectivity` — network timeouts, DNS failures, connection refused, firewall/VNet issues
- `resource_throttling` — rate limiting, quota exceeded, throttling, capacity exhaustion
- `schema_drift` — schema changes, column mismatches, type incompatibilities, evolution failures
- `data_quality` — null values, duplicate records, data validation failures, format mismatches

Updated `_classify_for_template()` with keyword-based routing for each new template type.  
**Impact:** HyDE generates domain-appropriate hypothetical documents for a broader range of alert types, improving embedding similarity with actual runbook and incident content.

---

#### R8 — CRAG Secondary Retriever Fallback
**Problem:** `LLMJudgeCRAG` had no secondary retrieval path. When the primary retriever returned low-confidence results (judged as "IRRELEVANT"), there was no fallback mechanism.  
**Fix:** Added new `CRAGSecondaryRetriever` class (positioned before `LLMJudgeCRAG`) with:
- `retrieve_fallback(query, original_source_type, primary_chunks, min_relevance_threshold)` — orchestrates fallback by trying both broadened-query re-retrieval and alternative-source retrieval.
- `_broaden_query(query)` — strips specific technical tokens (pipeline names, error codes, numbers) to create a broader query for retry.
- `_pick_alternative_source(original_source_type)` — selects a complementary source type (e.g., if primary was "confluence", tries "icm"; if "icm", tries "confluence").  
**Impact:** CRAG now has a two-stage fallback: (1) retry with a broadened query against the same source, (2) try an alternative knowledge source. Reduces "no answer found" outcomes for ambiguous or overly-specific queries.

---

#### R9 — Real Topology Extraction Implementations
**Problem:** `GraphRAGRetriever._extract_adf_topology()`, `_extract_databricks_topology()`, and `_extract_synapse_topology()` contained only `pass` stubs, leaving the knowledge graph always empty.  
**Fix:** Implemented all three extraction methods with duck-typing to handle both SDK client objects and dict-style API responses:

- **`_extract_adf_topology()`** — Extracts ADF pipelines (nodes), activities within each pipeline (child nodes), datasets referenced by activities (leaf nodes), and linked services. Creates edges: pipeline→activity, activity→dataset, activity→linked_service.

- **`_extract_databricks_topology()`** — Extracts Databricks jobs (nodes), tasks within each job (child nodes), notebooks and clusters referenced by tasks (leaf nodes). Creates edges: job→task, task→notebook, task→cluster.

- **`_extract_synapse_topology()`** — Extracts Synapse pipelines (nodes), activities (child nodes), SQL pools (leaf nodes, extracted from SQL activity targets), and datasets. Creates edges: pipeline→activity, activity→sql_pool, activity→dataset.

**Impact:** The knowledge graph is populated with actual infrastructure topology, enabling graph-based traversal for root-cause reasoning (e.g., "which downstream activities fail when this dataset is unavailable?").

---

#### R14 — Weighted Uncertainty Pattern Scoring in FLARE
**Problem:** `FLARERetriever.detect_low_confidence_segments()` used a simple list of regexes with no weighting — every uncertainty signal was treated equally, leading to both false positives (weak hedges triggering retrieval) and missed detections.  
**Fix:**  
- Replaced the regex list with `UNCERTAINTY_PATTERNS: list[tuple[str, str, float]]` — each entry is `(pattern, signal_type, base_weight)`.  
- Added `UNCERTAINTY_SCORE_THRESHOLD: float = 0.5` class constant.  
- Rewrote `detect_low_confidence_segments()` to:
  1. Split the response into sentences.
  2. For each sentence, accumulate weighted scores from all matching patterns.
  3. Apply a position factor (`1.0 + 0.1 * (sentence_index / total)`) to give slightly higher weight to uncertainty signals that appear later (typically more conclusive statements).
  4. Only include sentences with aggregated score ≥ `UNCERTAINTY_SCORE_THRESHOLD`.
  5. Return segments sorted by score descending (highest-uncertainty segments retrieved first).  
**Impact:** Uncertainty detection is now graduated rather than binary. Strong signals ("I don't know", "cannot determine") carry more weight than weak hedges ("might", "could"), reducing unnecessary retrieval calls while catching genuine knowledge gaps.

---

#### R15 — Dynamic Topic Tree Updates from Documents
**Problem:** `TopicTreeRetriever` had a static topic tree initialized at construction time with no mechanism to incorporate patterns discovered in ingested documents.  
**Fix:** Added `update_topic_tree_from_documents(chunks, min_docs_for_topic=3)` method that:
1. Scans all document chunks for pipeline name patterns (regex: `[A-Z][a-z]+(?:[A-Z][a-z]+)+Pipeline|[a-z_]+_pipeline`) and error patterns (keywords: `Error`, `Exception`, `Failure`, `Timeout`, `OOM`, etc.).
2. Counts occurrences with `Counter`.
3. For pipeline names with count ≥ `min_docs_for_topic`: adds a leaf node under the "data_pipelines" topic subtree.
4. For error patterns with count ≥ `min_docs_for_topic`: adds a leaf node under the "errors" topic subtree.
5. Returns a dict with `pipelines_discovered`, `errors_discovered`, `nodes_added` counts.  
**Impact:** The topic tree evolves as the knowledge base grows, improving routing accuracy for new pipeline types and error classes without manual tree maintenance.

---

#### R16 — Per-Source Confidence Tracking in Agentic RAG
**Problem:** `AgenticRAGOrchestrator` had no memory of which retrieval sources historically perform well for different query types, forcing the planner to treat all sources as equally reliable.  
**Fix:**  
- Added `_source_stats: dict[str, dict]` to `__init__()`, initialized with slots for `icm`, `confluence`, `graph` sources (each tracking `attempts`, `hits`, `avg_score`).  
- Added `get_source_confidence(source: str) -> float` — returns the exponential moving average score for the source (or `0.5` as prior for unseen sources).  
- Added `record_retrieval_outcome(source, hit, score, query_type)` — updates `_source_stats` using EMA with `alpha=0.1`: `avg_score = (1-alpha)*avg_score + alpha*score`. Logs the updated confidence.  
- Updated `execute_plan()` to call `record_retrieval_outcome()` after each source query completes.  
**Impact:** The orchestrator accumulates a running confidence estimate per source. This can be used by the planner to deprioritize consistently low-performing sources and boost priority for high-confidence ones.

---

### `src/rag/vector_store.py`

#### R10 — Configurable Parent Chunk Overlap
**Problem:** `ParentChildChunkManager.create_parent_child_chunks()` called `_split_text_with_overlap()` for parent texts with `overlap_tokens=0` hardcoded, causing context discontinuities at parent chunk boundaries.  
**Fix:**  
- Added `parent_overlap_tokens: int = 100` parameter to `ParentChildChunkManager.__init__()`, stored as `self.parent_overlap_tokens`.  
- Updated `create_parent_child_chunks()` to pass `overlap_tokens=self.parent_overlap_tokens` when splitting parent texts.  
- Added `parent_overlap_tokens: int = Field(default=100, ...)` to `RAGSettings`.  
**Impact:** Parent chunks now have configurable overlap, preserving cross-boundary context in long technical documents (runbooks, incident reports) where key information spans chunk edges.

---

#### R11 — Final Min-Chunk-Size Enforcement Pass
**Problem:** `SemanticChunker._build_chunks_from_breakpoints()` enforced `min_chunk_tokens` during the initial merge phase but could still produce sub-minimum fragments in edge cases (e.g., the last chunk being too small, or semantic breakpoints creating isolated short segments after the merge loop).  
**Fix:** Added a final enforcement pass at the end of `_build_chunks_from_breakpoints()` that iterates the assembled chunk list and merges any remaining chunk below `min_chunk_tokens` into the preceding chunk. The merge uses `"\n\n"` as a separator to preserve readability.  
**Impact:** Guarantees no fragment smaller than `min_chunk_tokens` escapes the chunker regardless of breakpoint placement, preventing tiny near-empty chunks from polluting the vector store with low-signal embeddings.

---

#### R12 — BM25 Field Weighting for Pipeline and Error Metadata
**Problem:** `BM25Index` treated all document terms uniformly — a match on `pipeline_name` carried the same weight as a match on incidental prose words, despite pipeline names and error classes being far more discriminative for alert routing.  
**Fix:**  
- Added `DEFAULT_FIELD_WEIGHTS: dict[str, float] = {"pipeline_name": 2.0, "error_class": 2.0, "content": 1.0}` class variable.  
- Added `field_weights: Optional[dict[str, float]] = None` parameter to `__init__()`.  
- Updated `add_document()` to accept an optional `metadata` dict; when `pipeline_name` or `error_class` metadata fields are present, their tokens are repeated in the indexed document text proportional to their field weights (e.g., weight 2.0 = tokens appear twice), applying TF boosting without modifying the BM25 algorithm itself.  
- Updated `add_documents_batch()` with `metadata_map: Optional[dict[str, dict]] = None` parameter.  
- Updated `VectorStore.add_documents()` to extract `pipeline_name` and `error_class` from chunk metadata and pass them to the BM25 indexer.  
**Impact:** BM25 keyword search strongly prefers documents matching the pipeline name or error class from the query, improving hybrid retrieval precision for alert-specific queries.

---

#### R13 — Progress Callback for Batch Ingestion
**Problem:** `VectorStore.add_documents()` provided no progress reporting during large batch ingestion, making it impossible for callers (UI, CLI, scheduled jobs) to show progress or detect stalls.  
**Fix:**  
- Added `progress_callback: Optional[Any] = None` parameter to `VectorStore.add_documents()`.  
- Added `total = len(chunks)` tracking variable.  
- After each batch upload completes, calls `progress_callback(processed=min(i+batch_size, total), total=total, batch_added=batch_added)` inside a `try/except` to ensure callback errors never abort ingestion.  
**Impact:** Callers can wire up progress bars (tqdm), logging callbacks, or webhook notifications without modifying the ingestion core.

---

#### R17 — RetrievalTelemetry Class
**Problem:** The RAG system had no unified telemetry layer for tracking retrieval effectiveness metrics (hit rate, latency, relevance scores) across different retrieval techniques, making it impossible to compare and tune techniques systematically.  
**Fix:** Added new `RetrievalTelemetry` class at the top of `vector_store.py` with:
- `record(technique, hit, latency_ms, relevance_score, source_type)` — records a single retrieval event, tracking hits, latency samples, and relevance scores per technique and per source.
- `get_stats(technique=None) -> dict` — returns aggregated metrics including `hit_rate`, `avg_latency_ms`, `p95_latency_ms` (computed from sorted latency list at 95th percentile index), `avg_relevance_score`, and per-source breakdowns.
- `reset(technique=None)` — clears stats for a specific technique or all techniques.
- `summary_log()` — emits a structured log line via `structlog` for each tracked technique (suitable for Datadog/Splunk ingestion).  
**Impact:** Provides a lightweight, zero-dependency telemetry layer that can be instantiated globally or per-component. Enables A/B comparison of retrieval techniques and latency SLO monitoring without requiring an external metrics backend.

---

## Config Additions (`config/settings.py` — `RAGSettings` class)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `rag_fusion_k` | `int` | `60` | Configurable RRF k for RAGFusion (R4) |
| `cross_encoder_latency_budget_ms` | `int` | `3000` | Per-chunk LLM reranking timeout (R5) |
| `self_rag_runbook_threshold` | `float` | `0.4` | SelfRAG confidence threshold for Confluence runbooks (R7) |
| `self_rag_incident_threshold` | `float` | `0.7` | SelfRAG confidence threshold for ICM incidents (R7) |
| `parent_overlap_tokens` | `int` | `100` | Parent chunk overlap in ParentChildChunkManager (R10) |

---

## Skipped Gaps

None. All 17 gaps (R1–R17) were implemented in full.

---

## Notes on Implementation Approach

1. **Edit-only, no rewrites** — All changes used targeted `edit` tool calls (string replacement). No file was rewritten wholesale.
2. **Backward compatibility** — All new parameters use defaults matching the original behavior where possible, ensuring existing call sites require no changes.
3. **Structural integrity** — Two structural issues were caught and corrected during implementation:
   - The `logger.info` + `return result` lines at the end of `SelfRAG.evaluate_and_enhance()` were temporarily displaced when inserting R7 methods; corrected with a follow-up edit.
   - A duplicate `total_time += result.retrieval_time_ms` line was introduced in `AgenticRAGOrchestrator.execute_plan()` during R16; removed with a follow-up edit.
4. **Import additions** — `asyncio` was added to `advanced_techniques.py` imports (required for R5's `asyncio.wait_for()`). All other required types (`Optional`, `defaultdict`, `Counter`) were already imported in the respective files.
