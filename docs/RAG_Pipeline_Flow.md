# Alert Whisperer — RAG Pipeline Flow Documentation

## Overview

The Alert Whisperer RAG (Retrieval-Augmented Generation) pipeline processes user queries through a sophisticated multi-stage system that combines 13 distinct techniques across 4 phases. This document explains how a query flows from the user's input through to the final generated response, detailing when and why each technique is invoked.

---

## Architecture Summary

```
User Query
    │
    ▼
┌───────────────────────────────────────────────┐
│  PHASE 1: PRE-RETRIEVAL                       │
│  ┌─────────────────┐  ┌────────────────────┐  │
│  │ Query Rewriting  │  │ RAG Fusion         │  │
│  │ (synonyms,      │  │ (multi-query       │  │
│  │  abbreviations,  │  │  decomposition)    │  │
│  │  context enrich) │  │                    │  │
│  └────────┬────────┘  └────────┬───────────┘  │
│           │                    │               │
│  ┌────────▼────────┐  ┌───────▼────────────┐  │
│  │ HyDE            │  │ Topic Tree         │  │
│  │ (hypothetical   │  │ (hierarchical      │  │
│  │  document gen)  │  │  classification)   │  │
│  └────────┬────────┘  └────────┬───────────┘  │
│           │                    │               │
│           ▼                    ▼               │
│     [Multiple optimised query variants]       │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│  PHASE 2: RETRIEVAL                           │
│  ┌─────────────────────────────────────────┐  │
│  │ Azure AI Search Hybrid                  │  │
│  │ (Vector 3072d + BM25 keyword)           │  │
│  │ + Semantic Reranking (L2 cross-encoder) │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ Agentic RAG Orchestrator                │  │
│  │ (routes to Confluence/ICM/LogAnalytics) │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ Parent-Child Chunk Expansion            │  │
│  │ (child retrieval → parent context)      │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ Semantic Chunking                       │  │
│  │ (embedding-similarity boundary detect)  │  │
│  └──────────────────┬──────────────────────┘  │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│  PHASE 3: POST-RETRIEVAL                      │
│  ┌─────────────────────────────────────────┐  │
│  │ CRAG (Corrective RAG)                   │  │
│  │ (relevance grading → filter incorrect)  │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ GraphRAG                                │  │
│  │ (dependency context injection)          │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ Cross-Encoder Reranking                 │  │
│  │ (joint query-doc scoring, ~15-20%       │  │
│  │  precision improvement)                 │  │
│  └──────────────────┬──────────────────────┘  │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────┐
│  PHASE 4: DURING GENERATION                   │
│  ┌─────────────────────────────────────────┐  │
│  │ LLM Generation (GPT-4o)                 │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ FLARE (Forward-Looking Active Retrieval)│  │
│  │ (retrieves more if confidence drops)    │  │
│  └──────────────────┬──────────────────────┘  │
│                     │                         │
│  ┌──────────────────▼──────────────────────┐  │
│  │ Self-RAG                                │  │
│  │ ([Supported] [Relevant] [Useful] eval)  │  │
│  └──────────────────┬──────────────────────┘  │
└──────────────────────┬────────────────────────┘
                       │
                       ▼
                Final Response
```

---

## Phase 1: Pre-Retrieval

Pre-retrieval techniques run BEFORE any vector/keyword search to optimise the query for maximum recall.

### 1.1 Query Rewriting

**When it fires:** Every query (unless disabled via `RAG_QUERY_REWRITING_ENABLED=false`)

**What it does:**
1. **Synonym Expansion** — Expands domain abbreviations: "OOM" → "OutOfMemoryError Java heap space executor memory"
2. **Abbreviation Expansion** — Expands infrastructure abbreviations: "ADX" → "Azure Data Explorer Kusto"
3. **Context Enrichment** — If an active alert exists, appends `pipeline:name error:class source:type` to the query
4. **LLM Rewriting** (optional) — Uses GPT-4o-mini to generate an explicit retrieval query from terse input

**Why it matters for Spark/Synapse/Kusto:**
Support engineers type terse queries like "OOM on daily job" that don't match the verbose Confluence runbook language. Query rewriting bridges this vocabulary gap, improving recall by 20-30%.

**Code path:** `QueryRewriter.rewrite()` → called from `RAGRetriever.retrieve_for_alert()` and `RAGRetriever.retrieve_for_query()`

**Input/Output Example:**
```
Input:  "OOM on daily job"
Output: [
    "OOM on daily job",                                    (original)
    "OOM on daily job OutOfMemoryError Java heap space executor memory",  (synonym expanded)
    "OOM (OutOfMemoryError) on daily job",                 (abbreviation expanded)
    "OOM on daily job pipeline:spark_etl_daily_ingest error:OutOfMemoryError source:spark"  (context enriched)
]
```

### 1.2 RAG Fusion

**When it fires:** For freeform conversational queries AND alert-triggered retrieval

**What it does:**
1. Generates 3 query variants (LLM-based or template-based):
   - Cause-focused: "root cause analysis: {query}"
   - Resolution-focused: "resolution steps fix workaround: {query}"
   - Similar incidents: "similar incidents past occurrences: {query}"
2. Runs independent retrieval for each variant
3. Merges results using **Reciprocal Rank Fusion (RRF):** `RRF_score(d) = Σ 1/(60 + rank_i(d))`

**Why it matters:**
Complex questions like "Why did the Spark job OOM and how do I prevent it from blocking Kusto ingestion?" span multiple domains. A single query can't optimally retrieve all facets. RAG Fusion decomposes into separate retrievals and fuses intelligently.

**Code path:** `RAGFusion.retrieve_and_fuse()` → `RAGFusion.generate_query_variants()` → `RAGFusion.fuse_results()`

### 1.3 HyDE (Hypothetical Document Embeddings)

**When it fires:** During alert-triggered retrieval (`retrieve_for_alert`)

**What it does:**
Generates a hypothetical resolution document based on the error classification. This document is embedded and used for retrieval instead of the raw query, because it's semantically closer to actual runbook content.

**Templates used:**
- `spark_oom` — For OOM/heap/GC errors
- `spark_shuffle` — For shuffle fetch failures
- `synapse_timeout` — For Synapse pipeline timeouts
- `kusto_ingestion` — For Kusto ingestion mapping errors
- `generic` — Fallback for unclassified errors

**Why it matters:**
User queries describe symptoms ("job keeps failing at stage 12") but documents describe solutions. HyDE generates what the ideal resolution doc would look like, creating an embedding bridge between symptom-language and solution-language.

**Code path:** `HyDEGenerator.generate_hypothetical_document()`

### 1.4 Topic Tree Classification

**When it fires:** During alert-triggered retrieval

**What it does:**
Classifies the error into a hierarchical topic tree:
```
Data Pipeline Errors
├── Spark Errors
│   ├── OutOfMemory
│   ├── Shuffle Failures
│   ├── Executor Lost
│   ├── Schema Mismatch
│   └── Spark Timeout
├── Synapse Errors
│   ├── Pipeline Timeout
│   ├── Copy Activity Failures
│   ├── SQL Pool Errors
│   └── Authentication Failures
├── Kusto Errors
│   ├── Ingestion Failures
│   ├── Query Failures
│   └── Cluster Issues
└── Infrastructure Errors
    ├── Network Connectivity
    ├── Storage Issues
    └── Authentication & Secrets
```

The matched topic branches are appended to the retrieval query for targeted search.

**Code path:** `TopicTreeRetriever.classify_error()`

---

## Phase 2: Retrieval

### 2.1 Azure AI Search Hybrid (Vector + BM25)

**When it fires:** Every search call (core retrieval engine)

**What it does:**
Sends the query to Azure AI Search which executes BOTH:
1. **Vector Search** — 3072-dimensional HNSW index (text-embedding-3-large) for semantic similarity
2. **BM25 Keyword Search** — Full-text search for exact matches (error codes, config keys, stack traces)
3. **Score Fusion** — Azure merges both signals server-side using `hybrid_alpha` weighting (default 0.6 = 60% vector, 40% BM25)
4. **Semantic Reranking** (optional) — Azure's L2 cross-encoder reranks the merged results for relevance

**Why Azure AI Search over ChromaDB:**
- Native hybrid search (no client-side BM25)
- Built-in semantic reranking (L2 model)
- Enterprise SLA, geo-redundancy, managed infrastructure
- 3072-dim HNSW handles high-dimensional embeddings efficiently

**Fallback:** If Azure AI Search is not configured, falls back to in-memory cosine similarity + BM25 index.

**Code path:** `VectorStore.search()` → `VectorStore._search_azure()` or `VectorStore._search_inmemory()`

### 2.2 Agentic RAG Orchestrator

**When it fires:** For freeform queries without a specific source filter

**What it does:**
Plans which data sources to query based on the query intent:

| Query Intent | Primary Source | Secondary Source | Reason |
|---|---|---|---|
| "similar incidents" | ICM | Confluence | Historical incidents in ICM, patterns in docs |
| "runbook steps" | Confluence | — | Procedures live in Confluence |
| "show me the logs" | Log Analytics | — | Fresh log data |
| "root cause" | ICM + Confluence | Graph | Past RCAs + architecture + dependency context |
| General | All sources | Graph | Cast a wide net |

**Code path:** `AgenticRAGOrchestrator.plan_retrieval()` → `AgenticRAGOrchestrator.execute_plan()`

### 2.3 Parent-Child Chunking

**When it fires:** During document indexing AND retrieval expansion

**What it does:**
- **Indexing:** Documents are split into parent chunks (~1200 tokens) which are further split into child chunks (~450 tokens). Child chunks are embedded and indexed; parent chunks are stored for context expansion.
- **Retrieval:** When a child chunk is retrieved, it can be expanded to the full parent chunk for richer context in the LLM prompt.

**Chunk sizes (token-based):**
| Level | Size (tokens) | Size (chars approx) | Purpose |
|---|---|---|---|
| Parent | ~1200 | ~4800 | Full narrative context |
| Child | 400-512 | ~1600-2000 | Precise embedding retrieval |
| Overlap | 50 | ~200 | Continuity between chunks |

**Why token-based over character-based:**
- LLM context windows are measured in tokens
- text-embedding-3-large was TRAINED on token-aligned inputs
- Consistent semantic density per chunk

**Code path:** `ParentChildChunkManager.create_parent_child_chunks()` → `VectorStore.add_documents_with_parent_child()`

### 2.4 Semantic Chunking

**When it fires:** During document indexing (alternative to fixed-token chunking)

**What it does:**
1. Splits document text into sentences
2. Embeds each sentence using text-embedding-3-large (3072 dims)
3. Computes cosine similarity between consecutive sentence embeddings
4. Identifies similarity drops below a percentile threshold (default: 25th percentile)
5. Groups sentences between breakpoints into semantically coherent chunks
6. Enforces min/max token limits (100-800 tokens) to avoid degenerate chunks

**Why it matters for Spark/Synapse/Kusto:**
Fixed-size token chunking splits mid-concept — a 450-token chunk might cut between "Root Cause" and "Resolution" sections of a Confluence runbook, losing the cause→fix linkage. Semantic chunking keeps related content together by detecting topic boundaries via embedding similarity. Spark error logs follow irregular structure — semantic chunking keeps a stack trace + its root cause summary together as one unit.

**Fallback:** When the embedding client is unavailable, falls back to token-based splitting (max_chunk_tokens=800, overlap=50).

**Code path:** `SemanticChunker.chunk()` → `VectorStore.add_documents_with_semantic_chunking()`

**Configuration:**
- `RAG_SEMANTIC_CHUNKING_ENABLED` — Enable/disable (default: `true`)
- `RAG_SEMANTIC_CHUNKING_PERCENTILE` — Breakpoint percentile threshold (default: `25.0`)

---

## Phase 3: Post-Retrieval

### 3.1 CRAG (Corrective RAG)

**When it fires:** After initial retrieval, before reranking

**What it does:**
Grades each retrieved chunk for relevance to the query:
1. **CORRECT** (score ≥ 0.5) — Keep the chunk
2. **AMBIGUOUS** (0.3 ≤ score < 0.5) — Keep but at lower priority
3. **INCORRECT** (score < 0.3) — Remove from context

If ALL chunks are INCORRECT, returns empty context (signals "no relevant knowledge found" to the LLM, which then uses parametric knowledge).

**Two evaluators available:**
- `CRAGEvaluator` — Fast heuristic (token overlap + vector similarity)
- `LLMJudgeCRAG` — LLM-as-judge for higher-precision grading (optional)

**Why it matters:**
Generic OOM runbooks get retrieved for specific Delta Lake compaction OOMs — CRAG detects this mismatch. Old Spark 2.x advice may match keywords but is wrong for Spark 3.x clusters — CRAG filters it out.

**Code path:** `CRAGEvaluator.grade_retrieval()` → `CRAGEvaluator.apply_correction()`

### 3.2 GraphRAG

**When it fires:** During alert-triggered retrieval, when a pipeline name is known

**What it does:**
Queries the pipeline dependency graph to inject:
1. **Blast Radius** — Which downstream pipelines and components are affected
2. **Failure Chain** — Upstream → failure point → downstream impact trace
3. **Common Error Associations** — Historical error patterns for this pipeline

**Example output injected into context:**
```
**Blast Radius:** 2 pipelines, 1 components
Affected pipelines: synapse_pipeline_customer360, kusto_ingestion_telemetry
Affected components: ADLS Gen2 Curated Zone

**Failure Chain:** ADLS Gen2 Raw Zone (READS_FROM) → spark_etl_daily_ingest (OutOfMemoryError)
→ synapse_pipeline_customer360 (BLOCKED_BY_FAILURE) → kusto_ingestion_telemetry (BLOCKED_BY_FAILURE)

**Common Errors for this Pipeline:** OutOfMemoryError, ShuffleFetchFailure
```

**Code path:** `GraphRAGEngine.get_related_context()` → `GraphRAGEngine.trace_blast_radius()` + `GraphRAGEngine.find_failure_chain()`

**Dynamic Graph Population:**
In production, the static default graph is replaced with live pipeline topology from ADF, Databricks, and Synapse metadata APIs. Call `GraphRAGEngine.refresh_from_live_metadata()` on application startup or on a schedule (e.g., hourly) to keep the graph current. Supported sources:
- Azure Data Factory: Extracts pipeline activities, dataset dependencies, and inter-pipeline edges
- Databricks: Extracts job definitions, task dependencies, and cluster references
- Synapse: Extracts pipeline activities, SQL pool references, and dataset chains

**Code path (dynamic):** `GraphRAGEngine.refresh_from_live_metadata()` → `_extract_adf_topology()` / `_extract_databricks_topology()` / `_extract_synapse_topology()`

### 3.3 Cross-Encoder Reranking

**When it fires:** After CRAG filtering and GraphRAG context injection

**Implementation paths (by priority):**
1. **Azure AI Search Semantic Reranking** (server-side L2 cross-encoder) — zero client-side latency, handled in `VectorStore._search_azure()`
2. **Dedicated Cross-Encoder Model** (`ms-marco-MiniLM-L-12-v2` via `sentence-transformers`) — ~150-300ms total for 10 chunks (batch inference). Enable with `RAG_USE_DEDICATED_RERANKER=true`.
3. **LLM-as-Reranker** (GPT-4o-mini) — ~200-500ms per chunk, 2-5s total for 10 chunks. Higher cost but more nuanced relevance reasoning.
4. **Heuristic Fallback** — keyword overlap + metadata weighting, zero external calls.

**Latency comparison:**
| Method | Latency (10 chunks) | Precision | Cost |
|---|---|---|---|
| Azure Semantic Reranking | ~0ms (server-side) | High | Included in search pricing |
| Dedicated Cross-Encoder | ~150-300ms | High | Local compute only |
| LLM-as-Reranker (GPT-4o-mini) | ~2-5 seconds | Highest | Per-token API cost |
| Heuristic | ~1-5ms | Medium | Zero |

**Precision improvement:** ~15-20% over bi-encoder scoring alone.

**Why it matters:**
Spark OOM errors have many subtypes (skew, broadcast, spill, GC) that share vocabulary but require different resolutions. Cross-encoders distinguish these by attending to both query AND document tokens together.

**Code path:** `CrossEncoderReranker.rerank()` → `CrossEncoderReranker._llm_rerank()` or `CrossEncoderReranker._heuristic_rerank()`

---

## Phase 4: During Generation

### 4.1 FLARE (Forward-Looking Active Retrieval)

**When it fires:** During LLM response generation, when confidence drops

**What it does:**
1. Scans generated text for low-confidence indicators:
   - Hedging language: "might be", "possibly", "I'm not sure"
   - Placeholder markers: `[unknown]`, `[TBD]`
   - Generic advice: "check the logs", "review documentation"
   - Information gaps: "more information needed"
2. For each low-confidence segment, triggers additional retrieval
3. Deduplicates new chunks against existing context
4. Injects new context for the remaining generation

**Why it matters:**
Simple questions ("what's the runbook for OOM?") don't need extra retrieval. Complex cascading failures (Spark → ADLS → Kusto) benefit from mid-generation retrieval when the LLM realises it needs info about a downstream component.

**Code path:** `FLARERetriever.detect_low_confidence_segments()` → `FLARERetriever.retrieve_for_segments()`

### 4.2 Self-RAG

**When it fires:** After LLM generation, before returning to the user

**What it does:**
Self-reflective evaluation of the generated response:
1. Splits response into segments (~500 chars each)
2. For each segment, evaluates:
   - **[Supported]** — Is the claim supported by retrieved evidence? (yes/partial/no)
   - **[Relevant]** — Is it relevant to the query? (yes/no)
   - **[Needs Retrieval]** — Does it need more context? (yes/no)
   - **[Confidence]** — Numeric confidence score (0.0-1.0)
3. If a segment needs retrieval, triggers additional search (max 2 rounds)
4. Aggregates evaluations into overall confidence + warnings

**Output format:**
```json
{
    "evaluation": {
        "supported": "partial",
        "confidence": 0.72,
        "retrieval_rounds": 1
    },
    "warnings": [
        "Some claims are only partially supported. Review the referenced context for full details."
    ]
}
```

**Why it matters:**
Prevents hallucinated Spark configs (e.g., "set spark.sql.adaptive.skewThreshold=0.5" is fabricated). Validates each resolution step against retrieved runbook content. Signals low confidence rather than generating plausible-sounding but unverified advice.

**Code path:** `SelfRAG.evaluate_and_enhance()` → `SelfRAG._evaluate_segment()`

---

## Complete Pipeline Execution Order

For an alert-triggered query (e.g., a Spark OOM error is detected):

```
1. Topic Tree Classification      → 0-1ms    (keyword matching)
2. Query Rewriting                 → 0-50ms   (synonym + abbreviation expansion)
   └─ LLM Rewriting (optional)    → +200ms   (GPT-4o-mini)
3. HyDE Document Generation        → 0-2ms    (template rendering)
4. RAG Fusion Variant Generation   → 0-200ms  (LLM-based) or 0ms (template)
5. Azure AI Search Hybrid          → 50-150ms (per query variant, parallel)
   └─ Vector Search (3072d HNSW)  
   └─ BM25 Keyword Search
   └─ Semantic Reranking (L2)
6. Agentic Source Routing          → 0-50ms   (intent classification)
7. Parent-Child Expansion          → 0-5ms    (in-memory lookup)
7.5 Semantic Chunking             → At indexing time (not per-query)
8. CRAG Grading + Filtering        → 1-10ms   (heuristic) or 200-500ms (LLM judge)
9. GraphRAG Context Injection      → 0-5ms    (graph traversal)
10. Cross-Encoder Reranking        → 1-10ms   (heuristic) or 200-500ms (LLM)
11. Context Window Trimming        → 0-1ms    (character budget)
12. LLM Generation (GPT-4o)        → 1-5s     (main response)
13. FLARE Active Retrieval         → 0-500ms  (triggered only if confidence drops)
14. Self-RAG Evaluation            → 0-800ms  (post-generation quality check)
```

**Total latency (typical):** 2-8 seconds end-to-end
**Total latency (with all LLM-based techniques):** 5-12 seconds

---

## Configuration Toggles

Each technique can be independently enabled/disabled via environment variables:

| Technique | Env Variable | Default |
|---|---|---|
| Query Rewriting | `RAG_QUERY_REWRITING_ENABLED` | `true` |
| RAG Fusion | `RAG_RAG_FUSION_ENABLED` | `true` |
| HyDE | Enabled when `enable_hyde=True` (constructor) | `true` |
| Topic Tree | Always enabled | — |
| Hybrid Search | `RAG_HYBRID_ALPHA` (0 = vector only, 1 = BM25 only) | `0.6` |
| Agentic RAG | Enabled when `enable_agentic=True` (constructor) | `true` |
| Parent-Child | Always enabled | — |
| CRAG | Enabled when `enable_crag=True` (constructor) | `true` |
| Cross-Encoder | `RAG_RERANK_ENABLED` | `true` |
| GraphRAG | Enabled when `enable_graph=True` (constructor) | `true` |
| FLARE | Enabled when VectorStore is available | `true` |
| Self-RAG | `RAG_SELF_RAG_ENABLED` | `true` |
| Semantic Reranking | `RAG_SEMANTIC_RERANKING_ENABLED` | `true` |
| Semantic Chunking | `RAG_SEMANTIC_CHUNKING_ENABLED` | `true` |
| Dedicated Cross-Encoder | `RAG_USE_DEDICATED_RERANKER` | `false` |

---

## Fallback Behaviour

The system is designed to gracefully degrade:

1. **Azure AI Search unavailable** → Falls back to in-memory cosine similarity + BM25
2. **OpenAI API unavailable** → Embeddings return zero vectors; retrieval still works via BM25
3. **LLM client unavailable** → Query Rewriting uses only synonym/abbreviation expansion (no LLM); Cross-Encoder uses heuristic scoring; CRAG uses token overlap; Self-RAG uses heuristic evaluation
4. **tiktoken unavailable** → Token-based chunking falls back to character-based (~4 chars/token estimate)
5. **Graph config missing** → GraphRAG uses built-in default pipeline dependency graph
6. **Semantic chunking unavailable** → Falls back to token-based splitting (400-512 tokens per child chunk)
7. **Dedicated cross-encoder unavailable** → Falls back to LLM-as-reranker, then to heuristic scoring
