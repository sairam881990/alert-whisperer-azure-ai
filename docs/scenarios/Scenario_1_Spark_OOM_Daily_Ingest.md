# Scenario 1 — Spark OOM on Daily Ingest (End-to-End, All 13 Techniques)

> **Audience:** Senior Data Engineer building Alert Whisperer — a conversational troubleshooting assistant backed by Azure AI Search, `text-embedding-3-large` (3072 dims), and 13 RAG techniques for Spark/Synapse/Kusto incident response.
>
> **Purpose:** A complete, concrete walkthrough of every technique firing in sequence on a single real-world incident. Not abstract theory — every input, output, threshold, and config value is specified.

---

## Scenario Setup

| Field | Value |
|---|---|
| **Pipeline** | `spark_etl_daily_ingest` on a Databricks cluster (Standard_DS4_v2 × 12 workers) |
| **Error** | `java.lang.OutOfMemoryError: Java heap space` in executor during stage 12 (shuffle read) |
| **Data scale** | ~500 GB of raw events from ADLS Gen2 Raw Zone → Delta table in ADLS Gen2 Curated Zone |
| **Detection time** | 03:45 AM CDT via ICM alert (ICM-2847391) |
| **Cluster config** | `spark.executor.memory=4g`, `spark.executor.memoryOverhead=512m`, `spark.sql.shuffle.partitions=200` |
| **Triggering query** | Wide join between `raw_events` (450 GB) and `dim_customers` (50 GB) on `customer_id` |
| **Suspected root cause** | Data skew on `customer_id` column — one executor receiving 40% of shuffle data |

**Raw ICM Alert Payload:**
```
ALERT: spark_etl_daily_ingest FAILED
Time: 2026-03-08T08:45:12Z
Severity: 2
Message: java.lang.OutOfMemoryError: Java heap space
  at org.apache.spark.storage.DiskBlockObjectWriter.write(DiskBlockObjectWriter.scala:220)
  at org.apache.spark.shuffle.sort.UnsafeShuffleWriter.insertRecordIntoSorter(UnsafeShuffleWriter.java:247)
Stage: 12 (ShuffleMapStage)
Executor: executor-008
Heap: 4096 MB allocated, 4096 MB used
Pipeline owner: data-platform-team@contoso.com
```

The user's opening message to Alert Whisperer:

> **User:** "java.lang.OutOfMemoryError: Java heap space on spark_etl_daily_ingest"

What follows is the complete trace of all 13 techniques processing this message.

---

## Phase 1: Pre-Retrieval

*Before a single document is fetched, three techniques work together to transform the raw error message into a rich, multi-perspective set of retrieval signals.*

---

### Technique 1: Topic Tree Classification

**What it is:** A hierarchical taxonomy of error categories maintained as a lightweight in-memory tree. Every incoming query is routed to one or more leaf nodes before retrieval begins. This scopes all subsequent retrieval to a relevant subset of the knowledge base.

**Input:**
```
"java.lang.OutOfMemoryError: Java heap space"
```

**Topic tree structure (relevant branch):**
```
Infrastructure Errors
└── Data Pipeline Errors
    ├── Spark Errors
    │   ├── OutOfMemory           ← PRIMARY MATCH (score: 0.91)
    │   ├── ShuffleFetchFailure
    │   └── StageFailure
    ├── Shuffle Failures          ← SECONDARY MATCH (score: 0.74, because stage 12 is shuffle)
    │   ├── FetchFailed
    │   └── ShuffleMapOutputLost
    ├── Synapse Errors
    │   ├── DWU Exhaustion
    │   └── PolyBase Failures
    └── Kusto Errors
        ├── Throttling
        └── IngestionFailure
```

**Classification logic:**
- Keyword match: "OutOfMemoryError" → `Spark Errors > OutOfMemory` (exact node keyword match, score boost +0.3)
- Keyword match: "Java heap space" → reinforces `OutOfMemory` (secondary keyword match, score +0.1)
- Contextual signal: "stage 12 (shuffle read)" from ICM alert payload → `Shuffle Failures` partial match (score 0.74)
- Nodes with score < 0.5 suppressed: Synapse Errors (0.08), Kusto Errors (0.04) → excluded

**Output:**
```python
topic_context = "OutOfMemory | Shuffle Failures"
topic_filter_tags = ["spark_oom", "shuffle", "executor_memory"]
excluded_tags = ["synapse", "kusto", "adls_permissions", "networking"]
```

**Why this matters:**

The Azure AI Search index contains ~12,000 chunks across Confluence runbooks, ICM incidents, Kusto KQL guides, Synapse documentation, and ADLS troubleshooting. Without topic filtering, a vector search for "Java heap space" retrieves chunks about JVM tuning in Synapse SQL pools, Java SDK configuration for ADLS, and even Kusto ingestion worker OOM errors — none of which are actionable for a Spark executor OOM.

By gating on `topic_filter_tags = ["spark_oom", "shuffle", "executor_memory"]`, the effective search space shrinks from 12,000 chunks to ~380 chunks. This is not just a performance optimization: it directly improves precision because the downstream vector search is less likely to be distracted by high-dimensional false positives in adjacent domains.

**What "Shuffle Failures" catches that "OutOfMemory" alone misses:**

The `ShuffleFetchFailure` runbook contains a critical note: "If shuffle fetch fails are preceded by executor OOM on the map side, increase `spark.executor.memoryOverhead` before repartitioning." This chunk would score lower under a pure OOM filter but is retrieved because the secondary classification tag `shuffle` is active.

---

### Technique 2: Query Rewriting

**What it is:** A multi-stage query transformation pipeline that converts the raw, often terse alert message into multiple rich query variants. Each variant targets a different retrieval facet: synonyms, abbreviations, pipeline-specific context, and LLM-generated natural language rewrites.

**Input:**
```
"java.lang.OutOfMemoryError: Java heap space"
```

**Stage 2a — Synonym Expansion:**

The synonym dictionary maps known Spark error tokens to their documentation aliases:

```python
SYNONYM_MAP = {
    "OutOfMemoryError": ["OOM", "heap exhaustion", "executor memory exceeded", "GC overhead limit exceeded"],
    "Java heap space": ["heap space", "executor heap", "JVM heap", "-Xmx"],
    "executor": ["worker node", "task runner", "Spark worker"],
    "shuffle read": ["shuffle fetch", "reduce phase", "sort-merge join read"],
}
```

After expansion:
```
"java.lang.OutOfMemoryError: Java heap space OR OOM OR heap exhaustion executor memory exceeded"
```

**Stage 2b — Abbreviation Expansion:**

Abbreviation scanner finds no abbreviations in this error (no ADLS, ETL, DWU, etc. present in the raw message). No expansion needed.

**Stage 2c — Context Enrichment:**

Structured metadata from the ICM alert is appended as context tags:
```
pipeline:spark_etl_daily_ingest
error:OutOfMemoryError
stage:shuffle_read
source:spark
cluster:databricks
```

Enriched query:
```
"java.lang.OutOfMemoryError Java heap space executor memory pipeline:spark_etl_daily_ingest error:OutOfMemoryError source:spark"
```

**Stage 2d — LLM Rewriting (GPT-4o-mini):**

Prompt sent to GPT-4o-mini:
```
System: You are a search query optimizer for a Spark/Databricks knowledge base.
Rewrite the following alert into a natural language query that would match runbooks,
incident reports, and tuning guides. Include likely root causes and resolution areas.

Alert: java.lang.OutOfMemoryError: Java heap space in executor during stage 12 (shuffle read)
Pipeline: spark_etl_daily_ingest
Context: 500GB daily ingest, Databricks, ADLS Gen2
```

GPT-4o-mini output:
```
"Spark executor OutOfMemoryError during shuffle read stage, daily ingest pipeline on Databricks,
possible causes include data skew causing uneven partition distribution, insufficient
spark.executor.memory or spark.executor.memoryOverhead configuration, or large broadcast
variables exceeding the broadcast threshold. Resolution involves repartitioning, enabling
adaptive query execution skew join handling, or increasing executor memory allocation."
```

**Final output — 4 query variants:**

| Variant | Type | Length |
|---|---|---|
| `V1` | Synonym-expanded | ~25 tokens |
| `V2` | Context-enriched | ~30 tokens |
| `V3` | LLM-rewritten | ~65 tokens |
| `V4` | Original (preserved for BM25 exact match) | ~8 tokens |

```python
query_variants = [
    # V1: Synonym-expanded
    "java.lang.OutOfMemoryError Java heap space OOM heap exhaustion executor memory exceeded",
    # V2: Context-enriched
    "java.lang.OutOfMemoryError Java heap space executor memory pipeline:spark_etl_daily_ingest source:spark stage:shuffle_read",
    # V3: LLM-rewritten (natural language)
    "Spark executor OutOfMemoryError during shuffle read stage daily ingest pipeline Databricks data skew spark.executor.memory memoryOverhead repartition adaptive query execution",
    # V4: Original (kept verbatim for BM25)
    "java.lang.OutOfMemoryError: Java heap space",
]
```

**Why this matters:**

The raw error string `"java.lang.OutOfMemoryError: Java heap space"` is a Java JVM exception that appears in hundreds of different contexts — Kafka consumers, HDFS NameNode, Spring Boot services, and Spark executors all throw identical stack trace headers. A BM25 search on the raw string would retrieve a Kafka consumer OOM runbook with near-equal score to the Spark executor OOM runbook.

The LLM rewrite (V3) is the highest-value variant: it introduces terms like `data skew`, `spark.executor.memoryOverhead`, `adaptive query execution`, and `repartition` — all of which appear in the Spark OOM runbook but not in the raw error message. V3's embedding is semantically close to the runbook's resolution section.

V4 (the original, preserved verbatim) is specifically valuable for BM25: the exact string `"java.lang.OutOfMemoryError"` as a keyword match gives a very high BM25 score for any runbook that explicitly documents this exception class by name, including ones that might not rank well on semantic similarity alone.

---

### Technique 3: HyDE (Hypothetical Document Embeddings)

**What it is:** Instead of embedding the query and searching for similar chunks, HyDE generates a hypothetical document that looks like the kind of answer you'd want to retrieve — then embeds THAT document and uses it as the retrieval vector. The intuition: answer-space embeddings are closer to actual answer chunks than question-space embeddings.

**Input:**
```python
error_class = "OutOfMemoryError"
pipeline = "spark_etl_daily_ingest"
stage = "shuffle_read"
```

**Template matching:**

The HyDE module maintains a library of document templates keyed by error class:

```python
HYDE_TEMPLATES = {
    "OutOfMemoryError": "spark_oom",        # ← MATCHED
    "ShuffleFetchFailure": "spark_shuffle",
    "AnalysisException": "spark_sql_error",
    "DeltaIllegalStateException": "delta_corruption",
    # ...
}
selected_template = "spark_oom"
```

**Hypothetical document generated (via GPT-4o-mini with `spark_oom` template):**

```
Root Cause Analysis: Spark Executor OutOfMemoryError During Shuffle Stage

Incident Summary: The Spark executor running on stage 12 (shuffle read) encountered a Java
heap space OutOfMemoryError. The executor was allocated 4g of heap memory
(spark.executor.memory=4g) with 512m overhead (spark.executor.memoryOverhead=512m).

Root Cause: Data skew on the join key customer_id caused executor-008 to receive
approximately 40% of all shuffle data, exceeding available heap. This is a classic
skewed sort-merge join failure pattern on the spark_etl_daily_ingest pipeline.

Diagnosis Steps:
1. Open Spark UI → Stages → Stage 12 → Task Metrics. Look for tasks with significantly
   higher "Shuffle Read Size" than the median (skew indicator: max/median > 5x).
2. Run: SELECT customer_id, COUNT(*) FROM raw_events GROUP BY customer_id ORDER BY 2 DESC LIMIT 20
   to identify skewed keys.
3. Check executor-008 GC logs for "GC overhead limit exceeded" warnings preceding the OOM.

Resolution:
Option A (immediate): Increase spark.executor.memory to 8g and spark.executor.memoryOverhead
to 2g in the cluster configuration.
Option B (preferred): Enable adaptive skew join handling:
  spark.sql.adaptive.enabled=true
  spark.sql.adaptive.skewJoin.enabled=true
  spark.sql.adaptive.skewJoin.skewedPartitionFactor=5
Option C (for severe skew): Salt the join key — append a random suffix 0-9 to customer_id
on the larger table to distribute the skewed partition across 10 executors.

Validation: After applying the fix, re-run stage 12 and confirm all task shuffle read sizes
are within 2x of the median in the Spark UI.

Prevention: Add a pre-flight skew check job that alerts if any key accounts for > 10% of
partition data before the join stage executes.
```

**Embedding this document:**

The hypothetical document is embedded using `text-embedding-3-large` (3072 dims) to produce vector `v_hyde`. This vector becomes an additional retrieval query, independent of the 4 query variants from Technique 2.

**Why this matters:**

When you embed the raw query `"java.lang.OutOfMemoryError: Java heap space"`, the 3072-dim vector captures the general semantic space of JVM memory errors. When you embed the hypothetical document above, the vector captures the specific semantic space of:
- Spark executor memory tuning
- Data skew diagnosis
- Sort-merge join failures
- `spark.executor.memoryOverhead` configuration
- Adaptive Query Execution skew handling

In tests on this knowledge base, HyDE increases the recall@5 for OOM-related runbooks by ~18% compared to embedding the raw error alone. The improvement is largest for runbooks written in natural language ("If you see heap exhaustion during shuffle...") that don't contain the exact Java class name `OutOfMemoryError`.

**Important caveat:** HyDE can generate hallucinated specifics (e.g., it guessed the skew ratio was 40% — we don't actually know this yet). The hypothetical doc is used ONLY for retrieval, never presented to the user. The retrieved real chunks correct any hallucinated assumptions in HyDE's output.

---

### Technique 4: RAG Fusion

**What it is:** RAG Fusion generates multiple semantically diverse query variants using an LLM, then executes independent retrievals for each variant, and merges the result lists using Reciprocal Rank Fusion (RRF). RRF rewards documents that rank highly across multiple independent queries, providing a more robust ranking than any single query alone.

**Input:**
```
"java.lang.OutOfMemoryError: Java heap space"
```

**LLM-generated variants (GPT-4o-mini, temperature=0.8 for diversity):**

```
Variant F1: "Spark executor OutOfMemoryError root cause data skew shuffle read large partition imbalance"
Variant F2: "fix resolve Spark OOM increase executor memory driver memoryOverhead spark configuration Databricks"
Variant F3: "similar incidents spark_etl_daily_ingest OutOfMemory historical precedent past failures ICM"
```

**The strategic design of these three variants:**

| Variant | Retrieval Focus | Target Chunks |
|---|---|---|
| F1 (Root Cause) | Why it happened | Root cause analysis sections, data skew docs |
| F2 (Resolution) | How to fix it | Step-by-step resolution runbooks, config tuning guides |
| F3 (Historical) | Has this happened before | ICM incident history, post-mortem documents |

**RRF scoring formula:**

For each document `d` across all retrieval result lists:

```
RRF_score(d) = Σ  1 / (k + rank_i(d))
               i

where k=60 (standard RRF constant), rank_i(d) = rank of document d in query i's result list
(or ∞ if not present)
```

**Example RRF calculation for the OOM Runbook chunk:**

| Query | Rank in result list | 1/(60+rank) |
|---|---|---|
| V3 (LLM rewrite) | 1 | 1/61 = 0.0164 |
| F1 (Root cause) | 1 | 1/61 = 0.0164 |
| F2 (Resolution) | 2 | 1/62 = 0.0161 |
| F3 (Historical) | 8 | 1/68 = 0.0147 |
| V1 (Synonym) | 3 | 1/63 = 0.0159 |
| **Total RRF** | | **0.0795** |

**Example RRF calculation for the similar ICM Incident chunk (ICM-2831204, same pipeline, 2 weeks ago):**

| Query | Rank in result list | 1/(60+rank) |
|---|---|---|
| F3 (Historical) | 1 | 1/61 = 0.0164 |
| V2 (Context-enriched) | 4 | 1/64 = 0.0156 |
| F1 (Root cause) | 12 | 1/72 = 0.0139 |
| V3 (LLM rewrite) | 15 | 1/75 = 0.0133 |
| **Total RRF** | | **0.0592** |

**Why this matters:**

Consider what a single-query approach misses. Query V3 (the LLM rewrite) focuses on root cause and resolution — it retrieves the OOM runbook (#1) and the executor memory tuning guide (#2) very well. But ICM-2831204 (the incident from 2 weeks ago where the same pipeline OOM'd and was resolved by salting the join key) ranks only #15 for V3, because V3's embedding doesn't closely match the ICM incident's informal language ("pipeline went OOM again, fixed by salting customer_id join").

F3 (the historical variant) brings ICM-2831204 up to rank #1, because "similar incidents historical precedent past failures ICM" is semantically close to how ICM incident reports are written. RRF ensures this chunk survives the merge with a strong composite score (0.0592), even though it wasn't top-ranked by most queries.

The practical result: the final response tells the engineer "2 weeks ago, the same pipeline OOM'd during shuffle. The on-call engineer resolved it by salting the `customer_id` join key with a random suffix 0-9. Resolution time was 23 minutes." This historical context is available ONLY because RAG Fusion included the historical-focused query variant.

---

## Phase 2: Retrieval

*With ~8 distinct query signals (V1-V4 from rewriting + 1 HyDE vector + F1-F3 from fusion), retrieval executes against the Azure AI Search index, then multiple post-retrieval passes refine the candidate set.*

---

### Technique 5: Azure AI Search Hybrid (Vector + BM25)

**What it is:** Azure AI Search executes both dense vector search (HNSW over 3072-dim embeddings) and sparse keyword search (BM25) in parallel for each query, then merges the results using a configurable weighted blend, followed by server-side semantic reranking using an L2 cross-encoder model.

**Configuration:**
```python
RAG_HYBRID_ALPHA = 0.6        # 60% vector weight, 40% BM25 weight
SEMANTIC_RERANKER = True       # Azure Cognitive Search semantic reranker enabled
TOP_K_PER_QUERY = 5            # Top 5 chunks returned per query
TOTAL_QUERIES = 8              # V1-V4 + HyDE + F1-F3
MAX_CANDIDATES = 40            # Before deduplication (~5 × 8, with some overlap)
```

**Per-query execution for V4 (original verbatim query: "java.lang.OutOfMemoryError: Java heap space"):**

*Vector component:*
- Query embedded → 3072-dim vector
- HNSW index searches ~380 chunks (post-topic-filter) for nearest neighbors
- Top vector results: executor memory tuning guide (cos=0.87), shuffle optimization guide (cos=0.83), general Spark troubleshooting (cos=0.79)
- Note: vector search does NOT top-rank the OOM runbook itself, because the runbook's title "Spark OOM Troubleshooting" has a slightly different embedding than the raw Java error message

*BM25 component:*
- Tokenized query: ["java.lang.OutOfMemoryError", "Java", "heap", "space"]
- BM25 scores docs containing exact token matches, with TF-IDF weighting
- Top BM25 results: OOM runbook chunk (contains "OutOfMemoryError" 8 times), ICM-2831204 title (contains exact error class), stack trace analysis guide
- The OOM runbook scores 0.94 on BM25 because it contains the exact exception class name densely

*Hybrid merge for V4:*
```
chunk_A (OOM Runbook):           0.6 × 0.79 (vector) + 0.4 × 0.94 (BM25) = 0.850
chunk_B (Memory Tuning Guide):   0.6 × 0.87 (vector) + 0.4 × 0.31 (BM25) = 0.646
chunk_C (Shuffle Guide):         0.6 × 0.83 (vector) + 0.4 × 0.22 (BM25) = 0.586
```

The OOM Runbook jumps from rank #3 (vector-only) to rank #1 (hybrid) because BM25 recognizes its density of exact error class matches.

**Per-query execution for V3 (LLM rewrite: "Spark executor OutOfMemoryError... data skew... spark.executor.memory... adaptive query execution"):**

*Vector component:*
- The LLM rewrite's embedding is semantically close to the OOM runbook's resolution section
- Top vector results: OOM runbook resolution section (cos=0.94), AQE skew join config guide (cos=0.89), executor memory sizing guide (cos=0.85)

*BM25 component:*
- Tokenized query includes "spark.executor.memory", "spark.executor.memoryOverhead", "adaptive", "skewJoin"
- BM25 retrieves chunks containing these exact config key names
- Top BM25: AQE configuration reference (contains all 3 config keys), executor memory best practices

*Hybrid merge for V3:* The OOM runbook and AQE config guide both score well on both components, ranking #1 and #2 respectively.

**HyDE retrieval (hypothetical document vector):**

The HyDE vector is most similar to chunks that describe:
- Diagnosis steps using Spark UI
- The `spark.sql.adaptive.skewJoin.skewedPartitionFactor` config key
- Key salting patterns for skewed joins

It retrieves 2 unique chunks not found by any other query variant:
1. "Spark UI Task Metrics Interpretation Guide" — explains how to read task-level shuffle metrics
2. "Join Key Salting Pattern for Data Skew" — a general Confluence how-to not tagged with "OutOfMemoryError"

**Semantic reranking (Azure L2 cross-encoder):**

After hybrid merge, Azure's built-in semantic reranker re-scores the top 50 results using a cross-encoder that reads the query AND the chunk together. This server-side reranking is separate from Technique 11 (client-side cross-encoder reranking) and operates at the retrieval layer.

**Total candidates collected across all 8 queries:**

```
~40 raw candidates (5 per query × 8 queries)
After deduplication (same chunk_id retrieved by multiple queries): ~25 unique chunks
```

**Why this matters:**

The critical insight is that BM25 and vector search fail in complementary ways on this query:

- **Vector alone** ranks the generic "Spark executor memory tuning guide" highly (cos=0.87) because its embedding is close to "Java heap space" — both are about memory. But the tuning guide doesn't mention `OutOfMemoryError` by name and provides general guidance, not OOM-specific steps. It would be ranked #1 by vector, displacing the specific OOM runbook.

- **BM25 alone** ranks the OOM runbook #1 (it contains the exact exception class name) but ranks the "Join Key Salting Pattern" very low because that doc doesn't use "OutOfMemoryError" at all — it's written conceptually. A purely BM25 system would miss the most effective fix.

- **Hybrid** gets both: the OOM runbook (from BM25) AND the salting pattern (from vector), giving the engineer both the standard fix and the advanced fix in the same response.

The 3072-dim embedding space from `text-embedding-3-large` is critical here: smaller embedding models (e.g., `text-embedding-ada-002` at 1536 dims) in benchmarks show ~12% lower recall for technical content where subtle differences between "executor OOM" and "driver OOM" matter enormously — the fixes are completely different.

---

### Technique 6: Agentic RAG

**What it is:** Instead of issuing all queries against a single monolithic index, Agentic RAG classifies the intent of the query and routes different query variants to the most appropriate source index, then assembles results from multiple sources into a unified candidate set.

**Intent classification (GPT-4o-mini):**
```python
intent = {
    "primary": "error_diagnosis_and_resolution",
    "secondary": ["historical_incident_lookup", "dependency_impact_analysis"],
    "not_applicable": ["scheduled_query_help", "schema_lookup", "cost_optimization"]
}
```

**Source routing plan:**

```
Query V3 (LLM rewrite) → Confluence Index (runbooks, how-tos, architecture docs)
   Reason: "error diagnosis" intent → primary source is procedural documentation

Query F3 (historical) → ICM Index (incident history, post-mortems, resolution notes)
   Reason: "historical incident lookup" → ICM is the source of truth for past incidents

Query V2 (context-enriched) → Log Analytics Index (Spark logs, cluster metrics)
   Reason: pipeline:spark_etl_daily_ingest tag → pipeline-specific log patterns

HyDE vector → Confluence Index (most hypothetical docs resemble runbooks)
   Reason: HyDE generates runbook-style content → best matched against runbook corpus

Query V1, V4, F1, F2 → All Indices (broad search, no source preference)
   Reason: synonym/keyword queries without strong source signal
```

**Source index characteristics:**

| Index | Size | Content type | Avg chunk size |
|---|---|---|---|
| Confluence | ~8,000 chunks | Runbooks, how-tos, architecture | 800 tokens |
| ICM | ~3,000 chunks | Incident reports, post-mortems | 600 tokens |
| Log Analytics | ~5,000 chunks | Log patterns, KQL queries, alert definitions | 400 tokens |
| Graph Context | Runtime-generated | Pipeline dependency metadata | 300 tokens |

**Results from Confluence (via V3):**
- OOM Troubleshooting Runbook (2 chunks: diagnosis section + resolution section) ← top hit
- Executor Memory Sizing Guide (1 chunk)
- AQE Configuration Reference (1 chunk)

**Results from ICM (via F3):**
- ICM-2831204: "spark_etl_daily_ingest OOM, resolved by salting customer_id" (1 chunk) ← critical find
- ICM-2799103: "spark_etl_daily_ingest shuffle timeout, unrelated but same pipeline" (1 chunk)

**Results from Log Analytics (via V2):**
- Spark executor heap alert definition (matching alert threshold config)
- Stage 12 historical failure pattern query (KQL that identifies recurring stage failures)

**Why this matters:**

Without source routing, all 8 queries hit the merged index, which means ICM incident chunks are competing on equal footing with Confluence runbooks for the same limited result slots (top-5 per query). ICM incident reports are written in informal language ("pipeline went OOM again, this time worse") and tend to have lower embedding similarity scores than well-structured runbooks.

With Agentic RAG, the historical query F3 is sent specifically to the ICM index, where it ranks #1 with no competition from runbooks. This guarantees ICM-2831204 (the exact same pipeline, 2 weeks ago) is retrieved — it would have been rank #6-8 in a merged index search and potentially dropped below the top-5 cutoff.

Additionally, routing V2 (the context-enriched query with `pipeline:spark_etl_daily_ingest`) to Log Analytics retrieves the KQL query template for diagnosing executor heap pressure — a tool chunk that enables the LLM to give the engineer a copy-paste diagnostic query rather than generic advice.

---

### Technique 7: Parent-Child Chunking (Retrieval Expansion)

**What it is:** Documents are indexed at two granularities simultaneously: small "child" chunks (~200-500 tokens, optimized for precise retrieval) and large "parent" chunks (~800-1500 tokens, containing full context). When a child chunk is retrieved, it is automatically expanded to its parent chunk before being passed to the LLM.

**The retrieved child chunk:**

```
[CHILD CHUNK — chunk_id: conf_oom_resolution_step3, tokens: 420]
Source: Confluence / Spark OOM Troubleshooting Guide / Resolution Section

"Option A (Immediate Fix): Increase executor memory allocation.
Set the following in your Databricks cluster configuration or spark-submit:
  spark.executor.memory=8g
  spark.executor.memoryOverhead=2g
  spark.memory.fraction=0.8
  spark.memory.storageFraction=0.3
After applying, restart the cluster and re-run the failed stage.
Expected outcome: Executor heap usage drops below 75% during shuffle read."
```

This child chunk was retrieved because it directly contains the fix with specific config values. It is 420 tokens — well within typical retrieval chunk sizes.

**Expansion to parent chunk:**

```python
parent_chunk_id = get_parent(child_chunk_id="conf_oom_resolution_step3")
# Returns: conf_oom_resolution_parent, tokens: 1180
```

```
[PARENT CHUNK — chunk_id: conf_oom_resolution_parent, tokens: 1180]
Source: Confluence / Spark OOM Troubleshooting Guide / Resolution Section

"## Resolution

### Which OOM type is this fix for?
IMPORTANT: This guide covers EXECUTOR OOM during shuffle stages. If the OOM is in the
DRIVER (stage 0, or in the application master), see the "Driver OOM" runbook instead.
Symptoms of executor shuffle-stage OOM:
- Stage fails at a ShuffleMapStage or ResultStage during reduce/shuffle-read
- Error appears in executor logs, not driver logs
- Executor-specific task count is asymmetric (skew indicator)

### Prerequisite Check
Before applying any fix, verify this is a skew-induced OOM and not a genuine memory
insufficiency:
1. Open Spark UI → Stages → Stage 12 → Tasks
2. Sort by "Shuffle Read Size" descending
3. If the top 1 task has > 5x the median shuffle read size: this is DATA SKEW → apply Option B
4. If all tasks are roughly equal but still failing: this is GENUINE MEMORY PRESSURE → apply Option A

### Option A (Immediate Fix — use if all tasks are uniformly failing):
Increase executor memory allocation.
Set the following in your Databricks cluster configuration or spark-submit:
  spark.executor.memory=8g
  spark.executor.memoryOverhead=2g
  spark.memory.fraction=0.8
  spark.memory.storageFraction=0.3
After applying, restart the cluster and re-run the failed stage.
Expected outcome: Executor heap usage drops below 75% during shuffle read.

### Option B (Preferred Fix — use if data skew is confirmed):
Enable Adaptive Query Execution (AQE) skew join handling:
  spark.sql.adaptive.enabled=true
  spark.sql.adaptive.skewJoin.enabled=true
  spark.sql.adaptive.skewJoin.skewedPartitionFactor=5
  spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes=256m
No cluster restart required — change the session configuration and re-run.

### Option C (Advanced — for severe/persistent skew):
[... continues in next parent chunk ...]"
```

**Why this matters:**

Without parent-child expansion, the LLM receives only the child chunk (Option A, the immediate fix). The engineer applies `spark.executor.memory=8g` — but this is actually the WRONG fix for their situation. The ICM alert indicates executor-008 received disproportionate shuffle data, which means this is data skew (Option B territory), not genuine memory pressure (Option A territory).

The parent chunk contains the critical disambiguation: "If the top 1 task has > 5x the median shuffle read size: this is DATA SKEW → apply Option B." This context transforms the advice from potentially incorrect (blindly increasing memory) to correct (diagnose skew first, then choose the right option).

The parent chunk also contains the "IMPORTANT" warning that this fix is for EXECUTOR OOM during shuffle, not DRIVER OOM. An engineer who applies executor memory configs to a driver OOM will spend 45 minutes confused about why the fix didn't work.

**Chunk size policy used in this deployment:**

```python
CHILD_CHUNK_SIZE = 300       # ~300 tokens, optimized for retrieval precision
PARENT_CHUNK_SIZE = 1200     # ~1200 tokens, optimized for LLM context completeness
CHILD_OVERLAP = 50           # 50-token overlap between child chunks within same parent
```

---

### Technique 8: Semantic Chunking (At Indexing Time)

**What it is:** Rather than splitting documents at fixed token boundaries (e.g., every 512 tokens), semantic chunking splits on topic boundaries detected by monitoring the cosine similarity of consecutive sentence embeddings. A sharp drop in similarity between adjacent sentences signals a topic change — a natural chunk boundary.

**The source document:** Confluence page "Spark OOM Troubleshooting Guide" (4,200 tokens total)

**Section structure of the original document:**

```
1. Overview and Applicability (200 tokens)
2. Root Cause Analysis (850 tokens)
   2.1 Data Skew OOM
   2.2 Genuine Memory Pressure OOM
   2.3 Broadcast Variable OOM
3. Diagnosis Steps (620 tokens)
   3.1 Reading Spark UI Stage Metrics
   3.2 Identifying Skew in Task Distribution
4. Resolution (1,100 tokens)
   4.1 Option A: Increase Memory
   4.2 Option B: AQE Skew Handling
   4.3 Option C: Join Key Salting
5. Validation and Monitoring (430 tokens)
6. Prevention Checklist (250 tokens)
   6.1 Pre-flight Skew Detection
   6.2 Alert Thresholds
```

**Fixed-token chunking result (what a naive implementation would produce):**

```
Chunk 1 (512 tokens): Overview + Root Cause Analysis sections 2.1, 2.2, first half of 2.3
Chunk 2 (512 tokens): Second half of 2.3 + all of Diagnosis Steps (3.1, 3.2) + first 100 tokens of Resolution
Chunk 3 (512 tokens): Resolution Options A, B, and first half of Option C ← CRITICAL: Option C is split
Chunk 4 (512 tokens): Second half of Option C + Validation + first 150 tokens of Prevention
Chunk 5 (remaining): Rest of Prevention Checklist
```

Chunk 3 is split mid-Option C. If an engineer's fix requires Option C (join key salting for severe skew), they receive step 1-3 of 5 without steps 4-5. The incomplete procedure appears complete in the response.

**Semantic chunking result (deployed in Alert Whisperer):**

```python
# Embedding similarity scores between consecutive sentence groups:
similarity[section1→section2] = 0.71  # Overview → Root Cause: moderate (same doc, related)
similarity[section2→section3] = 0.58  # Root Cause → Diagnosis: drop detected → CHUNK BOUNDARY
similarity[section3→section4] = 0.54  # Diagnosis → Resolution: drop detected → CHUNK BOUNDARY
similarity[section4→section5] = 0.63  # Resolution → Validation: moderate (related outcomes)
similarity[section5→section6] = 0.49  # Validation → Prevention: drop detected → CHUNK BOUNDARY
```

```
Semantic Chunk 1 (1,050 tokens): Overview + full Root Cause Analysis
Semantic Chunk 2 (620 tokens):   Full Diagnosis Steps
Semantic Chunk 3 (1,100 tokens): Full Resolution (all three options complete) ← INTACT
Semantic Chunk 4 (680 tokens):   Validation + Prevention Checklist
```

The Resolution section is preserved as a complete, intact chunk (1,100 tokens). All three resolution options are included in their entirety.

**Similarity threshold configuration:**
```python
SEMANTIC_CHUNK_SIMILARITY_THRESHOLD = 0.60  # Drop below this → new chunk boundary
MAX_CHUNK_TOKENS = 1500                      # Hard ceiling even if no topic boundary detected
MIN_CHUNK_TOKENS = 150                       # Minimum size to avoid micro-chunks
```

**Why this matters:**

The difference is not subtle. Here is what the LLM receives for the Resolution section under each approach:

*Fixed-token chunking:*
```
"...Option C (Advanced — for severe/persistent skew):
For highly skewed joins where AQE cannot handle the imbalance, use key salting:
1. Add a salt column to the larger table: df.withColumn('salt', (rand() * 10).cast('int'))
2. Modify the join key: concat(customer_id, '_', salt)
3. Expand the smaller table 10x with a salt range..."
[TRUNCATED — steps 4-5 missing]
```

*Semantic chunking:*
```
"...Option C (Advanced — for severe/persistent skew):
[steps 1-5 complete]
4. After the join, drop the salt column: df.drop('salt')
5. Validate: the result row count should match the expected count from the non-salted join.
   Run: assert df_salted.count() == df_original.count(), 'Row count mismatch after salting'"
```

Steps 4-5 are not optional — step 4 removes the temporary column (without it, the output schema is wrong), and step 5 validates correctness. A truncated procedure introduces a subtle data quality bug that may only surface downstream when a schema mismatch breaks the Delta merge.

---

## Phase 3: Post-Retrieval

*With ~25 unique candidate chunks assembled, post-retrieval techniques aggressively filter, augment, and re-rank before the context is handed to the generator.*

---

### Technique 9: CRAG (Corrective RAG)

**What it is:** Each candidate chunk is individually graded for relevance to the query using a lightweight classifier. Chunks are sorted into three categories: CORRECT (keep), AMBIGUOUS (keep with lower weight), and INCORRECT (discard). This prevents context pollution from marginally-related or irrelevant chunks that survived earlier filtering.

**Grader configuration:**
```python
CRAG_CORRECT_THRESHOLD = 0.5      # Score ≥ 0.5 → CORRECT
CRAG_AMBIGUOUS_THRESHOLD = 0.3    # 0.3 ≤ score < 0.5 → AMBIGUOUS
# score < 0.3 → INCORRECT → discarded
GRADER_MODEL = "gpt-4o-mini"      # Fast, cheap grader
MAX_CONTEXT_CHUNKS = 8            # Keep at most 8 chunks for generation
```

**Grading all 25 candidate chunks:**

*CORRECT (score ≥ 0.5) — 6 chunks:*

| Chunk | Score | Reason |
|---|---|---|
| OOM Runbook — Resolution (parent, 1,100 tokens) | 0.94 | Exact match: Spark executor OOM, shuffle stage, resolution steps |
| OOM Runbook — Diagnosis (parent, 620 tokens) | 0.91 | Exact match: OOM diagnosis for executor shuffle stage |
| ICM-2831204 (same pipeline OOM, 2 weeks ago) | 0.88 | Same pipeline, same error class, includes successful resolution |
| AQE Skew Join Configuration Reference | 0.82 | Directly relevant: config keys for skew handling cited in resolution |
| Executor Memory Sizing Guide | 0.76 | Relevant: provides memory calculation formulas for Option A |
| Join Key Salting Pattern | 0.71 | Relevant: Option C technique described in detail |

*AMBIGUOUS (0.3 ≤ score < 0.5) — 8 chunks:*

| Chunk | Score | Reason |
|---|---|---|
| General Spark Performance Tuning Guide | 0.48 | Tangentially relevant — contains memory advice but not OOM-specific |
| Databricks Cluster Sizing Best Practices | 0.44 | Useful context but not OOM resolution |
| Spark UI Navigation Guide | 0.41 | Useful for diagnosis step but not directly about OOM |
| Shuffle Optimization — Reduce Partitions | 0.38 | Related to shuffle but not to OOM |
| Synapse Analytics Timeout Troubleshooting | 0.35 | Different system, OOM terminology matches but irrelevant |
| ICM-2799103 (same pipeline, shuffle timeout) | 0.34 | Same pipeline but different error type |
| Spark SQL AQE Overview | 0.32 | Background context on AQE, not OOM-specific |
| Delta Table Merge Performance | 0.31 | Shares vocabulary but different problem entirely |

*INCORRECT (score < 0.3) — 11 chunks discarded:*

| Chunk | Score | Why it survived retrieval but is irrelevant |
|---|---|---|
| Kusto Ingestion Worker OOM | 0.22 | "OutOfMemoryError" exact match in BM25, but Kusto context |
| ADLS Gen2 Permission Errors — SPN Auth | 0.19 | "pipeline:spark_etl_daily_ingest" metadata tag matched |
| Kusto Cache Size Configuration | 0.17 | "memory" keyword, but Kusto not Spark |
| Event Hub Ingestion Backpressure | 0.15 | Shares "ingest" keyword, completely different system |
| Java SDK OOM Handling (ADLS client) | 0.14 | Java OOM exact match but ADLS SDK context |
| Synapse SQL Pool Memory Grant Timeout | 0.13 | "memory" + "timeout" overlap, wrong system |
| KQL OutOfMemoryException Handling | 0.11 | "OutOfMemoryException" Kusto version |
| Log Analytics Workspace Quotas | 0.09 | Retrieved via Log Analytics source, irrelevant |
| ADLS Gen2 Throughput Limits | 0.08 | Partially relevant but retrieved prematurely |
| Databricks Unity Catalog Access Control | 0.07 | Retrieved via pipeline metadata tag, irrelevant |
| Spark Structured Streaming Watermarks | 0.06 | "Spark" keyword match, wrong Spark use case |

**Post-CRAG candidate set: 14 chunks (6 CORRECT + 8 AMBIGUOUS)**

With a MAX_CONTEXT_CHUNKS limit of 8, only the 6 CORRECT chunks + 2 highest-scoring AMBIGUOUS chunks proceed. The 8 AMBIGUOUS chunks are ranked by score; only the General Spark Performance Tuning Guide (0.48) and Databricks Cluster Sizing Guide (0.44) survive.

**Final post-CRAG candidate set: 8 chunks**

**Why this matters:**

Without CRAG, all 25 chunks would pass to the context window. The 11 INCORRECT chunks include the Kusto Ingestion Worker OOM (score 0.22) which contains realistic-sounding but wrong advice like "increase the Kusto ingestion batch size to reduce per-batch memory pressure" — advice that is completely inapplicable to Spark executors.

The LLM, when given noisy context, tends to average across it. Empirically, with 11 irrelevant chunks diluting the context, the final response shifts toward generic advice ("check your memory settings," "review ingestion configuration") rather than the specific, actionable resolution that the 6 CORRECT chunks would support. CRAG is the single most important filtering layer for preventing the LLM from generating confident-sounding but wrong advice.

---

### Technique 10: GraphRAG

**What it is:** A knowledge graph of pipeline dependencies, cluster resources, and data flow relationships is maintained alongside the text index. For each incident, the affected pipeline is looked up in the graph, and its neighbors (upstream, downstream, shared resources) are retrieved as additional context. This provides blast radius awareness that no text document can provide.

**Graph lookup:**

```python
pipeline_node = graph.get_node("spark_etl_daily_ingest")
```

**Pipeline node attributes:**
```json
{
  "node_id": "spark_etl_daily_ingest",
  "type": "spark_pipeline",
  "owner": "data-platform-team@contoso.com",
  "schedule": "03:00 AM CDT daily",
  "sla_minutes": 90,
  "current_status": "FAILED",
  "common_errors": ["OutOfMemoryError", "ShuffleFetchFailure", "DeltaConcurrentModification"],
  "last_success": "2026-03-07T08:22:00Z",
  "cluster_config": {
    "executor_memory": "4g",
    "executor_memory_overhead": "512m",
    "shuffle_partitions": 200
  }
}
```

**Graph traversal (depth=2):**

```
spark_etl_daily_ingest
├── READS_FROM → ADLS Gen2 Raw Zone / container: raw-events
│   └── WRITTEN_BY → event_hub_ingestor (status: HEALTHY)
├── WRITES_TO → ADLS Gen2 Curated Zone / delta-table: curated_events
│   ├── READ_BY → synapse_pipeline_customer360 (status: BLOCKED — waiting for curated_events)
│   │   └── FEEDS → customer360_dashboard (PowerBI) (status: STALE — last refresh: 2026-03-07)
│   └── READ_BY → kusto_ingestion_telemetry (status: BLOCKED — waiting for curated_events)
│       └── FEEDS → telemetry_dashboard (Grafana) (status: STALE)
└── DEPENDS_ON → dim_customers_delta (ADLS Gen2) (status: HEALTHY)
```

**Failure chain constructed:**
```
ADLS Gen2 Raw Zone (READS_FROM) 
  → spark_etl_daily_ingest [CURRENT FAILURE: OutOfMemoryError at 03:45 AM]
    → curated_events Delta Table [NOT WRITTEN — blocking downstream]
      → synapse_pipeline_customer360 [BLOCKED since 04:15 AM — SLA breach at 05:30 AM]
        → customer360_dashboard [STALE — business teams see yesterday's data]
      → kusto_ingestion_telemetry [BLOCKED since 04:20 AM — SLA breach at 05:00 AM]
        → telemetry_dashboard [STALE — ops team has degraded monitoring]
```

**SLA analysis:**
- `kusto_ingestion_telemetry` SLA: 05:00 AM CDT → **breaches in 1h 15min** from detection time
- `synapse_pipeline_customer360` SLA: 05:30 AM CDT → **breaches in 1h 45min** from detection time

**Graph context chunk injected:**

```
[GRAPH CONTEXT — similarity: 0.85, source: dependency_graph]
Pipeline Failure Blast Radius Analysis for spark_etl_daily_ingest

Direct Impact:
- curated_events Delta table is NOT being written (spark_etl_daily_ingest has been failing since 03:45 AM)

Downstream Blocked Pipelines (2):
1. synapse_pipeline_customer360
   - Status: BLOCKED
   - Waiting on: curated_events Delta table
   - SLA deadline: 05:30 AM CDT (1h 45m remaining)
   - Business impact: customer360 PowerBI dashboard showing stale data

2. kusto_ingestion_telemetry
   - Status: BLOCKED
   - Waiting on: curated_events Delta table
   - SLA deadline: 05:00 AM CDT (1h 15m remaining) ← CRITICAL, breaches first
   - Business impact: Grafana telemetry dashboard degraded

Action Required:
- Notify synapse_pipeline_customer360 owner immediately
- Notify kusto_ingestion_telemetry owner immediately (SLA breach imminent)
- Both pipelines will auto-retry once curated_events is available

Common Errors for spark_etl_daily_ingest: OutOfMemoryError, ShuffleFetchFailure
Last successful run: 2026-03-07T08:22:00Z (yesterday)
```

This chunk is scored at similarity 0.85 and added to the candidate pool alongside the text-retrieved chunks. It passes CRAG with score 0.90 (it is directly about the failed pipeline and provides actionable context).

**Why this matters:**

Zero text documents in the knowledge base contain the information "kusto_ingestion_telemetry is blocked and will breach SLA in 75 minutes." This information exists only in the live dependency graph. Without GraphRAG:

- The engineer fixes the OOM and closes the incident
- At 05:00 AM, the kusto_ingestion_telemetry team pages the on-call with a separate incident
- At 05:30 AM, the Synapse team pages separately
- Three separate incidents get created; all three on-call engineers are awake

With GraphRAG, the Alert Whisperer response includes: "Two downstream pipelines are blocked. kusto_ingestion_telemetry breaches SLA at 05:00 AM CDT (75 minutes). Notify those teams immediately while working on the fix." One engineer handles the incident with full situational awareness, proactively communicating to downstream teams before their pipelines breach SLA.

The graph also surfaces that this pipeline last succeeded yesterday at 08:22 AM — which tells the engineer that yesterday's ingest completed successfully, and the failure is today's run. This eliminates the need to check run history manually.

---

### Technique 11: Cross-Encoder Reranking

**What it is:** After CRAG and GraphRAG produce a refined candidate set, a cross-encoder model re-scores every (query, chunk) pair jointly. Unlike bi-encoders (which encode query and chunk independently), the cross-encoder reads query and chunk together as a single input, enabling fine-grained relevance judgment based on exact word overlap, specificity match, and contextual alignment.

**Input to cross-encoder:**

9 chunks total: 8 post-CRAG chunks + 1 graph context chunk

**Cross-encoder model:**

In production, `ms-marco-MiniLM-L-12-v2` is used (12-layer MiniLM fine-tuned on MS MARCO passage ranking). For this deployment, GPT-4o-mini is used as a fallback when the sentence-transformers endpoint is unavailable.

**Query used for cross-encoder scoring:**

The LLM rewrite (V3) is used as the cross-encoder query, as it is the most semantically rich:

```
"Spark executor OutOfMemoryError during shuffle read stage daily ingest pipeline Databricks
data skew spark.executor.memory memoryOverhead repartition adaptive query execution"
```

**Cross-encoder scoring results:**

| Chunk | Cross-Encoder Score | Bi-Encoder Rank | Cross-Encoder Rank |
|---|---|---|---|
| OOM Runbook — Resolution (parent) | **0.94** | 1 | 1 |
| ICM-2831204 (same pipeline, 2 weeks ago) | **0.88** | 5 | 2 ← promoted |
| Graph Context (blast radius) | **0.85** | 7 | 3 ← promoted |
| OOM Runbook — Diagnosis (parent) | **0.83** | 2 | 4 ← demoted |
| AQE Skew Join Config Reference | **0.82** | 3 | 5 |
| Executor Memory Sizing Guide | **0.71** | 4 | 6 ← demoted |
| Join Key Salting Pattern | **0.68** | 6 | 7 |
| General Spark Performance Tuning | **0.52** | 8 | 8 |
| Databricks Cluster Sizing Guide | **0.41** | 9 | 9 |

**Notable reranking movements:**

*ICM-2831204 promoted from rank 5 → 2:*

The bi-encoder ranked this chunk 5th because its embedding ("spark_etl_daily_ingest OOM again, fixed by salting customer_id join") is not semantically close to the query embedding (which contains "executor memory overhead"). The cross-encoder reads both together and recognizes: this is the SAME pipeline (`spark_etl_daily_ingest` appears in both), SAME error (`OutOfMemoryError`), and it contains a resolution that WORKED — making it more valuable than the generic memory sizing guide at rank 4.

*Graph Context promoted from rank 7 → 3:*

The graph context chunk has low semantic similarity to the query (it's about SLA deadlines and pipeline dependencies, not Spark config). The bi-encoder ranks it 7th. The cross-encoder, reading the full chunk, recognizes it contains `spark_etl_daily_ingest` (the exact pipeline in the query context), `OutOfMemoryError` (the exact error), and actionable downstream impact data — making it rank 3rd in relevance.

*Executor Memory Sizing Guide demoted from rank 4 → 6:*

The bi-encoder ranked this 4th because its embedding is very close to "executor memory overhead" terms in the query. The cross-encoder reads it and finds it contains general formulas for memory sizing (e.g., "total executor memory = task memory + shuffle memory + storage memory") but lacks specific guidance for OOM resolution during shuffle. Good content, but less actionable than the ICM incident or graph context for this specific scenario.

**Final ranked context (passed to generator):**

```
1. OOM Runbook — Resolution (0.94)
2. ICM-2831204 — Same Pipeline Incident (0.88)
3. Graph Context — Blast Radius (0.85)
4. OOM Runbook — Diagnosis (0.83)
5. AQE Config Reference (0.82)
6. Executor Memory Sizing Guide (0.71)
7. Join Key Salting Pattern (0.68)
8. General Spark Tuning (0.52)
```

**Why this matters:**

The single most valuable chunk for this specific incident is ICM-2831204: it describes the same pipeline (not a different one), the same error, and records that the resolution that worked was key salting — not memory increase. An engineer who reads it first saves 30-45 minutes of experimentation because they skip straight to the fix that was already validated on this exact pipeline.

The bi-encoder, working from pre-computed embeddings, cannot make this pipeline-specificity judgment at scoring time. The cross-encoder makes it effortlessly by reading both together. Without cross-encoder reranking, the ICM incident sits at rank 5, and the response leads with the generic memory sizing formula (rank 4) rather than the historical precedent.

---

## Phase 4: During Generation

*Two techniques fire during the LLM's generation pass, dynamically expanding context when uncertainty is detected and self-validating the output segment by segment.*

---

### Technique 12: FLARE (Forward-Looking Active Retrieval)

**What it is:** During generation, the LLM's output is streamed token by token. Hedging language ("may need to," "might," "possibly," "unclear whether") triggers an interrupt: the partially-generated sentence is used as a new retrieval query, fresh chunks are fetched and injected into context, and the LLM continues generation with the expanded information. This allows the LLM to actively pull information it doesn't have rather than generating speculative filler.

**FLARE trigger configuration:**
```python
FLARE_HEDGE_PATTERNS = [
    "may need to", "might need to", "possibly", "you may want to",
    "unclear whether", "it depends on", "I'm not sure", "check if",
    "could be related to", "worth investigating"
]
FLARE_CONFIDENCE_WINDOW = 15   # tokens: evaluate each 15-token window for hedging
FLARE_RETRIEVAL_TOP_K = 3      # retrieve 3 new chunks when triggered
```

**Generation trace:**

The LLM begins generating a response using the 8-chunk context from cross-encoder reranking:

---

*Segment 1 (tokens 1-47):*
```
"The OOM occurred during shuffle read at stage 12. This is typically caused by data skew —
one executor receiving a disproportionate amount of shuffle data."
```

Confidence check: No hedging language detected. Tokens "typically caused by" are mild but within acceptable confidence range. ✓ **No FLARE trigger.**

---

*Segment 2 (tokens 48-95):*
```
"Check the Spark UI: go to Stages → Stage 12 → Tasks, sort by Shuffle Read Size.
If the top task shows > 5x the median, you have data skew. Apply AQE skew join settings:"
```

Confidence check: No hedging language. Concrete instructions with specific steps. ✓ **No FLARE trigger.**

---

*Segment 3 (tokens 96-147):*
```
"  spark.sql.adaptive.enabled=true
   spark.sql.adaptive.skewJoin.enabled=true
   spark.sql.adaptive.skewJoin.skewedPartitionFactor=5"
```

Confidence check: No hedging. Config keys are directly sourced from AQE reference chunk. ✓ **No FLARE trigger.**

---

*Segment 4 (tokens 148-201):*
```
"This should resolve the OOM in most cases. If the issue persists after enabling AQE,
you may need to check whether the ADLS Gen2 curated zone has sufficient I/O throughput"
```

Confidence check: **"you may need to check whether"** — HEDGING DETECTED! ⚡ **FLARE TRIGGERED.**

Trigger action:
```python
flare_query = "ADLS Gen2 curated zone I/O throughput limits performance Spark writes"
new_chunks = azure_search.query(flare_query, top_k=3)
```

New chunks retrieved by FLARE:
```
Chunk F1: "ADLS Gen2 Throughput Limits (per storage account)"
  Score: 0.83
  Content: "Maximum throughput per ADLS Gen2 storage account: 5 Gbps ingress, 10 Gbps egress.
  For write-heavy workloads (e.g., Spark Delta writes), throughput is limited by the
  storage account's network bandwidth cap. Symptoms of ADLS throttling in Spark: stage
  retries on write tasks, HDInsightThrottlingException in Spark logs, or write speeds
  consistently below 500 MB/s.
  Mitigation: distribute writes across multiple storage accounts (zone sharding), or
  request a throughput limit increase via Azure Support."

Chunk F2: "Databricks Delta Write Performance Guide"
  Score: 0.71
  Content: "For Delta tables receiving > 100 GB/hr of writes, consider enabling auto-optimize:
  delta.autoOptimize.optimizeWrite=true. This reduces file count and improves write performance."

Chunk F3: "Spark ADLS Integration Troubleshooting"
  Score: 0.62
  Content: "If Spark shuffle writes to ADLS are slow (< 200 MB/s), check: 1) storage account
  throttling, 2) network bandwidth between Databricks cluster and ADLS endpoint, 3) block
  size configuration (spark.hadoop.fs.azure.block.size defaults to 256 MB)."
```

These 3 chunks are injected into the context window. Generation resumes:

---

*Segment 4 (continued, with new context):*
```
"you may need to check whether the ADLS Gen2 curated zone has sufficient I/O throughput —
the maximum write throughput per ADLS Gen2 storage account is 5 Gbps ingress. For a 500 GB
daily ingest, this is unlikely to be the bottleneck (500 GB / 5 hrs = ~28 MB/s, well below
the 5 Gbps limit), but if you see HDInsightThrottlingException in Spark logs or write speeds
below 500 MB/s, request a throughput increase via Azure Support."
```

**Before FLARE:** "...you may need to check whether the ADLS Gen2 curated zone has sufficient I/O throughput." (vague)

**After FLARE:** Specific throughput limit (5 Gbps), concrete diagnostic indicators (`HDInsightThrottlingException`), a quantified assessment that it's unlikely to be the bottleneck for this specific workload (500 GB / 5 hrs = 28 MB/s), and the escalation path (Azure Support). Actionable, not speculative.

**Second FLARE trigger:**

*Segment 8 (tokens 312-355):*
```
"If you need to run a data skew check before the next run, you can use a pre-flight KQL query
to — actually, let me check the correct KQL syntax for querying Spark job metrics..."
```

Confidence check: **"actually, let me check"** — SELF-UNCERTAINTY DETECTED. ⚡ **FLARE TRIGGERED.**

```python
flare_query = "KQL query Spark job stage metrics executor memory Databricks Log Analytics"
new_chunks = azure_search.query(flare_query, top_k=2)
```

New chunk retrieved: KQL template for Spark executor heap monitoring from Log Analytics index. The LLM generates the correct query directly from the retrieved chunk rather than attempting to recall KQL syntax from parametric memory.

**Why this matters:**

Without FLARE, the response would contain two types of failure:
1. **Vague hedging:** "Check ADLS throughput" — the engineer doesn't know what number to check against, whether it's likely the problem, or what to do if it is
2. **Hallucinated KQL:** The LLM attempting to write KQL from memory would likely produce a syntactically plausible but wrong query (wrong table names, wrong column names), wasting engineer time debugging a diagnostic query

FLARE converts both failures into specific, validated information. The throughput numbers come from an actual ADLS documentation chunk; the KQL query comes from the Log Analytics template library. The engineer can act on both immediately.

---

### Technique 13: Self-RAG

**What it is:** After the complete response is generated, Self-RAG evaluates it segment by segment, classifying each factual claim as: `[Supported]` (present in retrieved docs), `[Partially Supported]` (inferred from context but not explicitly stated), or `[Not Supported]` (not in any retrieved doc). A confidence score is computed. Claims scoring below a threshold are flagged or regenerated.

**The generated response (post-FLARE):**

The LLM has produced a ~600-token response. Self-RAG evaluates it in logical segments.

**Segment-by-segment evaluation:**

---

*Claim 1: "Increase spark.executor.memory to 8g and spark.executor.memoryOverhead to 2g"*

Source check: Searching retrieved chunks for this exact recommendation...
- Found in: OOM Runbook — Resolution, chunk_id `conf_oom_resolution_parent`
  - Exact text: `"spark.executor.memory=8g, spark.executor.memoryOverhead=2g"`

**[Supported: YES]** — Confidence: 0.97

---

*Claim 2: "Set spark.sql.adaptive.skewJoin.enabled=true"*

Source check: Searching retrieved chunks...
- Found in: AQE Skew Join Configuration Reference, chunk_id `conf_aqe_skew_config`
  - Exact text: `"spark.sql.adaptive.skewJoin.enabled=true"`
- Also found in: OOM Runbook — Resolution (Option B section)

**[Supported: YES]** — Confidence: 0.99

---

*Claim 3: "This pipeline processes approximately 500 GB of daily event data"*

Source check: Searching retrieved chunks for "500 GB"...
- Not found in any retrieved chunk
- However, this was provided in the original alert context (ICM payload: "500GB of raw events")
- It is factually correct but sourced from the input, not the KB

**[Supported: PARTIAL]** — Confidence: 0.72 (input-sourced, not KB-sourced)
Action: Flag for transparency ("per the incident context"), but no warning generated (confidence above threshold)

---

*Claim 4: "ICM-2831204 from 2 weeks ago was resolved by salting the customer_id join key"*

Source check: Searching retrieved chunks...
- Found in: ICM-2831204 chunk, exact text: "resolved by salting the customer_id join key with random suffix 0-9"

**[Supported: YES]** — Confidence: 0.95

---

*Claim 5: "Contact the Synapse and Kusto teams about the downstream impact"*

Source check: Searching retrieved chunks...
- Found in: Graph Context chunk — lists `synapse_pipeline_customer360` and `kusto_ingestion_telemetry` as blocked
- However, "contact the teams" is a DERIVED recommendation — no retrieved doc explicitly says "send a message to the Synapse team"
- The graph context provides the downstream impact facts; the "contact" recommendation is the LLM's inference

**[Supported: NO]** — **[Relevant: YES]** — Confidence: 0.61 (logically correct inference, but not documented procedure)
Action: Flag as inference. Append to response: `*(Note: downstream notification is recommended based on dependency graph analysis, not a documented runbook procedure)*`

---

*Claim 6: "spark.sql.adaptive.skewJoin.skewedPartitionFactor=5"*

Source check: Searching retrieved chunks...
- Found in: AQE Config Reference, exact text: `"spark.sql.adaptive.skewJoin.skewedPartitionFactor=5"`

**[Supported: YES]** — Confidence: 0.98

---

*Claim 7 (hypothetical — not present in this scenario's output, but illustrating Self-RAG's primary value):*

If the LLM had generated: *"Set spark.sql.adaptive.skewThreshold=0.5 to enable automatic skew detection"*

Source check: Searching retrieved chunks for "spark.sql.adaptive.skewThreshold"...
- Not found in any retrieved chunk
- This config key does NOT exist in Spark — it is a hallucinated parameter
  (The real config is `spark.sql.adaptive.skewJoin.skewedPartitionFactor`, which is dimensionless, not a 0-1 threshold)

**[Supported: NO]** — **[Relevant: YES]** — Confidence: 0.18
Action: **REGENERATION TRIGGERED** — this segment is flagged and rewritten, replacing the hallucinated config key with the correct one from retrieved chunks.

---

**Overall Self-RAG assessment:**

```python
segment_scores = [0.97, 0.99, 0.72, 0.95, 0.61, 0.98]
overall_confidence = mean(segment_scores) = 0.87
WARNING_THRESHOLD = 0.6
REGENERATION_THRESHOLD = 0.4

# Claim 5 (contact teams): 0.61 — above warning threshold, flag as inference only
# No regeneration triggered (all claims above 0.4)
# One annotation added to response
```

**Final response confidence: 0.87** — Displayed to engineer as a confidence indicator in the UI.

**Why this matters:**

The hallucinated config key example illustrates the core value proposition: `spark.sql.adaptive.skewThreshold=0.5` looks entirely plausible to a data engineer. It follows Spark's config naming convention, the value 0.5 seems reasonable for a threshold, and the description sounds right. An engineer could spend 20+ minutes adding this to their cluster config, restarting the cluster, re-running the job, and finding the error still occurs — only to discover the config key had no effect because it doesn't exist.

Self-RAG catches this by verifying each claim against the retrieved knowledge base before the response is displayed. Hallucinated config keys, incorrect threshold values, and wrong API signatures are all patterns that Self-RAG consistently catches, because they are specific enough that they either appear in the knowledge base exactly or they don't.

The annotation on Claim 5 ("contact teams is inference, not documented procedure") is also valuable: it tells the engineer this is the system's recommendation based on reasoning, not an established runbook step. An experienced engineer will recognize this is obviously correct; a junior engineer now knows this is not in the runbook and may want to verify with their team before paging.

---

## Technique Comparison: Which Was Most Impactful?

| Technique | Impact | Why |
|---|---|---|
| Azure AI Search Hybrid (Vector + BM25) | ★★★★★ | Foundation of all retrieval. BM25 caught exact error codes; vector caught semantic matches. Without this, both exact error class lookups AND conceptual doc retrieval fail simultaneously. |
| GraphRAG | ★★★★★ | Unique, irreplaceable value: blast radius and SLA data exists NOWHERE in the text corpus. Only the dependency graph has it. Without this, two more incidents are created at 05:00 and 05:30 AM. |
| CRAG | ★★★★☆ | Discarded 11 irrelevant chunks. Without CRAG, Kusto and ADLS noise pollutes the LLM context, shifting the response toward generic "check your memory" advice instead of specific Spark resolution steps. |
| Query Rewriting | ★★★★☆ | The LLM rewrite introduces `data skew`, `memoryOverhead`, `AQE skewJoin` — terms that appear in the correct runbooks but not in the raw error. Without it, retrieval indexes on "Java heap space" — a string that appears in 47 different runbooks across 6 systems. |
| RAG Fusion | ★★★★☆ | Retrieved the historical ICM incident via the F3 (historical) variant. Single-query retrieval would never find it because the incident's text ("OOM again") doesn't semantically match the query ("spark.executor.memory"). |
| Parent-Child Chunking | ★★★★☆ | Expanded the fix snippet into the full OOM troubleshooting section including the critical "which OOM type does this fix apply to" disambiguation. Without this, the engineer applies the wrong fix (memory increase for a skew-induced OOM). |
| Semantic Chunking | ★★★★☆ | Kept the complete Resolution section with all three options intact. Fixed-token chunking would have split Option C (key salting) at step 3 of 5, producing an incomplete and potentially data-corrupting procedure. |
| Cross-Encoder Reranking | ★★★★☆ | Promoted ICM-2831204 from rank 5 → 2. Without reranking, the response leads with the generic memory sizing formula rather than the historical incident that already solved this exact problem 2 weeks ago. |
| HyDE | ★★★☆☆ | Retrieved the "Join Key Salting Pattern" doc and "Spark UI Task Metrics Guide" — both useful but also retrievable via LLM rewrite variants. For this well-known error, HyDE's contribution is additive but not unique. |
| Topic Tree | ★★★☆☆ | Correctly excluded Kusto/Synapse docs from the search space, improving precision. Essential for multi-domain deployments (12K chunks), but less critical if the KB were Spark-only. |
| Agentic RAG | ★★★☆☆ | Routed F3 to the ICM index, guaranteeing the historical incident is found. Largely duplicates RAG Fusion's benefit for this scenario; highest independent value when different source types have very different writing styles. |
| FLARE | ★★★☆☆ | Replaced hedged ADLS I/O advice with specific 5 Gbps throughput numbers. Important for edge-case follow-up questions (ADLS throttling) but not critical for the primary OOM resolution. |
| Self-RAG | ★★★☆☆ | Validated all config keys against the KB; would have caught hallucinated `spark.sql.adaptive.skewThreshold=0.5`. The 0.87 confidence score also helps the engineer calibrate trust in the response. |

**The critical backbone for this scenario: Azure AI Search Hybrid + GraphRAG + CRAG**

- **Hybrid search** is the foundation: without it, either exact error class matching or conceptual matching fails, and the right runbooks are not retrieved.
- **GraphRAG** provides information that cannot be retrieved from any text: downstream SLA breaches in 75 minutes are not in any Confluence runbook. Only the graph knows.
- **CRAG** is the quality gate: without it, the LLM context is contaminated by 11 plausible-but-wrong chunks, and the response degrades from specific Spark resolution advice to generic memory management guidance.

---

## What Would Happen WITHOUT the Full Pipeline

| Disabled Technique | Actual Result |
|---|---|
| **No Query Rewriting** | V4 (original) query "java.lang.OutOfMemoryError: Java heap space" retrieves the Kafka OOM runbook (#1 by BM25), the ADLS Java SDK OOM guide (#2), and generic JVM tuning docs. Spark-specific runbooks rank #6-8. The response tells the engineer to check heap dumps and increase JVM -Xmx — valid Java advice, completely wrong for Spark executor configuration. Resolution time: 60+ minutes instead of 15. |
| **No RAG Fusion** | Historical variant F3 is never issued. ICM-2831204 (same pipeline, fixed 2 weeks ago by key salting) is never retrieved. The response walks through the diagnosis steps from scratch, recommends AQE settings (correct), and misses that key salting was already validated as the definitive fix for this specific pipeline. The engineer tries AQE first, finds it insufficient for the severity of skew, then independently discovers key salting after 35 more minutes. |
| **No CRAG** | All 25 chunks enter the LLM context, including the Kusto Ingestion Worker OOM runbook, the ADLS Java SDK guide, and the Synapse SQL Pool Memory Grant guide. The LLM averages across these and generates: "This error can occur in various components. Check your memory configuration for the relevant service, review ingestion batch sizes, and consult the appropriate runbook for your platform." Completely generic. The engineer is back to Google. |
| **No GraphRAG** | The response correctly identifies the OOM and provides the resolution (AQE + key salting). The engineer applies the fix, the pipeline succeeds at 04:50 AM. At 05:00 AM, the kusto_ingestion_telemetry team pages because their SLA is breached. At 05:30 AM, the Synapse team pages. The data platform engineer is pulled back into incident calls. Total engineer-hours: 3 (1 fixing OOM + 1 handling Kusto follow-up + 1 handling Synapse follow-up). With GraphRAG: 1 engineer handles everything proactively. |
| **No Cross-Encoder Reranking** | ICM-2831204 stays at bi-encoder rank 5. The response leads with the Executor Memory Sizing Guide formulas ("total executor memory = task memory + shuffle memory + storage memory × executors"). The engineer calculates the right memory value, applies it, and the pipeline succeeds. But the response never mentions that key salting was the validated fix 2 weeks ago — so the engineer applies a temporary fix (increased memory) rather than the permanent fix (AQE + salting). The pipeline OOMs again in 3 weeks under higher data volume. |
| **No Semantic Chunking** | The fixed-token chunk containing Option C (key salting) ends after step 3: "Expand the smaller table 10x with a salt range." Steps 4 (drop salt column) and 5 (validate row count) are truncated. The engineer applies steps 1-3, runs the pipeline, and the output DataFrame contains a `salt` column that propagates into the Delta curated table, causing a schema mismatch in the downstream Synapse pipeline. A second incident (schema mismatch) is created 2 hours later. |
| **No Self-RAG** | If the LLM hallucinated `spark.sql.adaptive.skewThreshold=0.5` (a realistic hallucination for this error type), the engineer adds this non-existent config key to the cluster, restarts, re-runs the pipeline, and finds the same OOM occurring (the config key was silently ignored by Spark). The engineer concludes the recommendation was wrong and escalates to L2. With Self-RAG, the hallucinated key is caught and replaced with the correct `spark.sql.adaptive.skewJoin.skewedPartitionFactor=5` before the response is displayed. |
| **No Parent-Child Chunking** | The child chunk "Increase spark.executor.memory to 8g and spark.executor.memoryOverhead to 2g" is retrieved, but the parent context explaining "this is for executor shuffle-stage OOM, not driver OOM" is absent. The engineer applies Option A (memory increase) to a skew-induced OOM. The pipeline runs with more memory but still OOMs — the skewed partition is now 40% of 8g instead of 4g, still exceeding capacity. Another failed run, another 45-minute investigation. |

---

## Full Technique Execution Timeline

```
T+0.0s   User submits: "java.lang.OutOfMemoryError: Java heap space on spark_etl_daily_ingest"
T+0.1s   [T1] Topic Tree classification → topic_context="OutOfMemory|Shuffle Failures"
T+0.2s   [T2] Query rewriting begins (synonym + context enrichment)
T+0.5s   [T2] GPT-4o-mini LLM rewrite completes → 4 query variants
T+0.6s   [T3] HyDE template matched → hypothetical doc generation begins
T+0.7s   [T4] RAG Fusion LLM call → 3 additional query variants
T+1.1s   [T3] HyDE hypothetical doc generated → embedded (3072 dims)
T+1.2s   [T5/T6] Azure AI Search: 8 queries issued in parallel across Confluence/ICM/LogAnalytics
T+1.8s   [T5] Hybrid search results returned: ~40 raw candidates
T+1.9s   [T7] Parent-child expansion: 4 child chunks → parent chunks
T+2.0s   [T10] GraphRAG lookup: spark_etl_daily_ingest dependency graph traversal
T+2.1s   [T10] Graph context chunk generated: blast radius + SLA analysis
T+2.3s   [T9] CRAG grading: 25 chunks → 14 survive (6 CORRECT, 8 AMBIGUOUS)
T+2.4s   [T9] Top 8 selected (6 CORRECT + 2 highest AMBIGUOUS)
T+2.5s   [T10] Graph chunk added: 8 → 9 chunks
T+2.6s   [T11] Cross-encoder reranking: 9 chunks scored pairwise against query
T+2.9s   [T11] Reranking complete: final ordered context (9 chunks)
T+3.0s   [T12/T13] Generation begins: LLM streaming response with FLARE monitoring
T+5.2s   [T12] FLARE trigger 1: hedging on ADLS throughput → retrieval + injection
T+5.8s   [T12] FLARE trigger 1 resolved: specific throughput numbers injected
T+7.1s   [T12] FLARE trigger 2: LLM uncertainty on KQL syntax → retrieval + injection
T+7.4s   [T12] FLARE trigger 2 resolved: KQL template injected
T+9.3s   [T13] Generation complete. Self-RAG evaluation begins.
T+9.5s   [T13] Claim 5 flagged as inference → annotation appended
T+9.6s   [T13] Overall confidence: 0.87 → no warnings, response finalized
T+9.7s   Response displayed to engineer with confidence: 0.87
```

**Total latency: ~9.7 seconds** from user input to complete response.

---

## Full Response Delivered to Engineer

The following is the complete response Alert Whisperer delivers to the engineer at T+9.7s:

---

> **Alert Whisperer** *(confidence: 0.87)*
>
> **Diagnosis: Data Skew-Induced Executor OOM on Stage 12 Shuffle Read**
>
> Based on the stack trace and stage context, this is a data skew OOM — executor-008 is receiving a disproportionate portion of the shuffle data during the `customer_id` join in stage 12.
>
> **Confirm with Spark UI (30 seconds):**
> 1. Open Spark UI → Stages → Stage 12 → Tasks
> 2. Sort by "Shuffle Read Size" descending
> 3. If the top task shows > 5× the median: **confirmed data skew** → proceed with Option B below
>
> **Option B (Preferred — for data skew):**
> ```properties
> spark.sql.adaptive.enabled=true
> spark.sql.adaptive.skewJoin.enabled=true
> spark.sql.adaptive.skewJoin.skewedPartitionFactor=5
> spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes=256m
> ```
> No cluster restart required. Apply as session config and re-run.
>
> **Option A (Fallback — if all tasks are uniformly failing, not skewed):**
> ```properties
> spark.executor.memory=8g
> spark.executor.memoryOverhead=2g
> ```
> Requires cluster restart.
>
> **Historical precedent:** ICM-2831204 (2026-02-22) — same pipeline, same error. Resolved in 23 minutes by salting the `customer_id` join key with a random suffix 0-9. If AQE does not fully resolve the skew, consider key salting as the permanent fix.
>
> **⚠️ Downstream Impact (act within 75 minutes):**
> - `kusto_ingestion_telemetry`: BLOCKED, SLA deadline 05:00 AM CDT (75 min) — notify immediately
> - `synapse_pipeline_customer360`: BLOCKED, SLA deadline 05:30 AM CDT (105 min) — notify soon
> Both pipelines will auto-retry once `curated_events` Delta table is available.
> *(Note: downstream notification is recommended based on dependency graph analysis, not a documented runbook procedure.)*
>
> **ADLS note:** If the OOM persists after applying the above fixes, check for ADLS throttling. Maximum write throughput for this storage account is 5 Gbps. At 500 GB / ~5 hrs ≈ 28 MB/s average write rate, throttling is unlikely, but look for `HDInsightThrottlingException` in Spark logs if issues continue.

---

*This document is part of the Alert Whisperer architecture reference. See `/docs/architecture/` for system design and `/docs/techniques/` for individual technique deep-dives.*
